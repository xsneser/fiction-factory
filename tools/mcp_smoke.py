#!/usr/bin/env python3
"""MCP 协议验收 — 脚本化 MCP 客户端驱动 NovelEngine 最小闭环（零/低成本，不调 LLM 写作）。

用 mcp.client.stdio 连接 `python mcp_server.py`，走完整协议：
  initialize 握手 → tools/list（断言工具数 + 护栏）→ tools/call 真实往返。

护栏（2026-08-20 内置 agent 删除后）：create_book/delete_book 工具不存在于注册表，
建书走系统向导 UI（drive_ui 驱动），删书走书库页手动。因此：
  - setup/teardown 用 BookManager 直建直删临时书（文件级，不走 MCP）
  - MCP 面断言 create_book/delete_book **不存在**（护栏验收）
  - 往返测 save_basic_info → save_outlines → get_book_detail
  - drive_ui 命令桥意图断言

断言：
  1) MCP 工具数 = EXPECT_MCP_TOOLS；navigate/drive_ui/query_plots/query_profiles 在列；
     create_book/delete_book 不存在（护栏）
  2) 对临时书 save_basic_info → save_outlines → get_book_detail 全往返成功
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
sys.path.insert(0, _ROOT)   # 供 BookManager setup/teardown 导入项目包
os.chdir(_ROOT)   # 让 mcp_server 子进程的 books/、storage/ 相对路径解析正确

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECT_MCP_TOOLS = 43  # 39 + 4 样文库工具(add/delete/list/get_style_sample)。对齐 _build_registry 实收
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
    cfg = bm.create(title="MCP冒烟", pen_name="测试", chapter_count=500)
    bid = cfg.book_id
    bm.save_storyline(bid, BookStoryline(
        book_title=cfg.title, pen_name=cfg.pen_name, platform=cfg.platform,
        words_per_chapter=3000, basic_info={}, phase="config"))
    return bm, bid


async def main():
    # ── 0. setup：先建临时书，再拉起 MCP 子进程（子进程经磁盘协调可见）──
    bm, bid = _make_test_book()
    print(f"[setup] 临时书 {bid}（BookManager 直建，护栏：create_book 工具不存在）")

    # 临时笔名(样文库工具往返用;MCP 子进程与父进程共享 profiles/ 目录 → 落盘即可见)
    _tmp_pen = {"pid": "", "pen": ""}
    try:
        from libraries.profiles import ProfileManager
        _tp = ProfileManager().create(pen_name="冒烟样文库", description="mcp_smoke 临时笔名")
        _tmp_pen = {"pid": _tp.id, "pen": _tp.pen_name}
    except Exception as _e:  # 无权限/目录异常 → 跳过样文库往返,不阻塞主链路
        print(f"[setup] 临时笔名创建失败，跳过样文库往返: {_e}")

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
                check("create_book 工具不存在（护栏：建书走系统向导）", "create_book" not in names)
                check("delete_book 工具不存在（护栏：删书走书库页手动）", "delete_book" not in names)
                check("fill_gags 工具不存在（ready 只由书详情页确认触发）", "fill_gags" not in names)
                check("drive_ui 在列（建书向导命令桥）", "drive_ui" in names)
                check("navigate 在列（外部经意图桥驱动浏览器）", "navigate" in names)
                for t in ("query_plots", "query_profiles",
                          "query_characters", "chapter_quality_gate",
                          "fetch_book", "fetch_novel", "fetch_webnovel", "discover_hot", "list_rankings",
                          "list_crawled_novels", "read_crawled_novel", "extract_state", "ingest_library_assets",
                          "save_chapter_text", "save_bridge_draft", "save_outlines", "save_book_meta",
                          "get_writing_context", "get_pen_style", "add_style_rule", "delete_style_rule",
                          "add_style_sample", "delete_style_sample", "list_style_samples", "get_style_sample"):
                    check(f"工具 {t} 在列", t in names)

                # ── 2. 对临时书做 MCP 往返 ──
                ok_save = (await call_json(session, "save_basic_info", {
                    "book_id": bid,
                    "basic_info": {"protagonist": {
                        "name": "王小明", "identity": "程序员", "golden_finger": "读心术"}},
                })).get("ok")
                check("save_basic_info OK", bool(ok_save))
                r = await call_json(session, "save_outlines",
                                    {"book_id": bid, "outlines": [{"name": "开篇", "start_chapter": 1,
                                                                   "end_chapter": 30}],
                                     "plots": [{"name": "穿越开局", "outline_id": "outline_0001",
                                                "cover_beats": 6, "words": 1400}]})
                check("save_outlines OK", r.get("ok") and r.get("outlines") == 1,
                      f"{r}")
                detail = await call_json(session, "get_book_detail", {"book_id": bid})
                check("get_book_detail title/主角 正确",
                      detail.get("title") == "MCP冒烟"
                      and (detail.get("protagonist") or {}).get("name") == "王小明",
                      f"phase={detail.get('phase')}")
                # save_outlines 须透传 cover_beats/words（config/补弧路径勿静默重置默认 4）
                def _find_val(obj, key, val):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k == key and v == val:
                                return True
                            if isinstance(v, (dict, list)) and _find_val(v, key, val):
                                return True
                    elif isinstance(obj, list):
                        return any(_find_val(x, key, val) for x in obj if isinstance(x, (dict, list)))
                    return False
                sl = await call_json(session, "get_storyline", {"book_id": bid})
                check("save_outlines 保留 cover_beats/words",
                      _find_val(sl, "words", 1400) and _find_val(sl, "cover_beats", 6),
                      "桥段 words=1400/cover_beats=6 应落库")
                ctx = await call_json(session, "get_writing_context", {"book_id": bid})
                sc = (ctx.get("style_card") or "")
                check("get_writing_context style_card 非空（无笔名也注入默认笔名精简卡）",
                      isinstance(sc, str) and len(sc) > 20 and "笔名" in sc, f"{len(sc)} 字符")
                ps = await call_json(session, "get_pen_style", {"book_id": bid})
                check("get_pen_style 返回风格权威（style_rules/forbidden 非空）",
                      bool((ps.get("style_rules") or "")) and bool(ps.get("forbidden")),
                      f"{ps.get('pen_name')} style_rules {len(ps.get('style_rules') or '')} 字符")

                # ── 2.5 样文库工具往返(add/list/get/delete,临时笔名)──
                if _tmp_pen["pid"]:
                    _sm_text = ("这是一段用于样文库冒烟测试的连续场景文本，不含任何版权内容，"
                                "仅用于验证样文库的落盘、镜像渲染与预算注入链路是否可用。")
                    sa = await call_json(session, "add_style_sample", {
                        "profile_id": _tmp_pen["pid"], "text": _sm_text,
                        "title": "冒烟场景", "scene_tags": ["冒烟", "测试"]})
                    check("add_style_sample OK（落库 1 条 s1）",
                          sa.get("ok") and sa.get("total") == 1 and (sa.get("sample") or {}).get("id") == "s1",
                          f"{sa}")
                    sl = await call_json(session, "list_style_samples", {"profile_id": _tmp_pen["pid"]})
                    check("list_style_samples 元数据在列（不含正文）",
                          sl.get("count") == 1 and not (sl.get("samples") or [{}])[0].get("text"),
                          f"{sl.get('count')} 条")
                    gs = await call_json(session, "get_style_sample", {
                        "profile_id": _tmp_pen["pid"], "sample_id": "s1"})
                    check("get_style_sample 取回全文",
                          (gs.get("sample") or {}).get("text", "").startswith("这是一段"),
                          f"len={len((gs.get('sample') or {}).get('text') or '')}")
                    sd = await call_json(session, "delete_style_sample", {
                        "profile_id": _tmp_pen["pid"], "sample_id": "s1"})
                    sl2 = await call_json(session, "list_style_samples", {"profile_id": _tmp_pen["pid"]})
                    check("delete_style_sample OK（库清空）",
                          sd.get("ok") and sl2.get("count") == 0, f"{sd}")

                # drive_ui 步校验的宽松阀：写默认 build_status（updated_at 空 = 无真实向导状态），
                # 使 set_outline 等步敏感命令的步校验跳过，测试不依赖 live 向导状态。
                try:
                    from libraries.build_status import set_build_status as _reset_st
                    _reset_st({})
                except Exception:
                    pass

                # ── 3. navigate + drive_ui → 意图队列 ──
                nav = await call_json(session, "navigate", {"url": "/books", "tab": "tools"})
                check("navigate 返回 __navigate__", nav.get("__navigate__") == "/books")
                dui = await call_json(session, "drive_ui", {"cmd": "next"})
                check("drive_ui 返回 __ui_command__", dui.get("__ui_command__") == "next")
                dui2 = await call_json(session, "drive_ui", {"cmd": "set_outline",
                    "args": {"outlines": [{"id": "outline_0001", "name": "测试大纲"}], "plots": []}})
                check("drive_ui set_outline 返回 __ui_command__", dui2.get("__ui_command__") == "set_outline")
                # set_review（侦察/提取页呈现五库候选）：title 必填、五类至少一类非空
                dui3 = await call_json(session, "drive_ui", {"cmd": "set_review",
                    "args": {"title": "冒烟测试书", "platform": "fanqie", "folder": "冒烟测试书",
                             "plots": [{"name": "测试桥段", "category": "测试", "structure": "测试"}],
                             "style_rules": [{"kind": "prefer", "pattern": "句长偏短"}]}})
                check("drive_ui set_review 返回 __ui_command__", dui3.get("__ui_command__") == "set_review")
                # 负例：缺 title 应被拒（工具抛错或返回错误文本都算拒绝）
                _bad_review = True
                try:
                    _r = await session.call_tool("drive_ui",
                                                 {"cmd": "set_review", "args": {"title": ""}})
                    if "__ui_command__" in call_text(_r):
                        _bad_review = False
                except Exception:
                    pass
                check("drive_ui set_review 缺 title/五类被拒", _bad_review)
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
                check("drive_ui set_outline 已写入意图队列（kind=ui_command cmd=set_outline）",
                      any(i.get("kind") == "ui_command" and i.get("cmd") == "set_outline" for i in intents))
                check("drive_ui set_review 已写入意图队列（kind=ui_command cmd=set_review）",
                      any(i.get("kind") == "ui_command" and i.get("cmd") == "set_review" for i in intents))
                # extract_state（整本扫读断点/记忆存档）：load 无记录 → save → load(summary) 往返 → clear
                st0 = await call_json(session, "extract_state",
                                      {"folder": "冒烟扫读书", "action": "load"})
                check("extract_state load 无记录 exists=false",
                      st0.get("exists") is False)
                st1 = await call_json(session, "extract_state",
                                      {"folder": "冒烟扫读书", "action": "save",
                                       "state": {"book": {"title": "冒烟扫读书", "chapter_count": 10},
                                                 "cursor": 3, "status": "running",
                                                 "memory": {"digest": "已读前三章梗概"}}})
                check("extract_state save OK（cursor=3）",
                      st1.get("ok") is True and st1.get("cursor") == 3)
                st2 = await call_json(session, "extract_state",
                                      {"folder": "冒烟扫读书", "action": "load", "mode": "summary"})
                check("extract_state load(summary) 往返 cursor/digest 正确",
                      st2.get("exists") is True and st2.get("cursor") == 3
                      and (st2.get("memory") or {}).get("digest", "") == "已读前三章梗概")
                st3 = await call_json(session, "extract_state",
                                      {"folder": "冒烟扫读书", "action": "clear"})
                check("extract_state clear OK", st3.get("cleared") == "冒烟扫读书")

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
        try:
            os.remove(os.path.join(_ROOT, "storage", "extract_work", "冒烟扫读书.json"))
        except Exception:
            pass
        # 样文库临时笔名清理：删 profile + style_refs 文件（删库不留痕）
        if _tmp_pen["pen"]:
            try:
                from libraries.profiles import ProfileManager
                ProfileManager().delete(_tmp_pen["pid"])
            except Exception:
                pass
            for _ext in (".samples.json", ".reference.txt"):
                try:
                    os.remove(os.path.join(_ROOT, "storage", "style_refs", _tmp_pen["pen"] + _ext))
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
