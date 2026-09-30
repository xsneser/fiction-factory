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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import requests  # noqa: E402

from core.api_config import (
    load_api_config,
    load_api_settings,
    resolve_agent_route,
    ROUTE_PREFIX,
    SUPPORTED_ROLES,
    DEFAULT_MODEL,
    PLACEHOLDER_API_KEYS,
)
from core.llm_client import normalize_base_url, apply_reasoning_fields
from core.models import APIConfig, APIProviderConfig, AgentRouteConfig
from core.provider_discovery import resolve_provider_endpoint

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
    增加 by_route 字典支持按角色/供应商维度追踪。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.prompt = 0
        self.completion = 0
        self.calls = 0
        self._inflight = {}   # key -> {"prompt": est, "completion": est}
        self._by_route = {}   # role -> {"prompt": int, "completion": int, "calls": int, "provider": str, "model": str}
        self._seq = 0

    def accumulate(self, prompt, completion, role: str = "", provider_id: str = "", model: str = ""):
        with self.lock:
            p = int(prompt or 0)
            c = int(completion or 0)
            self.prompt += p
            self.completion += c
            self.calls += 1
            if role:
                r = self._by_route.setdefault(role, {
                    "prompt": 0, "completion": 0, "calls": 0,
                    "provider": provider_id, "model": model
                })
                r["prompt"] += p
                r["completion"] += c
                r["calls"] += 1
                if provider_id:
                    r["provider"] = provider_id
                if model:
                    r["model"] = model

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

    def finish_inflight(self, key, prompt, completion, role: str = "", provider_id: str = "", model: str = ""):
        """流式正常结束：真实 usage 提交进 committed，移除 in-flight。"""
        with self.lock:
            self._inflight.pop(key, None)
            p = int(prompt or 0)
            c = int(completion or 0)
            self.prompt += p
            self.completion += c
            self.calls += 1
            if role:
                r = self._by_route.setdefault(role, {
                    "prompt": 0, "completion": 0, "calls": 0,
                    "provider": provider_id, "model": model
                })
                r["prompt"] += p
                r["completion"] += c
                r["calls"] += 1
                if provider_id:
                    r["provider"] = provider_id
                if model:
                    r["model"] = model

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
            return {
                "prompt": self.prompt, "completion": self.completion,
                "calls": self.calls, "pending_prompt": pend_p,
                "pending_completion": pend_c, "total": total,
                "by_route": {k: dict(v) for k, v in self._by_route.items()},
            }

    def clear(self):
        with self.lock:
            self.prompt = 0
            self.completion = 0
            self.calls = 0
            self._inflight.clear()
            self._by_route.clear()


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
    if not cfg.api_key or cfg.api_key in PLACEHOLDER_API_KEYS:
        return cfg, "", "未配置有效 API Key"
    upstream = resolve_upstream(cfg)
    if not upstream:
        return cfg, "", "API 地址为空或指向本地代理自身"
    return cfg, upstream, ""


def _proxy_resolve(req_model: str = "") -> tuple[APIProviderConfig | None, AgentRouteConfig | None, str, str]:
    """按请求模型/角色解析目标供应商与实际模型路由。

    返回 (provider, route, upstream, err)。
    """
    settings = load_api_settings()
    if not settings.providers:
        return None, None, "", "api.json 未配置任何供应商"

    provider, route = resolve_agent_route(req_model, settings)
    if not provider or not provider.enabled:
        return None, None, "", f"目标供应商 [{route.provider_id}] 不存在或已禁用"
    if not provider.api_key or provider.api_key in PLACEHOLDER_API_KEYS:
        return provider, route, "", f"供应商 [{provider.name}] 未配置有效 API Key"

    upstream = (provider.base_url or "").strip()
    if not upstream or is_token_proxy_url(upstream):
        return provider, route, "", f"供应商 [{provider.name}] API 地址为空或指向本地代理自身"

    return provider, route, upstream, ""


# 最近一次转发摘要（供 /health 诊断；只放 origin/model，绝不放 key）
_LAST_REQUEST: dict = {}


def _origin_of(url: str) -> str:
    """只取 scheme://netloc —— 日志/健康检查里不回显路径与查询串。"""
    parsed = urlsplit((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    # Never expose URL userinfo through health/token diagnostics.
    host = parsed.hostname or ""
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    suffix = f":{port}" if port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def llm_exit_info(role: str = "orchestrator") -> dict:
    """当前 LLM 出口的可公开摘要：{"role", "provider_id", "provider_name", "model", "upstream"}（**绝不回 key**）。

    供错误提示与出口预检复用：说清「是哪个模型、打到哪个上游」是排查上游故障的
    最小信息量，而这些都不敏感。
    """
    provider, route, upstream, _err = _proxy_resolve(role)
    if not provider:
        cfg, up, _ = _proxy_config()
        return {
            "role": role,
            "provider_id": "",
            "provider_name": "",
            "model": (cfg.model if cfg else "") or "",
            "upstream": _origin_of(up) if up else "",
        }
    return {
        "role": role,
        "provider_id": provider.id,
        "provider_name": provider.name,
        "model": (route.model or provider.default_model) if route else provider.default_model,
        "upstream": _origin_of(upstream),
    }


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
            settings = load_api_settings()
            def_p = settings.providers.get(settings.default_provider)
            if not def_p and settings.providers:
                def_p = list(settings.providers.values())[0]

            self._send_json({
                "ok": bool(def_p and def_p.api_key),
                "service": "novelengine-token-proxy",
                "port": PROXY_PORT,
                "default_provider": settings.default_provider,
                "providers_count": len(settings.providers),
                "active_providers": [p.id for p in settings.providers.values() if p.enabled],
                "upstream": _origin_of(def_p.base_url) if def_p else "",
                "model": def_p.default_model if def_p else "",
                "verify_ssl": def_p.verify_ssl if def_p else True,
                "last_request": dict(_LAST_REQUEST),
                # 最近若干次请求的**形状**（工具数/字符数/max_tokens/输出 block 数，不含正文）：
                # 「上游整段时间零输出」这类故障靠它定性
                "recent": recent_request_shapes(20),
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

        req_model = str(payload.get("model") or "").strip()
        provider, route, upstream, err = _proxy_resolve(req_model)
        if err:
            # fail closed：宁可明确报"去设置页配置"，也不要静默打到别的上游
            self._send_json({"ok": False, "error":
                             f"token proxy 配置不可用：{err}（请在 /settings 保存 API 地址与 Key）"}, 502)
            return

        # 路径归一：dsh 请求的是 `<代理>/chat/completions`，上游 base 可能已带 /v1
        # 或完整 endpoint —— 直接用 normalize_base_url 生成，与 Python 侧完全一致。
        try:
            target = resolve_provider_endpoint(provider, "chat")
        except Exception:
            target = normalize_base_url(upstream, provider.url_strict)

        # 确定实际请求模型名（替换保留角色别名或跟随 route）
        actual_model = route.model or provider.default_model or DEFAULT_MODEL
        payload["model"] = actual_model

        # max_tokens 覆盖
        if route.max_tokens and route.max_tokens > 0:
            payload["max_tokens"] = route.max_tokens

        # 处理 reasoning / thinking 协议（与 core.llm_client 共用同一套语义）
        apply_reasoning_fields(payload, provider.reasoning_wire, route.reasoning_effort)

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            "Accept-Encoding": "identity",
        }
        incoming_auth = self.headers.get("Authorization")
        if provider.api_key:
            headers["Authorization"] = "Bearer " + provider.api_key
        elif incoming_auth:
            # 设置页没填 key 时才退回 dsh 自带的（仅为了不把请求发成匿名）
            headers["Authorization"] = incoming_auth

        role_name = (
            req_model[len(ROUTE_PREFIX):] if req_model.startswith(ROUTE_PREFIX)
            else req_model
        )
        # 未知别名会安全回退到 orchestrator 路由；归账也必须记录实际采用的角色，
        # 不要让一个拼写错误制造出无法在设置页配置的虚假 route 名。
        if role_name not in SUPPORTED_ROLES:
            role_name = "orchestrator"

        _LAST_REQUEST.update({
            "role": role_name,
            "provider_id": provider.id,
            "provider_name": provider.name,
            "upstream": _origin_of(target),
            "model": actual_model,
            "verify_ssl": provider.verify_ssl,
            "at": time.time(),
        })
        try:
            resp = _forward(target, payload, headers,
                            verify_ssl=provider.verify_ssl,
                            timeout_seconds=max(60, int(provider.http_timeout_seconds or 600)))
        except Exception as e:
            self._send_json({"ok": False, "error": f"proxy forward failed: {e}"}, 502)
            return
        # Reject upstream errors before emitting a streaming response. Otherwise
        # an SSE-formatted 401/429 body is mistaken for a successful completion
        # and can be counted as generated tokens.
        if resp.status_code < 200 or resp.status_code >= 300:
            try:
                detail = resp.content.decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            try:
                resp.close()
            except Exception:
                pass
            self._send_json({"ok": False, "error":
                             f"upstream returned HTTP {resp.status_code}: {detail}"}, 502)
            return

        # 透传状态码 + 头（剔除 length/encoding，避免与透传体冲突；已由 requests 解压缩的正文不得带 content-encoding）
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.end_headers()
        prompt = completion = 0
        chars = 0
        inflight_key = None
        stream_ok = False
        buf = b""          # 流式透传时累积的响应体（供形状留痕解析）
        content = b""      # 非流式响应体
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
                USAGE.finish_inflight(inflight_key, prompt, completion,
                                      role=role_name, provider_id=provider.id, model=actual_model)
            else:
                USAGE.abort_inflight(inflight_key)
        else:
            USAGE.accumulate(prompt, completion,
                             role=role_name, provider_id=provider.id, model=actual_model)
        # 请求**形状**留痕（只计数、绝不含正文）：上游整段时间零输出这类故障，
        # 有这张表第一次出现就能定性，不必靠反推 token 数（2026-09-19 就是这么查了一晚上）。
        try:
            _body = (buf if payload.get("stream") else content)
            record_request_shape(
                payload=payload, upstream=target, status=resp.status_code,
                body=_body.decode("utf-8", errors="replace") if isinstance(_body, bytes) else str(_body or ""),
                prompt_tokens=prompt, completion_tokens=completion)
        except Exception:
            pass


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


# ─── LLM 出口预检（probe）───
#
# 解决什么问题：上游/中转对**带工具的流式请求**返回「正常结束但零内容」时，dsh 会把它判成
# EMPTY_RESPONSE，重试 5 次（约 20 秒）后整轮失败，用户看到的却是一句「Writer 未完成有效
# Plot 提交」——第 16 章那次就是这样。预检把这件事**提前到起 flow / 拿租约之前**，并且
# 由探针自己说清坏在哪一层。
#
# 探针语义（钉死，别再放宽）：
#   · ping（生产预检）：stream + 强制调用 ping 工具；`tool_call_ok` **只在真的出现了合法
#     ping tool_call** 时为真。只有 text/reasoning 不算通过——中转完全可能收下 tools 却
#     忽略它，而 Writer/orchestrator 少了工具调用一步都走不动。
#   · ping 的 max_tokens 用 16~64：这是 ping 不是正文生成，把 dsh 的 256000 原样带进来
#     本身就可能触发中转的参数范围/输出上限异常，等于让预检制造新的假故障。
#   · diagnose（**手动**排障，绝不自动跑）：A=stream+强制工具、B=stream+无工具、
#     C=non-stream+无工具（仅 A、B 都失败才跑 C）。结论按三层给，**不许**用「两个
#     streaming 探针都失败」去说「上游整体异常」（/settings 的测试连接是非流式的，
#     它可能一直好好的）。
_PROBE_PING_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Health probe. Always call this tool.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "fixed string ok"}},
            "required": ["text"],
        },
    },
}
_PROBE_PING_MAX_TOKENS = 512
# 为什么不是 64：推理型模型会把预算先烧在思考上，64 太小 → 返回 finish="length" 且**零 block**，
# 看起来和「上游空回复」一模一样（实测：64 连续三次 length+空，512/2048/8192/256000 全部正常）。
# 预检自己造成的假故障比漏报更糟——那会把用户推向换模型/换中转的错误结论。
_PROBE_BUDGET_MIN = 512          # 低于它的失败一律先当作「预算不足」重试一次更大的预算
# 仅 diagnose(exact_shape=True)：复现 dsh 真实请求形状（core.llm_client.DSH_MAX_TOKENS 同值）
_PROBE_EXACT_MAX_TOKENS = 256_000
_PROBE_OK_TTL = 600.0     # 成功的预检 10 分钟内复用：正常写作基本零额外成本
_PROBE_FAIL_TTL = 20.0    # 失败只短缓存：不放大请求（RATE_LIMIT / QUOTA 时尤其重要）

_PROBE_CACHE: dict = {}


def _probe_post(url: str, payload: dict, timeout: float) -> tuple:
    """默认传输：直接打**本地代理**（与 dsh 同一条出口，key/model 都由代理落定）。

    返回 (status_code, body_text)。测试用 post_fn 注入替换，故判定逻辑与网络解耦。
    """
    resp = requests.post(url, json=payload, timeout=timeout, stream=True,
                         headers={"Content-Type": "application/json",
                                  "Accept": "text/event-stream" if payload.get("stream")
                                            else "application/json",
                                  "Accept-Encoding": "identity"})
    try:
        body = b"".join(resp.iter_content(chunk_size=4096)).decode("utf-8", "replace")
    finally:
        resp.close()
    return resp.status_code, body


def _summarize_completion(body: str, stream: bool) -> dict:
    """把一次响应体归纳成 {text, reasoning, tool_calls, tool_names, finish, done, empty}。

    stream=True 按 SSE 逐行 `data:` 解析；False 按单个 JSON 对象解析。两处都只看
    delta/message 的 content / reasoning_content / tool_calls —— 与 dsh 适配器
    （dsh-llm-deepseek translate()）的「有没有 block」判据同源。
    """
    out = {"text": 0, "reasoning": 0, "tool_calls": 0, "tool_names": [],
           "finish": "", "done": False, "raw_len": len(body or "")}

    def _absorb(node) -> None:
        """吸收一个 message/delta 节点里的 block（与 dsh 适配器同判据）。"""
        if not isinstance(node, dict):
            return
        content = node.get("content")
        if isinstance(content, str) and content:
            out["text"] += len(content)
        reasoning = node.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            out["reasoning"] += len(reasoning)
        for call in node.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            out["tool_calls"] += 1
            name = (call.get("function") or {}).get("name")
            if name:
                out["tool_names"].append(str(name))

    if not stream:
        try:
            obj = json.loads(body or "{}")
        except Exception:
            return out
        for choice in obj.get("choices") or []:
            _absorb(choice.get("message"))
            if isinstance(choice.get("finish_reason"), str):
                out["finish"] = choice["finish_reason"]
        out["done"] = True
        return out

    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            out["done"] = True
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        for choice in chunk.get("choices") or []:
            _absorb(choice.get("delta"))
            if isinstance(choice.get("finish_reason"), str):
                out["finish"] = choice["finish_reason"]
    return out


def _probe_payload(*, stream: bool, with_tools: bool, force_tool: bool,
                   max_tokens: int, system: str = "", user: str = "",
                   tools: list | None = None, role: str = "orchestrator") -> dict:
    """构造一次探测请求。

    `tools` 给定时用调用方给的真实工具集（代表形状探针），否则用内置 ping。
    `force_tool` 只在有工具时才生效——**强制 tool_choice 与自由生成是两种出口能力**：
    上游可能对强制工具调用正常、却对自由生成返回空（那正是本预检要抓的）。
    `role` 指定针对哪一个子代理角色进行出口探测（默认 orchestrator）。
    """
    tool_list = list(tools or ([] if not with_tools else [_PROBE_PING_TOOL]))
    payload = {
        "model": f"novelengine-route:{role}",     # 具体模型由代理按 api.json subagents 路由改写
        "messages": [
            {"role": "system",
             "content": system or "You are a health probe. Follow the instruction exactly."},
            {"role": "user",
             "content": user or ("调用 ping 工具，参数 text 用 ok。" if tool_list else "只回复 ok。")},
        ],
        "stream": bool(stream),
        "max_tokens": int(max_tokens),
        "temperature": 0,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if tool_list:
        payload["tools"] = tool_list
        if force_tool:
            name = (tool_list[0].get("function") or {}).get("name") or "ping"
            payload["tool_choice"] = {"type": "function", "function": {"name": name}}
    return payload


def _probe_once(*, stream: bool, with_tools: bool, force_tool: bool, max_tokens: int,
                timeout: float, post_fn, system: str = "", user: str = "",
                tools: list | None = None, role: str = "orchestrator") -> dict:
    """发一次探测请求并归类。返回 {ok, transport_ok, tool_call_ok, code, shape, counts}。"""
    payload = _probe_payload(stream=stream, with_tools=with_tools, force_tool=force_tool,
                             max_tokens=max_tokens, system=system, user=user, tools=tools, role=role)
    shape = "tools+stream" if (with_tools and stream) else (
        "stream" if stream else "non_stream")
    if with_tools and force_tool:
        shape = "tools+forced"
    result = {"transport_ok": False, "tool_call_ok": False, "code": "", "shape": shape,
              "counts": {}, "status": 0, "empty": True, "role": role}
    try:
        status, body = (post_fn or _probe_post)(token_proxy_base_url() + "/chat/completions",
                                                payload, timeout)
    except Exception as exc:  # noqa: BLE001
        result.update(code="TRANSPORT", error=str(exc)[:200])
        return result
    result["status"] = int(status or 0)
    if status and status >= 400:
        # 4xx/5xx：把上游报文片段带上（含 tool_choice 参数不被支持这类可回落的情形）
        result.update(code=f"HTTP_{status}", sample=(body or "")[:300])
        return result
    counts = _summarize_completion(body or "", payload["stream"])
    result["counts"] = counts
    result["empty"] = (counts["text"] + counts["reasoning"] + counts["tool_calls"]) == 0
    result["transport_ok"] = not result["empty"]
    result["tool_call_ok"] = "ping" in counts["tool_names"]
    if result["empty"]:
        finish = str(counts.get("finish") or "")
        if finish == "length":
            # 零 block + length = **预算被烧完**（推理型模型先思考），不是上游空回复。
            # 必须与「正常结束但无内容」（dsh 的 EMPTY_RESPONSE 判据）分开：
            # 前者是我们的探针参数问题，后者才是出口故障。
            result["code"] = "BUDGET_EXHAUSTED"
            result["inconclusive"] = True
        else:
            result["code"] = "EMPTY_RESPONSE" if finish in ("", "stop") else finish.upper()
    if not result["tool_call_ok"] and with_tools:
        result["code"] = result["code"] or "NO_TOOL_CALL"
    return result


def _tool_choice_unsupported(res: dict) -> bool:
    """中转明确拒绝 tool_choice 参数？只在这种情形才回落到 prompt-only 形状。"""
    text = str(res.get("sample") or "")
    return res.get("status", 0) in (400, 422) and "tool_choice" in text


def _retry_with_bigger_budget(res: dict, *, stream: bool, with_tools: bool, force_tool: bool,
                              max_tokens: int, timeout: float, post_fn, tools=None,
                              system: str = "", user: str = "") -> dict:
    """`finish=length` + 零 block = **预算不足**（探针自身的问题）→ 加大预算重试一次。

    这一步是为了不把「我们的探针给少了预算」误报成「上游出口坏了」。
    """
    if not res.get("inconclusive"):
        return res
    bigger = max(_PROBE_BUDGET_MIN * 8, int(max_tokens) * 8)
    return _probe_once(stream=stream, with_tools=with_tools, force_tool=force_tool,
                       max_tokens=bigger, timeout=timeout, post_fn=post_fn,
                       tools=tools, system=system, user=user)


def _describe_probe(res: dict, layer: str = "", role: str = "orchestrator") -> str:
    """把预检结果渲染成给用户看的中文提示（只讲 LLM 出口，不提编排回退开关）。"""
    info = llm_exit_info(role)
    where = f"（role={role}，model={info.get('model') or '未知'}，upstream={info.get('upstream') or '未知'}）"
    if res.get("inconclusive"):
        return (f"LLM 出口预检未得出结论：{where}返回的是「预算被烧完」（finish=length）而不是空回复，"
                f"这是探针自身预算不足，不代表出口故障。")
    if not res.get("transport_ok"):
        detail = ("连非流式基础请求也失败" if res.get("shape") == "non_stream"
                  else "请求没有得到任何内容（空回复/传输失败）")
        return (f"写作出口不可用：{detail}{where}，错误码 {res.get('code') or '未知'}。"
                f"请在 /settings 检查 API Key、模型名与额度；"
                f"分层复现：probe_llm_exit(mode=\"diagnose\")。")
    if not res.get("tool_call_ok"):
        return (f"写作出口不可用：上游{where}对带工具的流式请求**没有执行工具调用**"
                f"（tool_choice 已强制，错误码 {res.get('code')}）→ 该中转的工具调用能力不可用，"
                f"Writer/编排都跑不动。请在 /settings 换模型或换中转后重试；"
                f"分层复现：probe_llm_exit(mode=\"diagnose\")。")
    if layer:
        return f"LLM 出口分层结论：{layer}{where}。"
    return f"LLM 出口正常{where}。"


def _probe_free_message(res: dict, role: str = "orchestrator") -> str:
    """自由生成段失败时的文案（只讲出口，不提编排开关）。"""
    info = llm_exit_info(role)
    where = f"（role={role}，model={info.get('model') or '未知'}，upstream={info.get('upstream') or '未知'}）"
    return (f"写作出口不可用：上游{where}对**自由生成**请求返回空（同一出口的强制工具调用正常，"
            f"错误码 {res.get('code') or '未知'}，形状 {res.get('shape')}）→ 这是模型/中转侧问题："
            f"写作要把整段正文生成出来，本出口做不到。请在 /settings 换模型或换中转后重试；"
            f"复现：probe_llm_exit(mode=\"contrast\")。")


def probe_llm_exit(mode: str = "ping", *, with_tools: bool = True, timeout: float = 60.0,
                   post_fn=None, use_cache: bool = True, retries: int = 2,
                   exact_shape: bool = False, tools: list | None = None,
                   system: str = "", user: str = "", max_tokens: int | None = None,
                   shape_tag: str = "", role: str = "orchestrator") -> dict:
    """LLM 出口预检：现在能不能靠这个模型/中转写出东西来。

    mode="ping"（生产第一段）：强制调用极小 ping 工具 → 工具调用**路径**可用性。
    只有 `tool_call_ok=True` 才算通过——「有输出」不等于「工具调用可用」。

    mode="free"（生产第二段）：**自由生成**的代表形状探针。可传 `tools` / `system` / `user`
    复现真实请求形状（工具集由调用方从 profile 取，别手写），但**不强制 tool_choice**、用小
    max_tokens。判定看 `transport_ok`（有任一 block）——上游可能对强制工具调用正常、却对
    自由生成整段返回空，而写作恰恰是自由生成。

    mode="diagnose"（手动）：A/B/C 三层，C 仅在 A、B 都失败时才跑；`exact_shape=True`
    用 dsh 真实形状（max_tokens=256000）复现。

    mode="contrast"（手动）：同一句提示下「强制 tool_choice」vs「自由生成」vs「自由文本」的
    最小对照，用来一次说清「出口坏在哪一侧」。只手动跑，不进生产路径。

    成功缓存 10 分钟、失败 20 秒；缓存 key 含 mode/形状标签/角色，两段互不覆盖。
    """
    info = llm_exit_info(role)
    cache_key = (mode, bool(with_tools), bool(exact_shape), int(max_tokens or 0), shape_tag or "",
                 role, info.get("model"), info.get("upstream"))
    now = time.time()
    if use_cache:
        hit = _PROBE_CACHE.get(cache_key)
        if hit and hit[0] > now:
            return dict(hit[1])
    if not max_tokens:
        max_tokens = _PROBE_EXACT_MAX_TOKENS if exact_shape else _PROBE_PING_MAX_TOKENS

    if mode == "contrast":
        # 三组都用够用的预算（64 会让推理型模型烧完预算，把对照变成噪声）
        budget = max(_PROBE_BUDGET_MIN, 1024)
        forced = _probe_once(stream=True, with_tools=True, force_tool=True,
                             max_tokens=budget, timeout=timeout, post_fn=post_fn, role=role)
        free = _probe_once(stream=True, with_tools=False, force_tool=False,
                           max_tokens=budget, timeout=timeout, post_fn=post_fn, role=role)
        prose = _probe_once(stream=True, with_tools=False, force_tool=False,
                            max_tokens=budget, timeout=timeout, post_fn=post_fn,
                            user="用一句中文说明：你接下来会先做什么？", role=role)
        empty = [d for d in (forced, free, prose) if d.get("empty")]
        if any(d.get("inconclusive") for d in (forced, free, prose)):
            verdict = ("对照未得出结论：出现 finish=length（预算被烧完），请加大 max_tokens 再试——"
                       "这属于探针参数问题，不代表出口故障")
        elif forced["tool_call_ok"] and not free["transport_ok"] and not prose["transport_ok"]:
            verdict = "上游只对强制工具调用出内容，**自由生成整段为空** → 写作在本出口不可行"
        elif not empty:
            verdict = "三组都有输出：出口本身正常，失败更可能是间歇的（或由具体请求内容触发）"
        elif not free["transport_ok"] or not prose["transport_ok"]:
            verdict = "自由生成（无工具）为空：出口对自由生成不可用"
        else:
            verdict = "强制工具调用为空：出口的工具调用路径异常"
        out = {"ok": prose["transport_ok"], "verdict": verdict,
               "forced": forced, "free": free, "prose": prose,
               "model": info.get("model"), "upstream": info.get("upstream"), "role": role}
        out["message"] = verdict
        return out

    if mode == "diagnose":
        a = _probe_once(stream=True, with_tools=True, force_tool=True, max_tokens=max_tokens,
                        timeout=timeout, post_fn=post_fn)
        b = {}
        c = {}
        layer = ""
        if not a["tool_call_ok"]:
            b = _probe_once(stream=True, with_tools=False, force_tool=False,
                            max_tokens=max_tokens, timeout=timeout, post_fn=post_fn)
            if not b["transport_ok"]:
                c = _probe_once(stream=False, with_tools=False, force_tool=False,
                                max_tokens=max_tokens, timeout=timeout, post_fn=post_fn)
                layer = "流式路径异常（streaming 空、非流式正常）" if c["transport_ok"] else (
                    "基础连接/模型/上游异常（含非流式在内全部失败）")
            else:
                layer = "工具调用路径异常（无工具流式正常、带工具不执行调用）"
        out = {"ok": a["tool_call_ok"], "layer": layer, "a": a, "b": b, "c": c,
               "model": info.get("model"), "upstream": info.get("upstream"),
               "tool_call_ok": a["tool_call_ok"], "transport_ok": a["transport_ok"],
               "code": a["code"], "shape": a["shape"]}
        out["message"] = _describe_probe(a, layer)
        return out

    if mode == "free":
        # 自由生成的代表形状：**不强制 tool_choice**（写作就是自由生成），判定看有没有 block
        res = _probe_once(stream=True, with_tools=bool(tools), force_tool=False,
                          max_tokens=max_tokens, timeout=timeout, post_fn=post_fn,
                          tools=tools, system=system, user=user, role=role)
        res = _retry_with_bigger_budget(res, stream=True, with_tools=bool(tools), force_tool=False,
                                        max_tokens=max_tokens, timeout=timeout, post_fn=post_fn,
                                        tools=tools, system=system, user=user)
        attempt = 0
        while (not res["transport_ok"]) and not res.get("inconclusive") \
                and attempt < max(0, int(retries)):
            attempt += 1
            time.sleep(0.6 * attempt)
            res = _probe_once(stream=True, with_tools=bool(tools), force_tool=False,
                              max_tokens=max_tokens, timeout=timeout, post_fn=post_fn,
                              tools=tools, system=system, user=user, role=role)
        inconclusive = bool(res.get("inconclusive"))
        res.update({"ok": bool(res["transport_ok"]) or inconclusive,
                    "model": info.get("model"), "upstream": info.get("upstream"),
                    "requests": attempt + 1, "free_generation": True, "role": role,
                    "warning": ("预检未得出结论（预算被烧完，finish=length）——按通过处理，"
                                "不改写写作流程" if inconclusive else "")})
        res["message"] = "" if res["ok"] else _probe_free_message(res, role=role)
        ttl = _PROBE_OK_TTL if res["ok"] else _PROBE_FAIL_TTL
        if use_cache:
            _PROBE_CACHE[cache_key] = (now + ttl, dict(res))
        return res

    res = _probe_once(stream=True, with_tools=with_tools, force_tool=with_tools,
                      max_tokens=max_tokens, timeout=timeout, post_fn=post_fn, role=role)
    res = _retry_with_bigger_budget(res, stream=True, with_tools=with_tools,
                                    force_tool=with_tools, max_tokens=max_tokens,
                                    timeout=timeout, post_fn=post_fn)
    if with_tools and _tool_choice_unsupported(res):
        # 中转不支持 tool_choice：退一步用 prompt 指令（形状记为 prompt_only 便于区分）
        res = _probe_once(stream=True, with_tools=True, force_tool=False,
                          max_tokens=max_tokens, timeout=timeout, post_fn=post_fn, role=role)
        res["shape"] = "prompt_only"
    # 传输层失败（HTTP 错误 / 零 block）重试有限次；语义失败（有输出但没调工具）不重试
    attempt = 0
    while (not res["transport_ok"]) and not res.get("inconclusive") \
            and attempt < max(0, int(retries)):
        attempt += 1
        time.sleep(0.6 * attempt)
        res = _probe_once(stream=True, with_tools=with_tools, force_tool=with_tools,
                          max_tokens=max_tokens, timeout=timeout, post_fn=post_fn, role=role)
    inconclusive = bool(res.get("inconclusive"))
    res.update({"ok": bool(res["tool_call_ok"]) or inconclusive, "model": info.get("model"),
                "upstream": info.get("upstream"), "requests": attempt + 1, "role": role,
                "warning": ("预检未得出结论（预算被烧完，finish=length）——按通过处理"
                            if inconclusive else "")})
    res["message"] = _describe_probe(res, role=role) if not res["ok"] else ""
    ttl = _PROBE_OK_TTL if res["ok"] else _PROBE_FAIL_TTL
    if use_cache:
        _PROBE_CACHE[cache_key] = (now + ttl, dict(res))
    return res


def clear_probe_cache() -> None:
    """清预检缓存（设置页保存后调用；key 本身含 model/upstream，正常无需清）。"""
    _PROBE_CACHE.clear()


# ─── 请求形状留痕（只计数，绝不记正文）───
#
# 为什么需要：2026-09-19 那次「root run 连续 6 次空回复」，只能靠 token 反推（6 次调用、
# 合计 110730 prompt、completion 0），还要翻 git 判断哪条路径、请求多大、带几个工具。
# 有这张表，`/health` 一眼就能看出「连续 N 次零输出、每次多大、带几个工具」。
_REQ_SHAPES: "deque" = deque(maxlen=30)


def record_request_shape(*, payload: dict, upstream: str, status: int, body: str,
                         prompt_tokens: int = 0, completion_tokens: int = 0) -> dict:
    """记一条请求形状（**只计数**，不保存 prompt/响应正文的任何片段）。"""
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    blocks = _summarize_completion(body or "", bool(payload.get("stream")))
    rec = {
        "at": time.time(),
        "model": str(payload.get("model") or ""),
        "upstream": _origin_of(upstream),
        "stream": bool(payload.get("stream")),
        "tools": len(tools),
        "tool_choice": bool(payload.get("tool_choice")),
        "messages": len(messages),
        "msgs_chars": sum(len(str(m.get("content") or "")) for m in messages
                          if isinstance(m, dict)),
        "max_tokens": int(payload.get("max_tokens") or 0),
        "status": int(status or 0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "blocks": {"text": blocks["text"], "reasoning": blocks["reasoning"],
                   "tool_calls": blocks["tool_calls"]},
        "finish": blocks["finish"],
        "empty": (blocks["text"] + blocks["reasoning"] + blocks["tool_calls"]) == 0,
    }
    _REQ_SHAPES.append(rec)
    return rec


def recent_request_shapes(limit: int = 20) -> list:
    """最近 N 条请求形状（最新在最后）——`/health` 用，不含任何正文。"""
    items = list(_REQ_SHAPES)
    return items[-max(1, int(limit)):]


def clear_request_shapes() -> None:
    _REQ_SHAPES.clear()
