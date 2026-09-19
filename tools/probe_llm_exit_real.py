#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实形状的 LLM 出口诊断（**手动工具**，写作流程不会自动跑它）。

用它回答一个问题：**「现在我们配的这个模型/中转，能不能真的把正文生成出来？」**
它按真实 run 的形状发请求（该 profile 的真实工具集 + persona + skill 头部 + 任务头部），
而不是拿一个极小 ping 去猜——2026-09-19 那次 root run 连续 6 次空回复，而极小 ping 一直通过，
就是因为强制 tool_choice 的 ping 与「自由生成正文」不是同一件事。

用法：
    python tools/probe_llm_exit_real.py                # 默认 profile=orchestrate
    python tools/probe_llm_exit_real.py write          # 换 profile（write/orchestrate/build/…）
    python tools/probe_llm_exit_real.py --contrast     # 只跑强制工具 vs 自由生成的最小对照
    python tools/probe_llm_exit_real.py --exact        # 额外用 dsh 真实 max_tokens（256000）复现

注意：请求会打到你在 /settings 配的那个出口，prompt 里含任务与 skill 的**开头片段**
（与正常写作完全相同的去向，不含任何密钥）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _row(name: str, res: dict) -> str:
    c = res.get("counts") or {}
    return (f"  {name:14s} status={res.get('status')} finish={c.get('finish')!r} "
            f"text={c.get('text', 0)} reasoning={c.get('reasoning', 0)} "
            f"tool_calls={c.get('tool_calls', 0)} code={res.get('code')!r} ok={res.get('ok')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", nargs="?", default="orchestrate",
                    help="按哪个 profile 的真实形状探测（默认 orchestrate）")
    ap.add_argument("--contrast", action="store_true", help="只跑最小对照（强制工具 vs 自由生成 vs 自由文本）")
    ap.add_argument("--exact", action="store_true", help="额外用 dsh 真实 max_tokens=256000 复现一次")
    ap.add_argument("--task", default="", help="覆盖任务文本（默认用一段章级任务样例）")
    args = ap.parse_args()

    from libraries import dsh_bridge as DB
    from libraries.token_proxy import llm_exit_info, probe_llm_exit

    info = llm_exit_info()
    print(f"出口：model={info.get('model') or '未知'}  upstream={info.get('upstream') or '未知'}")

    if args.contrast:
        r = probe_llm_exit(mode="contrast", use_cache=False, timeout=120)
        for key in ("forced", "free", "prose"):
            print(_row(key, r.get(key) or {}))
        print(f"\n结论：{r.get('verdict')}")
        return 0

    task = args.task or ("请继续写这本（book book_XXXX）：完成第 16 章正文，目标约 3000 字。"
                         "这是一次章级父任务：请由服务端流程自主判断写作、收章或续规划，直到本章完成。")
    tools = DB._profile_probe_tools(args.profile)
    user = (task[:400] + "\n\n[当前 Skill，必须遵守]\n"
            + DB._skill_text_for_profile(args.profile)[:800])
    print(f"形状：profile={args.profile}  工具={len(tools)} 个  "
          f"skill 头部 {min(800, len(DB._skill_text_for_profile(args.profile)))} 字符  "
          f"任务头部 {min(400, len(task))} 字符")

    r = probe_llm_exit(mode="free", shape_tag=f"real:{args.profile}", max_tokens=2048,
                       use_cache=False, timeout=180, tools=tools,
                       system=DB._persona_for_profile(args.profile), user=user)
    print(_row("自由生成(2048)", r))
    if r.get("message"):
        print(f"  → {r['message']}")

    if args.exact:
        r2 = probe_llm_exit(mode="free", shape_tag=f"real-exact:{args.profile}",
                            max_tokens=256000, use_cache=False, timeout=300, tools=tools,
                            system=DB._persona_for_profile(args.profile), user=user)
        print(_row("自由生成(256000)", r2))

    ok = bool(r.get("ok"))
    print("\n" + ("✅ 出口能出字：本次若仍失败，更像间歇性/具体请求内容触发，而不是出口能力问题。"
                  if ok else
                  "❌ 出口出不了字：这是模型/中转侧问题 —— 请在 /settings 换模型或换中转后重试。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
