#!/usr/bin/env python3
"""MCP 协议验收 — 脚本化 MCP 客户端驱动 NovelEngine 最小闭环（零/低成本，不调 LLM 写作）。

用 mcp.client.stdio 连接 `python mcp_server.py`，走完整协议：
  initialize 握手 → tools/list（断言 35 个 mcp 面工具）→ tools/call 真实往返：
  create_book → save_basic_info → generate_outlines(rule) → confirm_outlines → fill_gags
  → get_book_detail → navigate（断言意图队列）→ delete_book 清理。

断言：
  1) MCP 面工具数 = 35；navigate 在列；canvas_command 不在列；query_plots/diagnose_retention 在列
  2) create→rule 大纲→get_book_detail 全往返成功
  3) navigate 写入 storage/nav_intent.json
  4) storage/tool_log.jsonl 出现 source="mcp" 调用条目
  5) delete_book 未确认时拒绝、确认后清理成功

用法：python tools/mcp_smoke.py
"""
import asyncio
import json
import os
import sys

# Windows 控制台默认 GBK，print emoji/中文会崩，强制走 UTF-8（与 test_all.py 同款）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)   # 让 mcp_server 子进程的 books/、storage/ 相对路径解析正确

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECT_MCP_TOOLS = 35
PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def call_text(result) -> str:
    """从 CallToolResult 提取纯文本内容（TextContent 列表拼串）。"""
    out = []
    for c in (result.content or []):
        t = getattr(c, "text", None)
        if isinstance(t, str):
            out.append(t)
    return "\n".join(out)


async def call_json(session, name, args: dict):
    """调工具并解析返回 JSON（MCP 工具统一返回 JSON 字符串）。"""
    return json.loads(call_text(await session.call_tool(name, args)))


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=_ROOT)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("[MCP 握手] initialize OK")

            # ── 1. tools/list 断言 ──
            tools = (await session.list_tools()).tools
            names = [t.name for t in tools]
            check("MCP 工具数 = %d" % EXPECT_MCP_TOOLS,
                  len(names) == EXPECT_MCP_TOOLS, f"(实际 {len(names)})")
            check("navigate 在列（外部经意图桥驱动浏览器）", "navigate" in names)
            check("canvas_command 不在列（web-only）", "canvas_command" not in names)
            for t in ("query_plots", "diagnose_retention", "generate_full_outline"):
                check(f"工具 {t} 在列", t in names)

            # ── 2. create → rule 大纲 → get_book_detail 往返 ──
            created = await call_json(session, "create_book", {
                "title": "MCP冒烟", "pen_name": "测试", "genre": "都市", "sub_genre": "爽文"})
            bid = created.get("book_id", "")
            check("create_book 返回 book_id", bool(bid), bid)
            try:
                ok_save = (await call_json(session, "save_basic_info", {
                    "book_id": bid,
                    "basic_info": {"protagonist": {
                        "name": "王小明", "identity": "程序员", "golden_finger": "读心术"}},
                })).get("ok")
                check("save_basic_info OK", bool(ok_save))
                r = await call_json(session, "generate_outlines",
                                    {"book_id": bid, "mode": "rule", "max_outlines": 2})
                check("generate_outlines(rule) count=2", r.get("ok") and r.get("count") == 2,
                      f"{r}")
                ok_confirm = (await call_json(
                    session, "confirm_outlines", {"book_id": bid})).get("ok")
                check("confirm_outlines OK", bool(ok_confirm))
                ok_gags = (await call_json(
                    session, "fill_gags", {"book_id": bid})).get("ok")
                check("fill_gags OK", bool(ok_gags))
                detail = await call_json(session, "get_book_detail", {"book_id": bid})
                check("get_book_detail title/主角 正确",
                      detail.get("title") == "MCP冒烟"
                      and (detail.get("protagonist") or {}).get("name") == "王小明",
                      f"phase={detail.get('phase')}")
            finally:
                # ── 3. navigate → 意图队列 ──
                nav = await call_json(session, "navigate", {"url": "/books", "tab": "tools"})
                check("navigate 返回 __navigate__", nav.get("__navigate__") == "/books")
                intent_file = os.path.join(_ROOT, "storage", "nav_intent.json")
                intents = []
                if os.path.exists(intent_file):
                    with open(intent_file, encoding="utf-8") as f:
                        try:
                            intents = json.load(f)
                        except Exception:
                            intents = []
                check("navigate 已写入意图队列（url=/books）",
                      any(i.get("url") == "/books" for i in intents))

                # ── 4. tool-log source=mcp 断言 ──
                log_file = os.path.join(_ROOT, "storage", "tool_log.jsonl")
                mcp_calls = []
                if os.path.exists(log_file):
                    with open(log_file, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                e = json.loads(line)
                            except Exception:
                                continue
                            if e.get("source") == "mcp":
                                mcp_calls.append(e)
                check("tool_log 出现 source=mcp 调用", len(mcp_calls) >= 3,
                      f"({len(mcp_calls)} 条，含 create_book={any(e['tool']=='create_book' for e in mcp_calls)})")

                # ── 5. delete_book 安全门 + 清理 ──
                deny = await call_json(session, "delete_book", {"book_id": bid})
                check("delete_book 未确认拒绝", deny.get("ok") is False,
                      f"{deny.get('error', '')[:40]}")
                cleaned = await call_json(
                    session, "delete_book", {"book_id": bid, "confirm": True})
                check("delete_book(confirm) 清理成功", cleaned.get("ok") is True)

    # 清理意图文件（避免残留）
    try:
        os.remove(os.path.join(_ROOT, "storage", "nav_intent.json"))
    except Exception:
        pass

    print("\n" + "=" * 50)
    print(f"  MCP 冒烟验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败项:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ MCP 协议验收通过（外部 agent 可经协议驱动 NovelEngine）")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
