"""
LLM 客户端 — 对齐 show-me-the-story llm/api.go
使用 requests 库（Windows 兼容性最佳）
"""
import json
import time
import re
import ssl
from typing import Callable, Optional
from urllib3 import PoolManager
from urllib3.util import create_urllib3_context


def _make_ssl_context(verify: bool = True):
    """创建 SSL 上下文。

    verify=True（默认）：启用证书/主机名校验，保证 LLM 流量不可被中间人劫持。
    verify=False：关闭校验（仅用于兼容旧证书环境，如特定 Windows/Python 组合），
    会同时降低 TLS 密码套件安全级别。
    """
    ctx = create_urllib3_context()
    if verify:
        return ctx
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    return ctx


def _http_post(url: str, headers: dict, json_data: dict, timeout: int = 300,
               stream: bool = False, verify: bool = True):
    """使用 urllib3 做 HTTP POST（绕过 requests/httpx 的SSL问题）"""
    import urllib3
    if not verify:
        urllib3.disable_warnings()
    http = PoolManager(
        timeout=urllib3.Timeout(connect=10, read=timeout),
        ssl_context=_make_ssl_context(verify),
        retries=urllib3.Retry(3, backoff_factor=0.5),
    )
    body = json.dumps(json_data).encode('utf-8')
    headers = {**headers, 'Content-Type': 'application/json'}
    if stream:
        return http.request('POST', url, body=body, headers=headers, preload_content=False)
    resp = http.request('POST', url, body=body, headers=headers)
    if resp.status != 200:
        raise IOError(f"HTTP {resp.status}: {resp.data.decode('utf-8', errors='replace')[:500]}")
    return resp.data.decode('utf-8')


def normalize_base_url(url: str, strict: bool = False) -> str:
    """标准化 API 地址 — 完全对齐 Go resolveChatCompletionsURL"""
    url = url.strip().rstrip("/")
    if not url:
        return ""
    # 已包含 /chat/completions，直接返回
    if url.endswith("/chat/completions"):
        return url
    # strict 模式：只补 /chat/completions
    if strict:
        return url + "/chat/completions"
    # 检查 URL 是否已包含版本段 (v1/v2/...)
    import re as _re
    if _re.search(r'/v\d+/', url) or _re.search(r'/v\d+$', url):
        return url + "/chat/completions"
    # 默认：补 /v1/chat/completions
    return url + "/v1/chat/completions"


def extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON（处理 markdown 包裹、多余文本）"""
    text = text.strip()
    # 去掉 markdown code block
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 找第一个 { 到最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return text[start:end+1]
    return text


class LLMClient:
    def __init__(self, api_config):
        self.cfg = api_config

    @property
    def api_url(self):
        return normalize_base_url(self.cfg.base_url, self.cfg.url_strict)

    def call(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.7, max_tokens: int = 4096) -> str:
        """同步调用 LLM"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens or 4096}

        last_err = None
        for attempt in range(3):
            try:
                data = _http_post(self.api_url, headers, body, self.cfg.http_timeout_seconds,
                                  verify=self.cfg.verify_ssl)
                return json.loads(data)["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise last_err

    def call_stream(self, system_prompt: str, user_prompt: str,
                    on_chunk: Callable[[str], None],
                    temperature: float = 0.7,
                    max_tokens: int = 4096) -> str:
        """流式调用 LLM"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens or 4096,
                "stream": True}

        resp = _http_post(self.api_url, headers, body, self.cfg.http_timeout_seconds,
                          stream=True, verify=self.cfg.verify_ssl)
        full_text = []
        for line in resp.read_chunked():
            line = line.decode('utf-8', errors='replace')
            if line.startswith("data: "):
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_text.append(content)
                        on_chunk(content)
                except (json.JSONDecodeError, KeyError):
                    continue
        resp.release_conn()
        return "".join(full_text)

    def call_messages(self, messages: list[dict]) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages}
        data = _http_post(self.api_url, headers, body, self.cfg.http_timeout_seconds,
                          verify=self.cfg.verify_ssl)
        return json.loads(data)["choices"][0]["message"]["content"]

    def test_connection(self) -> dict:
        try:
            result = self.call("", "Hi", max_tokens=50)
            return {"success": True, "sample": result[:100]}
        except Exception as e:
            return {"success": False, "error": str(e)}


def render_prompt(template: str, variables: dict) -> str:
    """安全渲染模板：只替换提供了的变量，丢失的保持原样"""
    import re
    def replacer(m):
        key = m.group(1)
        return str(variables.get(key, m.group(0)))
    return re.sub(r'\{(\w+)\}', replacer, template)


def is_fatal_error(err: Exception) -> bool:
    msg = str(err).lower()
    if "401" in msg or "403" in msg or "404" in msg:
        return True
    if "connection refused" in msg or "no such host" in msg:
        return True
    return False

def estimate_tokens(text: str) -> int:
    """估算token数：每字符约1.5个token"""
    return int(len(text) * 1.5)

def estimate_tokens_from_runes(runes: int) -> int:
    return int(runes * 1.5)
