#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 失败面与编排可见性回归（**零网络、零 LLM**）。

钉住三件事：
  1. `_llm_failure_note` **只**对明确的 LLM adapter 码追加诊断提示，且返回
     「原始错误 + 诊断提示」（不覆盖原文）；TIMEOUT/TRANSPORT 等模糊码原样返回——
     MCP 工具超时、subagent 超时都报这些，改写成「模型超时」就是误诊；
  2. `_writer_failure_message`：有 run 级错误 → 报错误本身（不再说「Writer 未完成
     有效 Plot 提交」）；真·没提交且无错误 → 保留原文案；
  3. 「编排回退开关」与「LLM 出口修复」两类建议**不许混进同一条消息**：
     NOVEL_DSH_ORCHESTRATOR 只能出现在编排类提示里。

用法：python tools/test_dsh_failure_surface.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries import dsh_bridge as DB   # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


print("\n═══ 1. _llm_failure_note：只改写明确的 adapter 码 ═══")
EMPTY = "EMPTY_RESPONSE: model returned a completed response with no content"
note = DB._llm_failure_note(EMPTY)
check("EMPTY_RESPONSE 追加诊断提示且保留原文", EMPTY in note and "诊断提示" in note, note)
check("提示里带 model/upstream 位置信息", "upstream=" in note, note)
check("提示指向 /settings 与分层复现命令",
      "/settings" in note and 'probe_llm_exit(mode="diagnose")' in note, note)
check("出口类提示**不提**编排回退开关", "NOVEL_DSH_ORCHESTRATOR" not in note, note)

check("STREAM_CLOSED 也识别",
      "诊断提示" in DB._llm_failure_note("STREAM_CLOSED: SSE payload stream ended without [DONE]"))
check("MALFORMED_RESPONSE 也识别",
      "诊断提示" in DB._llm_failure_note("MALFORMED_RESPONSE: malformed SSE payload: {"))

for raw in ("TIMEOUT: MCP tool call timed out after 600000ms",
            "TRANSPORT: connection reset",
            "RATE_LIMIT: too many requests",
            "任务已被打断",
            "dsh 任务异常退出（exit 1）",
            "Error: 命令 set_replan_preview planning_patch 校验失败：h1 必须是非空 list"):
    check(f"模糊/非 LLM 错误原样返回：{raw[:28]}…", DB._llm_failure_note(raw) == raw,
          DB._llm_failure_note(raw))
check("幂等：同一条错误过两遍不叠提示",
      DB._llm_failure_note(note).count("诊断提示：") == 1, DB._llm_failure_note(note))
check("幂等：_writer_failure_message 里也不会出现两段提示",
      DB._writer_failure_message(note, "", "book_003").count("诊断提示：") == 1,
      DB._writer_failure_message(note, "", "book_003"))
check("幂等：run 级出口 + 子 run 文案串起来仍只有一段提示",
      DB._writer_failure_message(DB._llm_failure_note(EMPTY), "", "book_003").count("诊断提示：") == 1)
check("认不出码时 _llm_failure_code 返回空", DB._llm_failure_code("TIMEOUT: x") == "",
      DB._llm_failure_code("TIMEOUT: x"))
check("EMPTY_RESPONSE 的码被认出", DB._llm_failure_code(EMPTY) == "EMPTY_RESPONSE")
check("未知 finish_reason（model stopped:）也归到 adapter 一类",
      DB._llm_failure_code("model stopped: safety") == "MODEL_STOPPED")

print("\n═══ 2. _writer_failure_message：不再把上游故障说成 Writer 没提交 ═══")
msg = DB._writer_failure_message(EMPTY, "", "book_003")
check("有 run 级错误 → 报错误本身 + 诊断提示",
      "Writer 子 run 报错" in msg and "EMPTY_RESPONSE" in msg and "诊断提示" in msg, msg)
check("有错误时不再出现「Writer 未完成有效 Plot 提交」",
      "未完成有效 Plot 提交" not in msg, msg)
msg2 = DB._writer_failure_message("", "这是 Writer 的收尾话", "book_003")
check("无错误 → 保留原文案（真·没提交）",
      msg2.startswith("Writer 未完成有效 Plot 提交") and "book_003" in msg2
      and "这是 Writer 的收尾话" in msg2, msg2)
check("带 book_id 便于定位", "book=book_003" in msg, msg)

print("\n═══ 3. 编排 notice 与 LLM 出口提示分工 ═══")
notice = DB._orchestrator_fallback_notice({"problems": ["缺少必需条目：mcp-novelengine-orch",
                                                        "具名子代理未启用 spawn provider"]})
check("notice 给启用命令", "sync_dsh_headless_profile.py --apply" in notice, notice)
check("notice 带上具体 problems（前 2 条）",
      "mcp-novelengine-orch" in notice and "spawn provider" in notice, notice)
check("notice 才是提回退开关的地方", "NOVEL_DSH_ORCHESTRATOR=0" in notice, notice)

print("\n═══ 4. leaf 运行期 overlay 关掉 9 个角色 server（编排 overlay 不关）═══")
from pathlib import Path   # noqa: E402
for profile in ("write", "build", "build-candidates", "scout", "publish", "replan", "style", ""):
    text = Path(DB._write_runtime_overlay(mcp_profile=profile)).read_text(encoding="utf-8")
    missing = [role for role, _ in DB._ROLE_MCP_SERVERS
               if f"- id: mcp-novelengine-{role}\n  disabled: true" not in text]
    check(f"leaf({profile or 'legacy'}) overlay 禁用全部角色 server", not missing, str(missing))
    check(f"leaf({profile or 'legacy'}) 仍挂单实例 mcp-novelengine",
          "- id: mcp-novelengine\n" in text, "缺单实例 server")

orch = Path(DB._write_runtime_overlay(mcp_profile="orchestrate")).read_text(encoding="utf-8")
check("orchestrate overlay 不给角色 server 加 disabled",
      "- id: mcp-novelengine-orch\n  disabled: true" not in orch)
check("orchestrate overlay 挂 9 个角色 server",
      all(f"- id: mcp-novelengine-{role}\n" in orch for role, _ in DB._ROLE_MCP_SERVERS),
      "缺角色 server")
check("orchestrate overlay 关掉单实例 mcp-novelengine",
      "- id: mcp-novelengine\n  disabled: true" in orch)

print("\n═══ 5. 任务口径：哪些任务才算编排/写作类 ═══")
check("章级父任务算编排类", DB._is_orchestrated_task("继续写", "chapter_to_completion"))
check("写正文任务算编排类", DB._is_orchestrated_task("继续写下一章（book book_003）"))
check("只读问句不算编排类", not DB._is_orchestrated_task("查看状态"))
check("章级任务算写作类（要预检出口）", DB._is_writing_task("继续写", "chapter_to_completion"))
check("只读问句不预检", not DB._is_writing_task("查看状态"))

print()
if FAILURES:
    print(f"  ❌ {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print("     - " + f)
    raise SystemExit(1)
print("  ✅ dsh 失败面：只改写明确 adapter 码 / 归因不误报 / overlay 工具面护栏 / 提示分工")
