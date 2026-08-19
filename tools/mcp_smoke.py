#!/usr/bin/env python3
"""MCP 协议验收 — 脚本化 MCP 客户端驱动 NovelEngine 最小闭环（零/低成本，不调 LLM 写作）。

用 mcp.client.stdio 连接 `python mcp_server.py`，走完整协议：
  initialize 握手 → tools/list（断言 35 个 mcp 面工具 + web-only 护栏）→ tools/call 真实往返。

护栏（2026-08-19 架构决策）：create_book / delete_book 已从 MCP 面移除（web-only），
建书走系统向导 UI（drive_ui 驱动），删书走书库页手动。因此：
  - setup/teardown 用 BookManager 直建直删临时书（文件级，不走 MCP）
  - MCP 面断言 create_book/delete_book/canvas_command **不在列**（护栏验收）
  - 往返改测 save_basic_info → generate_outlines(rule) → confirm_outlines → fill_gags → get_book_detail
  - 新增 drive_ui 命令桥意图断言

断言：
  1) MCP 面工具数 = 35；navigate/drive_ui/query_plots/diagnose_retention/query_profiles 在列；
     create_book/delete_book/canvas_command 不在列（web-only 护栏）；Web 面保留 create/delete（双面互证）
  2) 对临时书 save_basic_info → rule 大纲 → confirm → fill_gags → get_book_detail 全往返成功
  3) navigate 与 drive_ui 分别写入 storage/nav_intent.json（kind=navigate / kind=ui_command）
  4) storage/tool_log.jsonl 出现 source="mcp" 调用条目（含 drive_ui）

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
sys.path.insert(0, _ROOT)   # 供 BookManager setup/teardown 与 tools_for_surface 导入项目包
os.chdir(_ROOT)   # 让 mcp_server 子进程的 books/、storage/ 相对路径解析正确

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECT_MCP_TOOLS = 37
# web-only 护栏：这三个工具不得出现在 MCP 面（建书/删书必须走系统界面）
WEB_ONLY_ABSENT = ["create_book", "delete_book", "canvas_command"]
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


def _make_test_book():
    """setup：BookManager 直建临时书（create_book 已从 MCP 面移除，护栏要求）。返回 book_id。"""
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline
    bm = BookManager(os.path.join(_ROOT, "books"))
    cfg = bm.create(title="MCP冒烟", pen_name="测试", genre="都市", sub_genre="爽文",
                    chapter_count=500)
    bid = cfg.book_id
    bm.save_storyline(bid, BookStoryline(
        book_title=cfg.title, genre=cfg.genre, sub_genre=cfg.sub_genre,
        pen_name=cfg.pen_name, platform=cfg.platform,
        words_per_chapter=3000, basic_info={}, phase="config"))
    return bm, bid


async def main():
    # ── 0. setup：先建临时书，再拉起 MCP 子进程（子进程经磁盘协调可见）──
    bm, bid = _make_test_book()
    print(f"[setup] 临时书 {bid}（BookManager 直建，护栏：MCP 面无 create_book）")

    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=_ROOT)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("[MCP 握手] initialize OK")

                # ── 1. tools/list 断言（含护栏）──
                tools = (await session.list_tools()).tools
                names = [t.name for t in tools]
                check("MCP 工具数 = %d" % EXPECT_MCP_TOOLS,
                      len(names) == EXPECT_MCP_TOOLS, f"(实际 {len(names)})")
                for t in WEB_ONLY_ABSENT:
                    check(f"{t} 不在 MCP 面（web-only 护栏）", t not in names)
                check("drive_ui 在列（建书向导命令桥）", "drive_ui" in names)
                check("navigate 在列（外部经意图桥驱动浏览器）", "navigate" in names)
                for t in ("query_plots", "diagnose_retention", "generate_full_outline", "query_profiles",
                          "query_characters", "generate_characters"):
                    check(f"工具 {t} 在列", t in names)

                # Web 面保留 create/delete（护栏双面互证）
                from agent_tools import tools_for_surface  # noqa: E402
                web_names = [t["name"] for t in tools_for_surface("web")]
                check("Web 面保留 create_book/delete_book",
                      {"create_book", "delete_book"} <= set(web_names))
                check("MCP 面无 create_book/delete_book",
                      {"create_book", "delete_book"} & set(names) == set())

                # ── 2. 对临时书做 MCP 往返 ──
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

                # ── 3. navigate + drive_ui → 意图队列 ──
                nav = await call_json(session, "navigate", {"url": "/books", "tab": "tools"})
                check("navigate 返回 __navigate__", nav.get("__navigate__") == "/books")
                dui = await call_json(session, "drive_ui", {"cmd": "next"})
                check("drive_ui 返回 __ui_command__", dui.get("__ui_command__") == "next")
                intent_file = os.path.join(_ROOT, "storage", "nav_intent.json")
                intents = []
                if os.path.exists(intent_file):
                    with open(intent_file, encoding="utf-8") as f:
                        try:
                            intents = json.load(f)
                        except Exception:
                            intents = []
                check("navigate 已写入意图队列（kind=navigate url=/books）",
                      any(i.get("url") == "/books" and i.get("kind") == "navigate" for i in intents))
                check("drive_ui 已写入意图队列（kind=ui_command cmd=next）",
                      any(i.get("kind") == "ui_command" and i.get("cmd") == "next" for i in intents))

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
                      f"({len(mcp_calls)} 条，含 drive_ui={any(e['tool']=='drive_ui' for e in mcp_calls)})")
    finally:
        # ── teardown：文件级清理临时书 + 意图文件（删除工具对 MCP 不可见，只能文件级）──
        try:
            bm.delete(bid)
        except Exception:
            pass
        try:
            os.remove(os.path.join(_ROOT, "storage", "nav_intent.json"))
        except Exception:
            pass

    print("\n" + "=" * 50)
    print(f"  MCP 冒烟验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败项:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ MCP 协议验收通过（外部 agent 可经协议驱动 NovelEngine，护栏生效）")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
