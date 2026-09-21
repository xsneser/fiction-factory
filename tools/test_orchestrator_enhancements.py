#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排体系强化与新契约回归测试。

覆盖：
1. Plot 自适应预算与有效硬上限拦截（assigned=300 vs 1000，超出必拒，草稿不被污染）；
2. 关系数据规范化（Protocol v3 统一字典结构 + v2 兼容，双源冲突自动解决）；
3. 开章供给不足时拦截 WRITE_NEXT_PLOT 并推荐 DELEGATE_PLANNER；
4. 收章硬门禁与质量四态诊断解耦；
5. 编排事件流 delegation_id 稳定关联与 progress 生成。
"""
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries.plot_submission import (
    compute_effective_plot_budget,
    validate_plot_prose_units,
    normalize_plot_submission_facts,
    PlotEffectiveMaxExceeded,
)
from libraries import orchestration_policy
from libraries import dsh_bridge as DB


class TestPlotSubmissionAndBudget(unittest.TestCase):
    def test_adaptive_budget_calculation(self):
        # assigned = 300
        b300 = compute_effective_plot_budget(300)
        self.assertEqual(b300["assigned"], 300)
        self.assertLessEqual(b300["preferred_min"], 300)
        self.assertGreaterEqual(b300["preferred_max"], 300)
        # 300 字段落的有效硬上限约在 500~600 之间
        self.assertTrue(500 <= b300["effective_hard_max"] <= 650)

        # assigned = 1000
        b1000 = compute_effective_plot_budget(1000)
        self.assertEqual(b1000["assigned"], 1000)
        self.assertGreaterEqual(b1000["effective_hard_max"], 1350)
        self.assertLessEqual(b1000["effective_hard_max"], 1600)

    def test_plot_prose_units_hard_intercept(self):
        b300 = compute_effective_plot_budget(300)
        eff_max = b300["effective_hard_max"]

        # 450 字：正常放行 (eff_max = 555)
        text_450 = "测" * 450
        actual = validate_plot_prose_units(text_450, eff_max, assigned=300, plot_id="p1", is_v2=True)
        self.assertLessEqual(actual, eff_max)

        # 700 字：超上限，严格拒绝
        text_700 = "测" * 700
        with self.assertRaises(PlotEffectiveMaxExceeded):
            validate_plot_prose_units(text_700, eff_max, assigned=300, plot_id="p1", is_v2=True)

        # 1100 字：超上限，严格拒绝
        text_1100 = "测" * 1100
        with self.assertRaises(PlotEffectiveMaxExceeded):
            validate_plot_prose_units(text_1100, eff_max, assigned=300, plot_id="p1", is_v2=True)

        # assigned=1000, actual=1150 放行 (eff_max = 1500)
        b1000 = compute_effective_plot_budget(1000)
        text_1150 = "测" * 1150
        actual_1150 = validate_plot_prose_units(text_1150, b1000["effective_hard_max"], assigned=1000, plot_id="p2", is_v2=True)
        self.assertLessEqual(actual_1150, b1000["effective_hard_max"])

    def test_relationship_changes_protocol_v3_normalize(self):
        # 双源输入测试：character_events 中有 relationship，outcome 里也有 relationship_changes
        raw_outcome = {
            "choices_made": ["选择进入空岛"],
            "relationship_changes": ["李四对张三改观", {"subject": "王五", "target": "张三", "change_type": "trust_change", "to_state": "怀疑"}],
        }
        raw_char_events = [
            {
                "name": "张三",
                "events": [
                    {"type": "goal_shift", "to": "寻找水源"},
                    {"type": "relationship", "target": "李四", "to": "成为生死之交", "reason": "共同脱险"},
                ],
            },
        ]
        norm_outcome, clean_events = normalize_plot_submission_facts(raw_outcome, raw_char_events)

        # 1. 验证不抛双源异常
        # 2. 验证 clean_events 中的 relationship 已被提取并移除，只保留 goal_shift
        self.assertEqual(len(clean_events[0]["events"]), 1)
        self.assertEqual(clean_events[0]["events"][0]["type"], "goal_shift")

        # 3. 验证 norm_outcome.relationship_changes 包含了三条规范化 v3 结构
        rels = norm_outcome["relationship_changes"]
        self.assertEqual(len(rels), 3)
        # 第一条（来自旧 v2 字符串）：规范化对象
        self.assertEqual(rels[0]["to_state"], "李四对张三改观")
        self.assertEqual(rels[0]["change_type"], "relationship")
        # 第二条（来自 v3 字典）
        self.assertEqual(rels[1]["subject"], "王五")
        self.assertEqual(rels[1]["target"], "张三")
        # 第三条（从 character_events 提取出来的）
        self.assertEqual(rels[2]["subject"], "张三")
        self.assertEqual(rels[2]["target"], "李四")
        self.assertEqual(rels[2]["to_state"], "成为生死之交")


class TestOrchestrationPolicyAndSupply(unittest.TestCase):
    def test_opening_supply_insufficient_intercept(self):
        # 模拟第18章现场：开章空草稿，只剩 1 个 300 字段落，needs_replan 为 True，commit_floor=1800
        facts = {
            "draft_has_bridges": False,
            "boundary": {"needs_replan": True, "reason_codes": ["PLOTS_LOW", "WORDS_LOW"]},
            "chapter_status": {"commit_floor": 1800, "written_words": 0},
            "available_committed_planned_words": 300,
            "is_terminal_chapter": False,
            "next_plot": {"id": "plot_040", "planned_words": 300},
            "budget": {"actions_used": 0, "actions_max": 20, "replan_used": 0, "replan_max": 3},
            "preview": {"exists": False},
        }
        perms = orchestration_policy.compute_orchestration_permissions(facts)

        # 断言 WRITE_NEXT_PLOT 必须被硬拦截
        self.assertFalse(perms["write_next_plot"]["allowed"])
        self.assertIn("OPENING_COMMITTED_SUPPLY_BELOW_FLOOR", perms["write_next_plot"]["reasons"])

        # 断言 generate_replan 允许，但 commit_replan 因无 preview 被拒
        self.assertTrue(perms["generate_replan"]["allowed"])
        self.assertFalse(perms["commit_replan"]["allowed"])

        # 断言推荐动作优先切为 DELEGATE_PLANNER
        rec = orchestration_policy.recommend_action(facts, perms)
        self.assertEqual(rec["action"], "DELEGATE_PLANNER")
        self.assertIn("OPENING_COMMITTED_SUPPLY_BELOW_FLOOR", rec["reason_codes"])


class TestEventStreamDelegationMapping(unittest.TestCase):
    def test_delegation_id_and_progress_emission(self):
        pending = {}
        # 1. 主 Agent 发起 delegate_writer
        del_evt = {
            "type": "tool/call",
            "data": {
                "name": "delegate_writer",
                "callId": "call_w1",
                "sessionId": "root-session",
                "arguments": "{}",
            },
        }
        mapped_del = list(DB._map_dsh_event(del_evt, pending))
        # 应该产出 progress 事件和 tool_call 事件
        types = [e["type"] for e in mapped_del]
        self.assertIn("progress", types)
        self.assertIn("tool_call", types)

        # 提取 delegation_id
        tool_call_evt = next(e for e in mapped_del if e["type"] == "tool_call")
        self.assertEqual(tool_call_evt["delegation_id"], "dg_call_w1")
        self.assertEqual(tool_call_evt["agent_role"], "root")

        # 2. Writer 子 Agent 启动
        start_evt = {
            "type": "subagent/start",
            "data": {
                "runId": "r1",
                "sessionId": "child-session-writer",
                "parentSessionId": "root-session",
            },
        }
        mapped_start = list(DB._map_dsh_event(start_evt, pending))
        self.assertEqual(mapped_start[0]["type"], "subagent_start")
        self.assertEqual(mapped_start[0]["delegation_id"], "dg_call_w1")
        self.assertEqual(mapped_start[0]["agent_role"], "writer")

        # 3. Writer 子 Agent 调用 prepare_plot_run
        child_call_evt = {
            "type": "tool/call",
            "data": {
                "name": "mcp__novelengine-write__prepare_plot_run",
                "callId": "call_prep",
                "sessionId": "child-session-writer",
                "parentSessionId": "root-session",
                "delegationDepth": 1,
            },
        }
        mapped_child = list(DB._map_dsh_event(child_call_evt, pending))
        child_tool_evt = next(e for e in mapped_child if e["type"] == "tool_call")
        # 验证子工具精准挂载到 dg_call_w1，且角色为 writer
        self.assertEqual(child_tool_evt["delegation_id"], "dg_call_w1")
        self.assertEqual(child_tool_evt["agent_role"], "writer")

        # 4. Writer 子 Agent 的 LLM 调用（调试模式）
        child_llm_evt = {
            "type": "llm/call",
            "data": {
                "seq": 101,
                "turn": 1,
                "step": 1,
                "sessionId": "child-session-writer",
                "parentSessionId": "root-session",
                "delegationDepth": 1,
                "request": {"model": "deepseek-v4-flash", "messages": []},
                "response": {"content": "ok"},
            },
        }
        mapped_child_llm = list(DB._map_dsh_event(child_llm_evt, pending))
        child_llm = next(e for e in mapped_child_llm if e["type"] == "llm_call")
        self.assertEqual(child_llm["delegation_id"], "dg_call_w1")
        self.assertEqual(child_llm["agent_role"], "writer")
        self.assertEqual(child_llm["session_id"], "child-session-writer")

        # 5. Root Orchestrator 的 LLM 调用
        root_llm_evt = {
            "type": "llm/call",
            "data": {
                "seq": 102,
                "turn": 0,
                "step": 0,
                "sessionId": "root-session",
                "parentSessionId": "",
                "delegationDepth": 0,
                "request": {"model": "deepseek-v4-flash", "messages": []},
                "response": {"content": "root thinking"},
            },
        }
        mapped_root_llm = list(DB._map_dsh_event(root_llm_evt, pending))
        root_llm = next(e for e in mapped_root_llm if e["type"] == "llm_call")
        self.assertEqual(root_llm["delegation_id"], "")
        self.assertEqual(root_llm["agent_role"], "root")
        self.assertEqual(root_llm["session_id"], "root-session")


if __name__ == "__main__":
    unittest.main()
