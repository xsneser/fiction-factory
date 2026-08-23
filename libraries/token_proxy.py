"""本地 LLM API 代理 + token 流量实时检测器（show-me-the-story 模式）。

拦截 dsh Node 与 Python LLMClient 的 DeepSeek 调用（api.json base_url 指向本代理，
real_base_url 为真实 DeepSeek），转发时注入 include_usage:true，从每个响应解析
usage 实时累计，暴露 /token-usage 供前端轮询。无 usage 时按 ~1.5 tokens/字估算兜底。

用法：from libraries.token_proxy import ensure_proxy, get_token_usage, clear_token_usage
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_PORT = int(os.environ.get("NE_TOKEN_PROXY_PORT", "58082"))


class _UsageAccumulator:
    def __init__(self):
        self.lock = threading.Lock()
        self.prompt = 0
        self.completion = 0
        self.calls = 0

    def accumulate(self, prompt, completion):
        with self.lock:
            self.prompt += int(prompt or 0)
            self.completion += int(completion or 0)
            self.calls += 1

    def snapshot(self):
        with self.lock:
            total = self.prompt + self.completion
            return {"prompt": self.prompt, "completion": self.completion,
                    "calls": self.calls, "total": total}

    def clear(self):
        with self.lock:
            self.prompt = 0
            self.completion = 0
            self.calls = 0


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
        if api_key:
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
        try:
            if payload.get("stream"):
                buf = b""
                for chunk in resp.iter_content(chunk_size=1024):
                    if not chunk:
                        continue
                    self.wfile.write(chunk)      # 实时透传（保持 SSE 原始格式，不破坏 dsh 流解析）
                    self.wfile.flush()
                    buf += chunk
                chars = len(buf)
                prompt, completion = _parse_usage_objs(buf.decode("utf-8", errors="replace"))
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
        except Exception:
            pass
        # 无 usage → 兜底估算（~1.5 tokens/字，prompt 占 1/3、completion 占 2/3）
        if not prompt and not completion and chars:
            est = _estimate_tokens(chars)
            prompt, completion = est // 3, est - est // 3
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
