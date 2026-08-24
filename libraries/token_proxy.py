"""本地 LLM API 代理 + token 流量实时检测器（show-me-the-story 模式）。

拦截 dsh Node 与 Python LLMClient 的 DeepSeek 调用（api.json base_url 指向本代理，
real_base_url 为真实 DeepSeek），转发时注入 include_usage:true，从每个响应解析
usage 实时累计，暴露 /token-usage 供前端轮询。无 usage 时按 ~1.5 tokens/字估算兜底。

用法：from libraries.token_proxy import ensure_proxy, get_token_usage, clear_token_usage
"""
import codecs
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_PORT = int(os.environ.get("NE_TOKEN_PROXY_PORT", "58082"))


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
            total = self.prompt + self.completion + pend_p + pend_c
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


def _api_config():
    """真实 DeepSeek URL + api_key（real_base_url 优先，兜底旧 base_url）。"""
    try:
        with open(os.path.join(_ROOT, "api.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    real = (cfg.get("real_base_url") or "").strip() or "https://api.deepseek.com"
    key = (cfg.get("api_key") or "").strip()
    return real, key


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
            self._send_json({"ok": True, **USAGE.snapshot()})
        elif self.path.rstrip("/") == "/token-usage/clear":
            USAGE.clear()
            self._send_json({"ok": True})
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
        # 注入 include_usage（流式需 stream_options），让响应带 usage
        if payload.get("stream"):
            payload["stream_options"] = {"include_usage": True}
        real_url, api_key = _api_config()
        target = real_url.rstrip("/") + self.path
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        }
        # 优先透传调用方自带的 Authorization（dsh 用 ~/.dsh/.env 的 key），
        # 仅当缺失时回退 api.json 的 key——避免 dsh 流量被静默改记到平台 key 名下。
        incoming_auth = self.headers.get("Authorization")
        if incoming_auth:
            headers["Authorization"] = incoming_auth
        elif api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            resp = requests.post(target, json=payload, headers=headers, stream=True, timeout=600)
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
                buf = b""
                for chunk in resp.iter_content(chunk_size=1024):
                    if not chunk:
                        continue
                    self.wfile.write(chunk)      # 实时透传（保持 SSE 原始格式，不破坏 dsh 流解析）
                    self.wfile.flush()
                    buf += chunk
                    char_count += len(decoder.decode(chunk))
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


def ensure_proxy():
    """确保代理在跑（幂等；dsh_bridge / LLMClient 惰性拉起 daemon 线程）。"""
    if _started.is_set():
        return True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), _Handler)
    except Exception as e:
        return False  # 端口占用等：忽略，不影响主链路
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _started.set()
    return True


def get_token_usage() -> dict:
    return USAGE.snapshot()


def clear_token_usage() -> None:
    USAGE.clear()
