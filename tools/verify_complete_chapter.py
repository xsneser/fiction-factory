#!/usr/bin/env python3
"""完整章节质量门禁验收 — 建临时书 + 手工落假章 + MCP 调 chapter_quality_gate 断言报告结构。

零 LLM（默认）：正文用规则拼一段（含 AI 高频词/对话/章末钩子/绑定词，让各 check 产出
多样结果），直接落 chapters/0001.json；门禁纯规则层、不调 LLM。
可选：
  --book X     对已有书最近一章跑门禁（不新建，配合 --chapter 指定章号）
  --write      先真实 write_chapter 再门禁（慢、烧 token，需书有桥段；临时书无桥段会 noop）
用法：
  python tools/verify_complete_chapter.py
  python tools/verify_complete_chapter.py --book book_003
  python tools/verify_complete_chapter.py --book book_003 --chapter 2 --write
"""
import argparse
import asyncio
import json
import os
import sys

# Windows 控制台默认 GBK，强制走 UTF-8（与 test_all.py 同款）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)   # 供 BookManager setup/teardown 导入项目包
os.chdir(_ROOT)   # 让 mcp_server 子进程的 books/、storage/ 相对路径解析正确

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def call_text(result) -> str:
    out = []
    for c in (result.content or []):
        t = getattr(c, "text", None)
        if isinstance(t, str):
            out.append(t)
    return "\n".join(out)


async def call_json(session, name, args: dict):
    """调工具并解析返回 JSON（MCP 工具统一返回 JSON 字符串）。"""
    return json.loads(call_text(await session.call_tool(name, args)))


# 假正文：塞入 AI 高频词（仿佛/似乎/只见）、对话、章末钩子（突然）、绑定词、重复数值，
# 让 review(ai_pattern)/continuity(system_binding/numeric)/retention(章末钩子) 各有产出
FAKE_CONTENT = """清晨，仿佛有一层薄雾笼罩着整座城市。林凡推开窗，似乎听见远处传来一阵轰鸣。
他揉了揉眼睛，只见一辆黑色轿车停在楼下。
“你是谁？”林凡问道。
黑衣人冷笑一声：“宿主，系统绑定成功。从今天起，你将获得每日签到奖励。”
林凡愣住了。这个系统来得太过突然，他隐约觉得事情并不简单。
他看了看自己的手，仿佛握住了什么力量。数值 9999 从眼前闪过，随即消失。
这一天，林凡决定先回家。他走在熟悉的街道上，心里却总觉得哪里不对劲。
傍晚时分，他收到了第一条签到奖励——一枚青铜钥匙。钥匙很轻，但他握在手里，似乎有某种温度。
就在这时，窗外的天空突然暗了下来。
"""


def _make_test_book():
    """setup：BookManager 直建临时书（create_book 已从 MCP 面移除，护栏要求），
    phase=ready + 手工落第 1 章假正文。返回 (bm, bid)。"""
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline
    bm = BookManager(os.path.join(_ROOT, "books"))
    cfg = bm.create(title="门禁验收", pen_name="测试", genre="都市", sub_genre="爽文",
                    chapter_count=500)
    bid = cfg.book_id
    bm.save_storyline(bid, BookStoryline(
        book_title=cfg.title,
        pen_name=cfg.pen_name, platform=cfg.platform,
        words_per_chapter=3000, basic_info={}, phase="ready"))
    cfg.current_chapter = 1
    cfg.status = "writing"
    bm.update(cfg)
    bm.save_chapter(bid, 1, "第1章", FAKE_CONTENT, "验收用假章")
    print(f"[落章] 假正文 {len(FAKE_CONTENT)} 字 → books/{bid}/chapters/0001.json")
    return bm, bid


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", default="", help="对已有书最近一章跑门禁（默认新建临时书）")
    ap.add_argument("--chapter", type=int, default=0, help="门禁目标章号（0=最近一章）")
    ap.add_argument("--write", action="store_true", help="先真实 write_chapter 再门禁（慢）")
    args = ap.parse_args()

    bm = None
    bid = args.book or ""
    if bid:
        from libraries.book_manager import BookManager
        bm = BookManager(os.path.join(_ROOT, "books"))
        book = bm.get(bid)
        if not book:
            print(f"❌ 书 {bid} 不存在")
            sys.exit(1)
        print(f"[book 模式] 已有书 {bid}（当前写到第 {book.current_chapter} 章）")
    else:
        bm, bid = _make_test_book()
        print(f"[setup] 临时书 {bid}（phase=ready）")

    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=_ROOT)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("[MCP 握手] initialize OK")
                names = [t.name for t in (await session.list_tools()).tools]
                check("chapter_quality_gate 在列", "chapter_quality_gate" in names)

                if args.write:
                    r = await call_json(session, "write_chapter", {"book_id": bid, "chapter_num": 0})
                    print(f"[--write] write_chapter → status={r.get('status')} "
                          f"word_count={r.get('word_count')}")
                    check("write_chapter 返回 status 字段", bool(r.get("status")), f"{r.get('status')}")

                gate = await call_json(session, "chapter_quality_gate",
                                       {"book_id": bid, "chapter_num": args.chapter})
                for key in ("book_id", "chapter", "word_count", "target_words", "passed",
                            "complete", "summary", "issue_count", "review_score",
                            "overdue_count", "stalled_count", "drop_risk_count",
                            "decision_points", "checks"):
                    check(f"门禁顶层键 {key} 存在", key in gate, f"={gate.get(key)!r}"[:120])
                checks = gate.get("checks") or {}
                check("checks 恰 5 项",
                      set(checks.keys()) == {"review", "continuity", "retention",
                                             "promises", "punch_points"},
                      f"({sorted(checks.keys())})")
                for k in ("review", "continuity", "retention", "promises", "punch_points"):
                    c = checks.get(k) or {}
                    check(f"checks.{k}.passed ∈ True/False/None",
                          c.get("passed") in (True, False, None), f"({c.get('passed')!r})")
                check("chapter>0", int(gate.get("chapter") or 0) > 0,
                      f"(chapter={gate.get('chapter')})")
                check("word_count>0", int(gate.get("word_count") or 0) > 0,
                      f"(word_count={gate.get('word_count')})")
                check("passed 为 bool", isinstance(gate.get("passed"), bool),
                      f"({gate.get('passed')!r})")
                check("decision_points 为 list", isinstance(gate.get("decision_points"), list))
                check("summary 非空", bool(gate.get("summary")), f"({gate.get('summary')})")
                if gate.get("decision_points"):
                    check("decision_points 首条字段齐",
                          all(k in gate["decision_points"][0]
                              for k in ("check", "severity", "description",
                                        "location", "suggestion")),
                          f"({gate['decision_points'][0]})")
                # dsh 8KB 裁剪约束：报告体积必须 <8KB
                payload = json.dumps(gate, ensure_ascii=False)
                check("报告体积 <8KB（dsh 裁剪阈值）", len(payload) < 8192,
                      f"({len(payload)} 字符)")
                print(f"\n[门禁摘要] {gate.get('summary')}")
                for dp in gate.get("decision_points") or []:
                    print(f"  · [{dp.get('check')}/{dp.get('severity')}] "
                          f"{dp.get('description')}")
    finally:
        if not args.book:
            # 仅清理脚本新建的临时书；--book 模式的真实书不动
            try:
                bm.delete(bid)
            except Exception:
                pass
            print(f"[teardown] 已删临时书 {bid}")

    print("\n" + "=" * 50)
    print(f"  完整章节质量门禁验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  ❌ 验收未通过")
        sys.exit(1)
    print("  ✅ 验收通过")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
