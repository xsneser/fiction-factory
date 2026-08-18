"""验证 deepseek-v4-flash 是否支持原生 function calling（tool_calls）。

用法：
    python tools/smoke_llm_tools.py
    python tools/smoke_llm_tools.py --no-call   # 只构造，不发请求（离线检查）

Phase 0 预检：若打印 tool_calls 为空或请求报错，说明 flash 不兼容原生 function
calling，agent_loop 需走 JSON 降级协议。
"""
import sys
import os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm_client import LLMClient
from core.models import APIConfig
from core.json_store import read_json

LIST_BOOKS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_books",
        "description": "列出书库全部书籍的摘要",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def main():
    cfg = read_json("api.json", {})
    if not cfg.get("api_key"):
        print("[FAIL] api.json 未配置 api_key")
        return 1
    client = LLMClient(APIConfig(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-v4-flash"),
        http_timeout_seconds=cfg.get("http_timeout_seconds", 300),
        verify_ssl=cfg.get("verify_ssl", True),
    ))
    messages = [
        {"role": "system", "content": "你是 NovelEngine 的 Agent，通过工具操作创作引擎。"},
        {"role": "user", "content": "请调用 list_books 列出书库里的书。"},
    ]
    print(f"model={cfg.get('model')} url={client.api_url}")
    print("发送 messages + tools=[list_books]，等待响应...")
    msg = client.call_tools(messages, [LIST_BOOKS_TOOL], temperature=0.2, max_tokens=8192)
    tcs = msg.get("tool_calls") or []
    print("返回 message:")
    print("  content:", (msg.get("content") or "")[:200] or "(空)")
    print("  tool_calls:", len(tcs))
    for tc in tcs:
        print("    id:", tc.get("id"))
        print("    function:", tc.get("function"))
    if tcs:
        print("[OK] flash 返回 tool_calls，原生 function calling 可用")
        return 0
    print("[FALLBACK] flash 未返回 tool_calls → agent_loop 需走 JSON 降级协议")
    return 2


if __name__ == "__main__":
    sys.exit(main())
