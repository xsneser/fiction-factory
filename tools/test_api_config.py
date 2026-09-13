#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 配置链路测试（离线，不发任何网络请求）。

覆盖三件事：
  1. core/api_config.py —— api.json 唯一加载器（全字段映射、类型归一、缺文件）
  2. core/llm_client.py —— 全站统一输出预算 + 流式状态码检查
  3. libraries/token_proxy.py —— 代理作为配置执行点（上游解析、自环、路径归一、
     模型改写、key 权威、TLS 透传）

用法: python tools/test_api_config.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILURES = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


TMP = tempfile.mkdtemp(prefix="ne_api_cfg_")


def write_cfg(obj) -> str:
    path = os.path.join(TMP, f"api_{len(os.listdir(TMP))}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return path


# ══════════════════════════════════════════════
print("\n═══ 1. core/api_config.py：唯一加载器 ═══")
from core.api_config import (                                        # noqa: E402
    api_config_from_mapping, api_config_path, is_api_configured,
    load_api_config, load_api_config_raw, redact_secret,
)
from core.models import APIConfig                                   # noqa: E402

full = write_cfg({
    "api_key": "sk-real-key", "base_url": "https://relay.example.com/",
    "url_strict": True, "model": "gemini-3.8-flash", "max_tokens": 8192,
    "http_timeout_seconds": 120, "context_budget_tokens": 500000,
    "verify_ssl": False, "real_base_url": "https://api.deepseek.com",
})
cfg = load_api_config(full)
check("全字段映射", (cfg.api_key == "sk-real-key" and cfg.base_url == "https://relay.example.com"
                and cfg.url_strict is True and cfg.model == "gemini-3.8-flash"
                and cfg.max_tokens == 8192 and cfg.http_timeout_seconds == 120
                and cfg.context_budget_tokens == 500000 and cfg.verify_ssl is False
                and cfg.real_base_url == "https://api.deepseek.com"), str(cfg))
check("base_url 去尾斜杠", cfg.base_url == "https://relay.example.com", cfg.base_url)

# 字符串布尔：不能用 bool() 解析（"false" 会被判成 True）
s = api_config_from_mapping({"verify_ssl": "false", "url_strict": "true"})
check("布尔字符串解析", s.verify_ssl is False and s.url_strict is True,
      f"verify_ssl={s.verify_ssl} url_strict={s.url_strict}")
check("布尔缺省", api_config_from_mapping({}).verify_ssl is True)

# 缺文件 / 空配置
check("缺文件 → None", load_api_config(os.path.join(TMP, "__nope__.json")) is None)
check("缺文件 raw → {}", load_api_config_raw(os.path.join(TMP, "__nope__.json")) == {})

# 数值归一
n = api_config_from_mapping({"max_tokens": -5, "http_timeout_seconds": 99999,
                             "context_budget_tokens": 1})
check("max_tokens 负数 → 0", n.max_tokens == 0, str(n.max_tokens))
check("超时/预算范围裁剪", n.http_timeout_seconds == 1800 and n.context_budget_tokens == 8000,
      f"{n.http_timeout_seconds}/{n.context_budget_tokens}")
check("空 model 回退默认", api_config_from_mapping({}).model == "deepseek-chat")

# is_api_configured
check("占位 key 不算已配置",
      is_api_configured(api_config_from_mapping({"api_key": "sk-your-api-key-here",
                                                 "base_url": "https://x.com"})) is False)
check("空 key 不算已配置", is_api_configured(api_config_from_mapping({})) is False)
check("None 不算已配置", is_api_configured(None) is False)
check("真 key 算已配置", is_api_configured(api_config_from_mapping(
    {"api_key": "sk-real", "base_url": "https://x.com"})) is True)

# 路径与 cwd 无关
os.chdir(tempfile.gettempdir())
check("路径固定仓库根", api_config_path().name == "api.json"
      and str(api_config_path()).endswith(os.path.join("NovelEngine", "api.json")), str(api_config_path()))
check("掩码不回显明文", redact_secret("sk-1234567890") == "sk-12345****", redact_secret("sk-1234567890"))


# ══════════════════════════════════════════════
print("\n═══ 2. core/llm_client.py：统一预算 + 状态码 ═══")
import core.llm_client as llm_mod                                   # noqa: E402
from core.llm_client import DSH_MAX_TOKENS, LLMClient, _http_post   # noqa: E402

# 测试进程内不要把本地代理真拉起来
import libraries.token_proxy as tp_mod                              # noqa: E402
tp_mod.ensure_proxy = lambda *a, **k: True

captured = {}


def fake_post(url, headers, json_data, timeout=300, stream=False, verify=True):
    captured.update(url=url, headers=headers, body=json_data, timeout=timeout,
                    stream=stream, verify=verify)
    if stream:
        return _FakeStream([], status=200)
    return json.dumps({"choices": [{"message": {"content": "ok"}}]})


class _FakeStream:
    def __init__(self, lines, status=200):
        self._chunks = [ln.encode("utf-8") for ln in lines]
        self.status = status
        self.released = False

    def read_chunked(self):
        for chunk in self._chunks:
            yield chunk

    def release_conn(self):
        self.released = True


llm_mod._http_post = fake_post

test_cfg = APIConfig(api_key="sk-k", base_url="https://relay.example.com",
                     model="gemini-3.8-flash", verify_ssl=False,
                     http_timeout_seconds=77, url_strict=False)
client = LLMClient(test_cfg)

client.call("sys", "user")
check("call 预算 = DSH_MAX_TOKENS", captured["body"]["max_tokens"] == DSH_MAX_TOKENS,
      str(captured["body"]["max_tokens"]))
check("DSH_MAX_TOKENS = 256000（对齐 dsh）", DSH_MAX_TOKENS == 256_000, str(DSH_MAX_TOKENS))
check("URL 归一补 /v1/chat/completions",
      captured["url"] == "https://relay.example.com/v1/chat/completions", captured["url"])
check("model / Authorization 来自配置",
      captured["body"]["model"] == "gemini-3.8-flash"
      and captured["headers"]["Authorization"] == "Bearer sk-k")
check("verify_ssl / 超时透传", captured["verify"] is False and captured["timeout"] == 77)

# 逃生阀：api.json 显式填正数时覆盖
esc = LLMClient(APIConfig(api_key="k", base_url="https://x.com", model="m", max_tokens=8192))
esc.call("s", "u")
check("max_tokens 逃生阀生效", captured["body"]["max_tokens"] == 8192,
      str(captured["body"]["max_tokens"]))

# 流式同样是一个值，且不再区分功能（stream_deltas 是生成器，必须消费才会发请求）
list(client.stream_deltas("s", "u"))
check("stream_deltas 预算 = DSH_MAX_TOKENS", captured["body"]["max_tokens"] == DSH_MAX_TOKENS,
      str(captured["body"]["max_tokens"]))
check("stream 标记", captured["body"].get("stream") is True)

# 空 choices 的 usage-only 分片被跳过，正文照常输出
llm_mod._http_post = lambda *a, **k: _FakeStream([
    'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n',
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n',
    'data: [DONE]\n',
], status=200)
got = list(LLMClient(test_cfg).stream_deltas("s", "u"))
check("空 choices 分片被跳过且不中断流", got == [("content", "你好")], str(got))

# 流式非 200 必须抛错，而不是被 SSE 解析器静默吃掉
class _FakePool:
    def request(self, *a, **k):
        return _FakeResp()


class _FakeResp:
    status = 401
    data = b'{"error":{"message":"invalid api key"}}'

    def release_conn(self):
        pass


llm_mod._POOL = _FakePool()
try:
    _http_post("https://x.com/v1/chat/completions", {}, {}, stream=True)
    check("流式非 200 抛 IOError", False, "未抛错")
except IOError as e:
    check("流式非 200 抛 IOError", "401" in str(e), str(e))

# 同步路径空 choices → 带响应片段的 IOError（保留的工作区补丁行为）
llm_mod._http_post = lambda *a, **k: json.dumps({"choices": [], "usage": {}})
try:
    LLMClient(test_cfg).call("s", "u")
    check("同步空 choices 抛 IOError", False, "未抛错")
except IOError as e:
    check("同步空 choices 抛 IOError", "choices" in str(e), str(e))


# ══════════════════════════════════════════════
print("\n═══ 3. token_proxy：配置执行点 ═══")
from libraries.token_proxy import (                                 # noqa: E402
    PROXY_PORT, is_token_proxy_url, normalize_base_url,
    resolve_upstream, token_proxy_base_url,
)

check("代理地址", token_proxy_base_url() == f"http://127.0.0.1:{PROXY_PORT}",
      token_proxy_base_url())
for url, expect in [
    ("http://127.0.0.1:58082", True), ("http://localhost:58082/v1", True),
    ("http://[::1]:58082/chat/completions", True), ("http://localhost:58083", False),
    ("http://127.0.0.1:58080", False), ("https://api.claudecode.net.cn", False),
    ("", False),
]:
    check(f"自环判定 {url or '(空)'}", is_token_proxy_url(url) is expect,
          str(is_token_proxy_url(url)))

# 上游：设置页 base_url 优先；只有它指向代理自身时才回退 real_base_url
check("base_url 优先于 stale real_base_url",
      resolve_upstream(APIConfig(base_url="https://relay.example.com",
                                 real_base_url="https://api.deepseek.com"))
      == "https://relay.example.com")
check("base_url 指向代理 → 回退 real_base_url",
      resolve_upstream(APIConfig(base_url=f"http://127.0.0.1:{PROXY_PORT}",
                                 real_base_url="https://api.deepseek.com"))
      == "https://api.deepseek.com")
check("双自环 → 空（fail closed）",
      resolve_upstream(APIConfig(base_url=f"http://127.0.0.1:{PROXY_PORT}",
                                 real_base_url=f"http://localhost:{PROXY_PORT}")) == "")
check("无配置 → 空（不静默兜底 deepseek）", resolve_upstream(APIConfig()) == "")
check("None → 空", resolve_upstream(None) == "")

# 路径归一：代理必须和 Python 侧拼出同一个 endpoint
for base, strict, want in [
    ("https://host", False, "https://host/v1/chat/completions"),
    ("https://host", True, "https://host/chat/completions"),
    ("https://host/v1", False, "https://host/v1/chat/completions"),
    ("https://host/v1/chat/completions", False, "https://host/v1/chat/completions"),
    ("https://host/chat/completions", False, "https://host/chat/completions"),
]:
    got_url = normalize_base_url(base, strict)
    check(f"endpoint {base} strict={strict}", got_url == want, got_url)


# ══════════════════════════════════════════════
print("\n" + "=" * 56)
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
