"""本地 LLM API 代理 + token 流量实时检测器（show-me-the-story 模式）。

**只服务 dsh**：`dsh_bridge` 把 dsh 子进程的 `DEEPSEEK_BASE_URL` 指向本代理，
代理再把请求转发到用户在 `/settings` 配置的**真实上游**，顺手注入
`include_usage:true` 并从响应里解析 usage 实时累计，暴露 `/token-usage` 供侧栏轮询
（无 usage 时按 ~1.5 tokens/字估算兜底）。Python 侧 LLMClient 不经这里。

代理同时是**配置执行点**：上游地址、模型名、API key、TLS 开关都在这一层统一按
api.json 落地，所以 dsh 不必自己认识这些配置；而 api.json 每次请求都重读，
改完设置对 dsh 立即生效、无需重启。

上游地址 = 设置页的 `base_url`；只有当它指向本代理自身（防自环）时才回退旧版的
`real_base_url`。两者都没有则明确报配置错误，不再静默兜底 api.deepseek.com。

用法：from libraries.token_proxy import ensure_proxy, get_token_usage, clear_token_usage
"""
import codecs
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import requests  # noqa: E402

from core.api_config import load_api_config
from core.llm_client import normalize_base_url
from core.models import APIConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def token_proxy_port() -> int:
    """代理端口（NE_TOKEN_PROXY_PORT 非法时回退 58082）。"""
    try:
        port = int(os.environ.get("NE_TOKEN_PROXY_PORT", "58082"))
    except (TypeError, ValueError):
        return 58082
    return port if 1 <= port <= 65535 else 58082


PROXY_PORT = token_proxy_port()

# 本机回环地址：用于判定某 URL 是否指向代理自身（防转发自环）
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class _UsageAccumulator:
    """token 累计器：committed（已结束的调用）+ pending（流式中实时估算）。

    镜像 show-me-the-story 的 TaskTokenUsage（tokens.go）：流式 LLM 调用期间
    pending 随已收字符实时估算，供前端「实时滚动」；流结束用真实 usage 提交进
    committed。ThreadingHTTPServer 每连接一线程，用 dict 支持并发 in-flight。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.prompt = 0
        self.completion = 0
        self.calls = 0
        self._inflight = {}   # key -> {"prompt": est, "completion": est}
        self._seq = 0

    def accumulate(self, prompt, completion):
        with self.lock:
            self.prompt += int(prompt or 0)
            self.completion += int(completion or 0)
            self.calls += 1

    def begin_inflight(self, est_prompt):
        """流式调用开始：按请求 messages 估算 prompt 注册 in-flight，返回 key。"""
        with self.lock:
            self._seq += 1
            key = self._seq
            self._inflight[key] = {"prompt": int(est_prompt or 0), "completion": 0}
            return key

    def update_inflight(self, key, est_completion):
        """流式逐 chunk：更新该调用的 pending completion（实时滚动数据源）。"""
        with self.lock:
            rec = self._inflight.get(key)
            if rec is not None:
                rec["completion"] = int(est_completion or 0)

    def finish_inflight(self, key, prompt, completion):
        """流式正常结束：真实 usage 提交进 committed，移除 in-flight。"""
        with self.lock:
            self._inflight.pop(key, None)
            self.prompt += int(prompt or 0)
            self.completion += int(completion or 0)
            self.calls += 1

    def abort_inflight(self, key):
        """流异常中断：丢弃该 in-flight（不提交，避免误计）。"""
        with self.lock:
            self._inflight.pop(key, None)

    def snapshot(self):
        with self.lock:
            pend_p = sum(r["prompt"] for r in self._inflight.values())
            pend_c = sum(r["completion"] for r in self._inflight.values())
            # total 只含流式中「生成中」的 pending_completion（实时滚动来源）；不含
            # pending_prompt：dsh 上下文巨大，char×1.5 对 prompt 高估严重，计入会造成
            # 「先涨后跌」伪影。prompt 真实成本在调用提交时精确计入（total 向上跳）。
            total = self.prompt + self.completion + pend_c
            return {"prompt": self.prompt, "completion": self.completion,
                    "calls": self.calls, "pending_prompt": pend_p,
                    "pending_completion": pend_c, "total": total}

    def clear(self):
        with self.lock:
            self.prompt = 0
            self.completion = 0
            self.calls = 0
            self._inflight.clear()


USAGE = _UsageAccumulator()


def token_proxy_base_url(port: int | None = None) -> str:
    """本代理的监听地址（dsh_bridge 注入 DEEPSEEK_BASE_URL 用，认 NE_TOKEN_PROXY_PORT）。"""
    return f"http://127.0.0.1:{port or PROXY_PORT}"


def is_token_proxy_url(url: str, port: int | None = None) -> bool:
    """该地址是否指向本代理自身 —— 用于防止把代理再转发给代理（自环）。"""
    parsed = urlsplit((url or "").strip())
    if not parsed.hostname or parsed.hostname.lower() not in LOOPBACK_HOSTS:
        return False
    try:
        effective = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return effective == (port or PROXY_PORT)


def resolve_upstream(cfg: APIConfig | None) -> str:
    """代理的上游地址 = 设置页 `base_url`。

    仅当 base_url 指向代理自身（旧配置把本地代理写成 API 地址）时才回退
    `real_base_url`。都没有则返回空串 —— 由调用方 fail closed，不做静默兜底。
    """
    if cfg is None:
        return ""
    base = (cfg.base_url or "").strip()
    if base and not is_token_proxy_url(base):
        return base
    legacy = (cfg.real_base_url or "").strip()
    if legacy and not is_token_proxy_url(legacy):
        return legacy
    return ""


def _proxy_config() -> tuple[APIConfig | None, str, str]:
    """返回 (配置, 上游地址, 错误说明)。错误说明非空时不得发起上游请求。"""
    cfg = load_api_config()
    if cfg is None:
        return None, "", "api.json 不存在"
    if not cfg.api_key:
        return cfg, "", "未配置 API Key"
    upstream = resolve_upstream(cfg)
    if not upstream:
        return cfg, "", "API 地址为空或指向本地代理自身"
    return cfg, upstream, ""


# 最近一次转发摘要（供 /health 诊断；只放 origin/model，绝不放 key）
_LAST_REQUEST: dict = {}


def _origin_of(url: str) -> str:
    """只取 scheme://netloc —— 日志/健康检查里不回显路径与查询串。"""
    parsed = urlsplit((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _estimate_tokens(chars):
    """兜底估算：API 未返回 usage 时 ~1.5 tokens/字（show-me-the-story EstimateTokensFromRunes）。"""
    return int(float(chars) * 1.5)


def _parse_usage_objs(text):
    """从流式 SSE data 行/JSON 里提取最后一个 usage（OpenAI 兼容格式）。"""
    prompt = completion = 0
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
            u = obj.get("usage") or {}
            if u:
                prompt = u.get("prompt_tokens") or 0
                completion = u.get("completion_tokens") or 0
        except Exception:
            pass
    return prompt, completion


def _count_output_chars(text):
    """累计 SSE 流中实际生成文本的字符数（choices[0].delta.content + reasoning_content）。

    只数增量文本、不算 `data:` 包装与 JSON 键（否则把响应体积误当 token 高估），
    与 show-me-the-story 的 updateStreamContent 只计 delta.Content 同思路；
    加上 reasoning_content 让流式估算覆盖 DeepSeek 的思考段（真实 completion_tokens 含它）。
    """
    n = 0
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                for key in ("content", "reasoning_content"):
                    c = delta.get(key)
                    if c:
                        n += len(c)
        except Exception:
            pass
    return n


def _local_http_proxy_fallbacks(url: str) -> dict:
    """Normalize Windows' HTTPS proxy registry entry for local HTTP proxies.

    Clash-like local listeners commonly expose HTTP CONNECT on 7897 while
    WinHTTP/urllib reports the registry value as ``https://127.0.0.1:7897``.
    Requests then attempts TLS to the proxy itself and raises SSLEOFError.
    """
    proxies = requests.utils.get_environ_proxies(url) or {}
    fixed = {}
    changed = False
    for scheme, proxy in proxies.items():
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme == "https" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                proxy = urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
                changed = True
        except (TypeError, ValueError):
            pass
        fixed[scheme] = proxy
    return fixed if changed else {}


def _forward(target: str, payload: dict, headers: dict, verify_ssl: bool = True,
             timeout_seconds: int = 600):
    """转发到上游；TLS 校验跟随 api.json 的 verify_ssl（上游证书有兼容问题时才关）。

    两条路径（直连 + ProxyError 后的本机代理纠正）都必须带同一个 verify，
    否则"关掉校验"的配置只在一种情形下生效，表现为偶发证书报错。
    """
    try:
        return requests.post(target, json=payload, headers=headers, stream=True,
                             timeout=timeout_seconds, verify=verify_ssl)
    except requests.exceptions.ProxyError:
        fallback = _local_http_proxy_fallbacks(target)
        if not fallback:
            raise
        return requests.post(target, json=payload, headers=headers, proxies=fallback,
                             stream=True, timeout=timeout_seconds, verify=verify_ssl)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/token-usage":
            # last_request 只含 origin/model，便于肉眼确认 dsh 打到了哪个上游
            self._send_json({"ok": True, **USAGE.snapshot(),
                             "last_request": dict(_LAST_REQUEST)})
        elif self.path.rstrip("/") == "/token-usage/clear":
            USAGE.clear()
            self._send_json({"ok": True})
        elif self.path.rstrip("/") == "/health":
            # 供 dsh_bridge / 人工排查：这个端口上跑的到底是不是当前代码的代理，
            # 以及它把流量发去了哪里（只回 origin + 模型名，绝不回 key）。
            cfg, upstream, err = _proxy_config()
            self._send_json({
                "ok": not err,
                "service": "novelengine-token-proxy",
                "port": PROXY_PORT,
                "error": err,
                "upstream": _origin_of(upstream),
                "model": (cfg.model if cfg else ""),
                "verify_ssl": (cfg.verify_ssl if cfg else None),
                "last_request": dict(_LAST_REQUEST),
            })
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        # 注入 include_usage（流式需 stream_options），让响应带 usage。
        # 合并而非整体覆盖：别丢掉调用方自己加的 stream_options。
        if payload.get("stream"):
            stream_options = dict(payload.get("stream_options") or {})
            stream_options["include_usage"] = True
            payload["stream_options"] = stream_options

        cfg, upstream, err = _proxy_config()
        if err:
            # fail closed：宁可明确报"去设置页配置"，也不要静默打到别的上游
            self._send_json({"ok": False, "error":
                             f"token proxy 配置不可用：{err}（请在 /settings 保存 API 地址与 Key）"}, 502)
            return

        # 路径归一：dsh 请求的是 `<代理>/chat/completions`，上游 base 可能已带 /v1
        # 或完整 endpoint —— 直接用 normalize_base_url 生成，与 Python 侧完全一致。
        target = normalize_base_url(upstream, cfg.url_strict)

        # 模型与 key 都以设置页为准：dsh 发的是它自己的 deepseek-v4-flash 和
        # ~/.dsh/.env 里的旧 key，直接透传会打不动用户新配的中转。
        if cfg.model:
            payload["model"] = cfg.model
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        }
        incoming_auth = self.headers.get("Authorization")
        if cfg.api_key:
            headers["Authorization"] = "Bearer " + cfg.api_key
        elif incoming_auth:
            # 设置页没填 key 时才退回 dsh 自带的（仅为了不把请求发成匿名）
            headers["Authorization"] = incoming_auth

        _LAST_REQUEST.update({
            "upstream": _origin_of(target),
            "model": payload.get("model", ""),
            "verify_ssl": cfg.verify_ssl,
            "at": time.time(),
        })
        try:
            resp = _forward(target, payload, headers,
                            verify_ssl=cfg.verify_ssl,
                            timeout_seconds=max(60, int(cfg.http_timeout_seconds or 600)))
        except Exception as e:
            self._send_json({"ok": False, "error": f"proxy forward failed: {e}"}, 502)
            return
        # 透传状态码 + 头（剔除 length/encoding，避免与透传体冲突）
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.end_headers()
        prompt = completion = 0
        chars = 0
        inflight_key = None
        stream_ok = False
        try:
            if payload.get("stream"):
                # 流式：注册 in-flight，逐 chunk 按已收字符实时估算 pending（token 实时滚动数据源）
                est_prompt = _estimate_tokens(sum(
                    len(str(m.get("content", ""))) for m in (payload.get("messages") or [])))
                inflight_key = USAGE.begin_inflight(est_prompt)
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                char_count = 0
                text_tail = ""
                buf = b""
                for chunk in resp.iter_content(chunk_size=1024):
                    if not chunk:
                        continue
                    self.wfile.write(chunk)      # 实时透传（保持 SSE 原始格式，不破坏 dsh 流解析）
                    self.wfile.flush()
                    buf += chunk
                    # 只处理完整 data: 行，按实际输出文本(delta.content/reasoning_content)估 token
                    text_tail += decoder.decode(chunk)
                    nl = text_tail.rfind("\n")
                    if nl >= 0:
                        char_count += _count_output_chars(text_tail[:nl + 1])
                        text_tail = text_tail[nl + 1:]
                        USAGE.update_inflight(inflight_key, _estimate_tokens(char_count))
                decoder.decode(b"", final=True)
                chars = len(buf)
                prompt, completion = _parse_usage_objs(buf.decode("utf-8", errors="replace"))
                stream_ok = True
            else:
                content = resp.content
                self.wfile.write(content)
                self.wfile.flush()
                chars = len(content)
                try:
                    obj = json.loads(content)
                    u = obj.get("usage") or {}
                    if u:
                        prompt = u.get("prompt_tokens") or 0
                        completion = u.get("completion_tokens") or 0
                except Exception:
                    pass
                stream_ok = True
        except Exception:
            pass
        # 无 usage → 兜底估算（~1.5 tokens/字，prompt 占 1/3、completion 占 2/3）
        if not prompt and not completion and chars:
            est = _estimate_tokens(chars)
            prompt, completion = est // 3, est - est // 3
        if inflight_key is not None:
            if stream_ok:
                USAGE.finish_inflight(inflight_key, prompt, completion)
            else:
                USAGE.abort_inflight(inflight_key)
        else:
            USAGE.accumulate(prompt, completion)


_started = threading.Event()


class _ProxyServer(ThreadingHTTPServer):
    """Windows 上必须关掉 allow_reuse_address。

    socketserver 默认 allow_reuse_address=1（映射到 SO_REUSEADDR），Windows 的
    SO_REUSEADDR 语义与 Linux 不同：它允许**抢占**另一个进程已监听的端口 ——
    两个进程会同时"socket 监听成功"并瓜分进来的连接（实测：平台在跑的时候，
    另一个进程也能 ensure_proxy() 返回 True，然后 /chat/completions 与 /token-usage
    被两个进程分别应答，token 统计直接错乱）。关掉它，"端口已被占用"才会如实报错，
    dsh_bridge 的 fail-closed 护栏才成立。
    """
    allow_reuse_address = False


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """先探一次连接：有人监听就别再 bind（Windows 下 bind 不会失败）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def ensure_proxy() -> bool:
    """确保代理在跑（幂等；dsh_bridge / web_ui 惰性拉起 daemon 线程）。

    返回 False 表示**本进程没能成为代理**（端口被别的进程占着）。
    此时 dsh 的流量会落进那个进程 —— 若它是旧代码的残留代理，表现就是
    "改完设置不生效"。调用方（dsh_bridge）据此 fail closed 并提示用户清进程。
    """
    if _started.is_set():
        return True
    if _port_in_use(PROXY_PORT):
        print(f"[token-proxy] 127.0.0.1:{PROXY_PORT} 已被占用，本进程不再监听"
              "（另一个 NovelEngine / 残留进程在跑；用 /health 确认它是不是当前代码）")
        return False
    try:
        server = _ProxyServer(("127.0.0.1", PROXY_PORT), _Handler)
    except Exception as e:  # 端口占用等
        print(f"[token-proxy] 无法监听 127.0.0.1:{PROXY_PORT}: {e}")
        return False
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _started.set()
    return True


def probe_proxy(timeout: float = 1.5) -> dict:
    """探测该端口上跑的是不是当前代码的 token 代理（排查残留进程用）。"""
    try:
        resp = requests.get(token_proxy_base_url() + "/health", timeout=timeout)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "error": f"端口 {PROXY_PORT} 无响应或不是本代理: {e}"}
    if data.get("service") != "novelengine-token-proxy":
        return {"ok": False, "error": f"端口 {PROXY_PORT} 被其他程序占用"}
    return data


def get_token_usage() -> dict:
    return USAGE.snapshot()


def clear_token_usage() -> None:
    USAGE.clear()
