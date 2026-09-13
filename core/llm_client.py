"""
LLM 客户端 — 对齐 show-me-the-story llm/api.go
使用 requests 库（Windows 兼容性最佳）
"""
import json
import time
import re
import ssl
import urllib3
from urllib3 import PoolManager
from urllib3.util import create_urllib3_context

# 全站统一的输出预算（max_tokens），**不按功能区分**。
# 取值对齐 dsh 侧 DeepSeek adapter 的默认值：DEFAULT_MAX_TOKENS = 256e3
# （vendor/dsh-ne/node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js:786-794），
# dsh-llm 会在调用方未指定时把它物化进每次调用配置（dsh-llm/lib/index.js:1356），
# adapter 再序列化成线上的 max_tokens（dsh-llm-deepseek/lib/index.js:229）。
# 推理型模型（deepseek-v4-flash / gemini-3.8-flash 等）会先烧数百上千个思考 token，
# 预算给小了正文会被截断甚至只剩空串 —— 与 dsh 保持一致最省心。
DSH_MAX_TOKENS = 256_000


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


# 进程级复用连接池（保留 HTTP keep-alive / TLS 会话）。
# verify=True（默认安全）走共享池；verify=False（仅旧证书环境）按需新建。
_SSL_CTX_VERIFY = _make_ssl_context(True)
_SSL_CTX_INSECURE = _make_ssl_context(False)
_POOL = PoolManager(
    ssl_context=_SSL_CTX_VERIFY,
    retries=urllib3.Retry(3, backoff_factor=0.5),
)


def _http_post(url: str, headers: dict, json_data: dict, timeout: int = 300,
               stream: bool = False, verify: bool = True):
    """使用 urllib3 做 HTTP POST（绕过 requests/httpx 的SSL问题）"""
    http = _POOL if verify else PoolManager(
        ssl_context=_SSL_CTX_INSECURE,
        retries=urllib3.Retry(3, backoff_factor=0.5),
    )
    if not verify:
        urllib3.disable_warnings()
    req_timeout = urllib3.Timeout(connect=10, read=timeout)
    body = json.dumps(json_data).encode('utf-8')
    headers = {**headers, 'Content-Type': 'application/json'}
    if stream:
        resp = http.request('POST', url, body=body, headers=headers,
                            timeout=req_timeout, preload_content=False)
        # 流式分支同样要查状态码：否则 401/400 的错误 JSON 会被 SSE 解析器
        # 当成"没有 delta 的行"整条静默吃掉，最终表现为"模型返回空正文"。
        if resp.status != 200:
            detail = resp.data.decode('utf-8', errors='replace')[:500]
            resp.release_conn()
            raise IOError(f"HTTP {resp.status}: {detail}")
        return resp
    resp = http.request('POST', url, body=body, headers=headers, timeout=req_timeout)
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

    def _max_tokens(self) -> int:
        """输出预算：全站同一个值，不按功能区分。

        默认取 `DSH_MAX_TOKENS`（与 dsh 侧一致）。`api.json.max_tokens` 只在
        显式填了正数时覆盖 —— 那是给"上游拒绝大 max_tokens"准备的逃生阀，
        不在设置界面暴露。
        """
        try:
            configured = int(self.cfg.max_tokens or 0)
        except (TypeError, ValueError):
            configured = 0
        return configured if configured > 0 else DSH_MAX_TOKENS

    def call(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.7) -> str:
        """同步调用 LLM（直连 cfg.base_url；dsh 侧的流量才经本地代理计数）"""
        # 顺带确保本地 token 代理在跑（幂等；只在 Web 进程没拉起它时兜底）。
        # 注意：本方法**不**经代理转发 —— 代理只服务 dsh（见 libraries/token_proxy.py）。
        from libraries.token_proxy import ensure_proxy
        ensure_proxy()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages,
                "temperature": temperature, "max_tokens": self._max_tokens()}

        last_err = None
        for attempt in range(3):
            try:
                data = _http_post(self.api_url, headers, body, self.cfg.http_timeout_seconds,
                                  verify=self.cfg.verify_ssl)
                # 少数中转（如 api.claudecode.net.cn）可能返回 choices 为空
                # 的 usage-only 响应；旧写法直接下标会抛 IndexError。
                choices = json.loads(data).get("choices") or []
                if not choices:
                    raise IOError(f"响应缺少 choices: {data[:200]}")
                return choices[0]["message"]["content"]
            except Exception as e:
                last_err = e
                # 401/403/404/连接类错误重试无意义，立即抛出
                if is_fatal_error(e):
                    raise
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise last_err

    def stream_deltas(self, system_prompt: str, user_prompt: str,
                      temperature: float = 0.7):
        """流式调用 LLM，逐个 yield (delta_key, text)。

        delta_key ∈ {"reasoning", "content"}：
          - reasoning：模型内部思考过程（DeepSeek 的 reasoning_content，链式思考）
          - content：最终输出正文

        供需要把 AI 思考过程实时转发给 UI 的调用方使用：
            for kind, text in client.stream_deltas(sys, user):
                if kind == "reasoning": show_thinking(text)
                else: collect_answer(text)

        read_chunked() 返回的是 HTTP chunked 编码的任意大小块，不是按行，
        因此这里做换行缓冲，兼容"一个块含多条 SSE"与"一条 SSE 被切成多块"。
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages,
                "temperature": temperature, "max_tokens": self._max_tokens(),
                "stream": True}

        resp = _http_post(self.api_url, headers, body, self.cfg.http_timeout_seconds,
                          stream=True, verify=self.cfg.verify_ssl)
        buf = ""
        try:
            for chunk in resp.read_chunked():
                buf += chunk.decode('utf-8', errors='replace')
                while "\n" in buf:
                    sse_line, buf = buf.split("\n", 1)
                    sse_line = sse_line.strip()
                    if not sse_line or not sse_line.startswith("data:"):
                        continue
                    data = sse_line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        event = json.loads(data)
                        # 中转常在 [DONE] 前补发一个 choices 为空的 usage chunk
                        # （api.claudecode.net.cn 就会），空 choices 直接跳过，
                        # 否则 event["choices"][0] 抛 IndexError 打断整条流。
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}) or {}
                        if delta.get("reasoning_content"):
                            yield ("reasoning", delta["reasoning_content"])
                        if delta.get("content"):
                            yield ("content", delta["content"])
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
        finally:
            resp.release_conn()

    def test_connection(self) -> dict:
        try:
            # 预算与真实生成同一套（DSH_MAX_TOKENS）——推理型模型的思考 token
            # 会吃掉预算，给少了 content 为空，会被误报成"连接失败"。
            result = self.call("", "Hi")
            return {"success": True, "sample": (result or "")[:100]}
        except Exception as e:
            return {"success": False, "error": str(e)}


def is_fatal_error(err: Exception) -> bool:
    """判定错误是否属于「重试无意义」的致命错误（认证/权限/不存在/连接类）"""
    msg = str(err).lower()
    if "401" in msg or "403" in msg or "404" in msg:
        return True
    if "connection refused" in msg or "no such host" in msg:
        return True
    return False
