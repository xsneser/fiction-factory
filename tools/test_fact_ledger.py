#!/usr/bin/env python3
"""fact_ledger 纯规则单元测试与边界验收。

测试覆盖：
1. 事实来源优先级：canonical > accepted staged；无 canonical 才回退 legacy_memory。
2. 同 plot_id 冲突：canonical 存在时忽略 staged。
3. 严格 review_state 门禁：仅 accepted 进入 ledger；rejected / pending / unreviewed 保守排除。
4. 多版本确定性收敛：同 plot 保留 accepted/current 最高版本。
5. 安全去重：同 plot 内相同文本/结构化键去重；不同 plot 相同文本保留。
6. 近期问题语义：new_story_questions 保持「近期提出问题」，不判 open/resolved；普通 facts 为历史事件。
7. 有界投影与分页：active 为空、recent 有界、cursor 翻页与失效防伪。
8. 缓存命中与指纹失效验证。
9. Entry 形状符合 {category, label, text, plot_id, plot_name, chapter_num, status, provenance, staged} 规范。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from libraries import fact_ledger  # noqa: E402


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def test_canonical_priority_and_legacy_fallback():
    """canonical 优先；若无 canonical 则回退 legacy_memory。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_book_fallback"
        book_dir = root / "books" / bid

        # 1. 只有 legacy_memory 时：回退读取
        memory_payload = {
            "schema_version": 1,
            "chapters": [
                {
                    "chapter_num": 1,
                    "plot_deltas": [
                        {
                            "plot_id": "p_legacy",
                            "plot_name": "旧章节记忆",
                            "chapter_num": 1,
                            "facts": {
                                "choices_made": ["旧时代做出的决定"],
                            },
                        }
                    ],
                }
            ],
        }
        _write_json(book_dir / "story_memory.json", memory_payload)

        sources = fact_ledger.load_fact_sources(bid, root=root)
        assert len(sources["canonical"]) == 0
        assert len(sources["legacy_memory"]) == 1
        assert sources["legacy_memory"][0]["plot_id"] == "p_legacy"

        proj = fact_ledger.project(bid, root=root)
        assert len(proj["recent"]) == 1
        entry = proj["recent"][0]
        assert entry["plot_id"] == "p_legacy"
        assert entry["provenance"] == "legacy_memory"
        assert entry["staged"] is False
        assert entry["status"] == "historical"
        assert entry["text"] == "旧时代做出的决定"
        assert "legacy_memory" in proj["provenance"]["sources"]

        # 2. 新增 canonical 文件后：权威切换，不混入 legacy_memory
        canonical_file = book_dir / "facts" / "plot" / "p_canonical.json"
        _write_json(canonical_file, {
            "plot_id": "p_canonical",
            "plot_name": "权威事实段",
            "chapter_num": 1,
            "facts": {
                "choices_made": ["正式归档的决定"],
            },
        })

        fact_ledger.clear_cache(bid, root=root)
        proj_after = fact_ledger.project(bid, root=root)
        pids = [e["plot_id"] for e in proj_after["recent"]]
        assert "p_canonical" in pids
        assert "p_legacy" not in pids, "存在 canonical 时不再混入旧镜像 legacy_memory"
        assert proj_after["provenance"]["plot_counts"]["canonical"] == 1
        assert proj_after["provenance"]["plot_counts"]["legacy_memory"] == 0


def test_canonical_ignores_staged_for_same_plot_id():
    """canonical > accepted staged；同 plot_id canonical 存在时忽略 staged。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_book_conflict"
        book_dir = root / "books" / bid

        # canonical 中包含 p1
        _write_json(book_dir / "facts" / "plot" / "p1.json", {
            "plot_id": "p1",
            "plot_name": "正式提交段",
            "chapter_num": 1,
            "facts": {
                "choices_made": ["正式决策文本"],
            },
        })

        # staged 中也包含 p1（即便 marked accepted 也应被 canonical 覆盖）
        _write_json(book_dir / "staged_story_state.json", {
            "chapter_num": 1,
            "plot_deltas": [
                {
                    "plot_id": "p1",
                    "plot_name": "暂存未收章版本",
                    "chapter_num": 1,
                    "review_state": "accepted",
                    "facts": {
                        "choices_made": ["暂存决策文本"],
                    },
                },
                {
                    "plot_id": "p2",
                    "plot_name": "暂存新段",
                    "chapter_num": 1,
                    "review_state": "accepted",
                    "facts": {
                        "choices_made": ["P2 暂存决策"],
                    },
                },
            ],
        })

        proj = fact_ledger.project(bid, root=root)
        texts = {e["plot_id"]: e["text"] for e in proj["recent"]}
        assert texts["p1"] == "正式决策文本", "同 plot_id canonical 必须覆盖 staged"
        assert texts["p2"] == "P2 暂存决策", "非冲突 plot_id 的 accepted staged 仍正常纳入"

        prov_by_plot = {e["plot_id"]: e["provenance"] for e in proj["recent"]}
        assert prov_by_plot["p1"] == "canonical"
        assert prov_by_plot["p2"] == "accepted_staged"


def test_staged_strict_review_state_gating():
    """staged review state 严格 accepted 门禁测试（保守不纳入，不假设）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_staged_gate"
        book_dir = root / "books" / bid

        _write_json(book_dir / "staged_story_state.json", {
            "chapter_num": 1,
            "plot_deltas": [
                # 1. delta 显式 accepted -> 纳入
                {
                    "plot_id": "p_acc_direct",
                    "plot_name": "直接接受",
                    "chapter_num": 1,
                    "review": {"state": "accepted"},
                    "facts": {"choices_made": ["直接接受的事件"]},
                },
                # 2. delta 显式 rejected -> 排除
                {
                    "plot_id": "p_rej_direct",
                    "plot_name": "直接拒绝",
                    "chapter_num": 1,
                    "review": {"state": "rejected"},
                    "facts": {"choices_made": ["被拒的事件"]},
                },
                # 3. delta 显式 pending -> 排除
                {
                    "plot_id": "p_pending_direct",
                    "plot_name": "待审",
                    "chapter_num": 1,
                    "review_state": "pending",
                    "facts": {"choices_made": ["待审的事件"]},
                },
                # 4. delta 无 review，对应 draft_chapter bridge 且 bridge 为 accepted -> 纳入
                {
                    "plot_id": "p_bridge_acc",
                    "run_id": "run_01",
                    "plot_name": "Bridge 接受",
                    "chapter_num": 1,
                    "facts": {"choices_made": ["Bridge 接受事件"]},
                },
                # 5. delta 无 review，对应 draft_chapter bridge 但 bridge 为 rejected -> 排除
                {
                    "plot_id": "p_bridge_rej",
                    "run_id": "run_02",
                    "plot_name": "Bridge 拒绝",
                    "chapter_num": 1,
                    "facts": {"choices_made": ["Bridge 拒绝事件"]},
                },
                # 6. delta 无 review，无对应 bridge -> 数据结构不足，保守排除！
                {
                    "plot_id": "p_no_bridge_unreviewed",
                    "plot_name": "孤儿无审段",
                    "chapter_num": 1,
                    "facts": {"choices_made": ["不应被假设接受"]},
                },
                # 7. delta 无 review，bridge 的 run_id 不匹配 -> 排除
                {
                    "plot_id": "p_mismatched_run",
                    "run_id": "run_v2",
                    "plot_name": "版本不匹配",
                    "chapter_num": 1,
                    "facts": {"choices_made": ["旧版本不该借用新审"]},
                },
            ],
        })

        # 写入 draft_chapter.json 提供 bridge 审查状态
        _write_json(book_dir / "draft_chapter.json", {
            "chapter_num": 1,
            "bridges": [
                {
                    "plot_id": "p_bridge_acc",
                    "run_id": "run_01",
                    "review": {"state": "accepted"},
                },
                {
                    "plot_id": "p_bridge_rej",
                    "run_id": "run_02",
                    "review": {"state": "rejected"},
                },
                {
                    "plot_id": "p_mismatched_run",
                    "run_id": "run_v1",  # 与 delta 的 run_v2 不一致
                    "review": {"state": "accepted"},
                },
            ],
        })

        proj = fact_ledger.project(bid, root=root)
        pids = [e["plot_id"] for e in proj["recent"]]

        assert "p_acc_direct" in pids, "直接标注 accepted 必须纳入"
        assert "p_bridge_acc" in pids, "通过 bridge 证明 accepted 必须纳入"
        assert "p_rej_direct" not in pids, "rejected 绝不能进入 ledger"
        assert "p_pending_direct" not in pids, "pending 绝不能进入 ledger"
        assert "p_bridge_rej" not in pids, "bridge rejected 绝不能进入 ledger"
        assert "p_no_bridge_unreviewed" not in pids, "无 review/无 bridge 保守不纳入"
        assert "p_mismatched_run" not in pids, "run_id 不符保守不纳入"


def test_multi_version_deterministic_selection():
    """同 plot 多版本只留 accepted/current 最高版本。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_multiver"
        book_dir = root / "books" / bid

        # facts/plot/p1.json 包含多版本数组
        _write_json(book_dir / "facts" / "plot" / "p1.json", [
            {
                "plot_id": "p1",
                "plot_name": "草稿版",
                "draft_revision": 1,
                "review_state": "accepted",
                "facts": {"choices_made": ["第一版选择"]},
            },
            {
                "plot_id": "p1",
                "plot_name": "修改被拒版",
                "draft_revision": 2,
                "review_state": "rejected",
                "facts": {"choices_made": ["被拒的选择"]},
            },
            {
                "plot_id": "p1",
                "plot_name": "定稿版",
                "draft_revision": 3,
                "review_state": "accepted",
                "facts": {"choices_made": ["第三版定稿选择"]},
            },
        ])

        proj = fact_ledger.project(bid, root=root)
        assert len(proj["recent"]) == 1
        assert proj["recent"][0]["text"] == "第三版定稿选择"
        assert proj["recent"][0]["draft_revision"] == 3


def test_facts_deduplication_and_question_semantics():
    """安全去重（同 plot 去重，不同 plot 保留）；new_story_questions 保持近期提出问题。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_dedupe"
        book_dir = root / "books" / bid

        _write_json(book_dir / "facts" / "plot" / "p1.json", {
            "plot_id": "p1",
            "plot_name": "情节段1",
            "chapter_num": 1,
            "facts": {
                "choices_made": ["前往黑石塔", "前往黑石塔"],  # 同 plot 相同文本
                "information_revealed": ["塔底有封印"],
                "new_story_questions": ["封印何时破裂？"],
                "character_events": [
                    {
                        "name": "主角",
                        "events": [{"type": "location_shift", "to": "黑石塔"}],
                    },
                    {
                        "name": "主角",
                        "events": [{"type": "location_shift", "to": "黑石塔"}],  # 结构化相同
                    },
                ],
            },
        })

        _write_json(book_dir / "facts" / "plot" / "p2.json", {
            "plot_id": "p2",
            "plot_name": "情节段2",
            "chapter_num": 1,
            "facts": {
                "choices_made": ["前往黑石塔"],  # 不同 plot 相同文本，必须保留
                "new_story_questions": ["谁动了守卫？"],
            },
        })

        proj = fact_ledger.project(bid, root=root)
        events = proj["recent"]

        # 检查同 plot 去重
        p1_choices = [e for e in events if e["plot_id"] == "p1" and e["category"] == "choices_made"]
        assert len(p1_choices) == 1, "同 plot 重复文本已去重"
        assert p1_choices[0]["text"] == "前往黑石塔"

        p1_char = [e for e in events if e["plot_id"] == "p1" and e["category"] == "character_events"]
        assert len(p1_char) == 1, "同 plot 结构化重复事件已去重"

        # 检查跨 plot 保留相同文本
        p2_choices = [e for e in events if e["plot_id"] == "p2" and e["category"] == "choices_made"]
        assert len(p2_choices) == 1
        assert p2_choices[0]["text"] == "前往黑石塔", "不同 plot 的相同文本必须保留"

        # 检查 new_story_questions 语义
        questions = [e for e in events if e["category"] == "new_story_questions"]
        assert len(questions) == 2
        for q in questions:
            assert q["label"] == "近期提出问题"
            assert q["status"] == "recent_question"
            assert "open" not in q["status"] and "resolved" not in q["status"], "不判 open/resolved"

        # 普通 facts 只按历史事件展示
        other_events = [e for e in events if e["category"] != "new_story_questions"]
        for oe in other_events:
            assert oe["status"] == "historical"

        # active 投影故意为空
        assert proj["active"] == []


def test_entry_json_shape():
    """验证 entry 格式完全匹配协调者与 UI 要求：
    {category, label, text, plot_id, plot_name, chapter_num, status, provenance, staged}。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_shape"
        book_dir = root / "books" / bid

        _write_json(book_dir / "facts" / "plot" / "p1.json", {
            "plot_id": "p1",
            "plot_name": "测试形状",
            "chapter_num": 2,
            "run_id": "p1@1",
            "facts": {
                "choices_made": ["决定深入地下通道"],
                "resource_changes": ["消耗照明符文一枚"],
                "relationship_changes": [{"name": "同伴A", "relation": "结为生死之交"}],
                "new_story_questions": ["地下深处的哭声来自何处？"],
            },
        })

        proj = fact_ledger.project(bid, root=root)
        assert len(proj["recent"]) == 4

        required_keys = {
            "category", "label", "text", "plot_id", "plot_name",
            "chapter_num", "status", "provenance", "staged"
        }
        for item in proj["recent"]:
            missing = required_keys - set(item.keys())
            assert not missing, f"缺少必要字段: {missing}"
            assert isinstance(item["category"], str)
            assert isinstance(item["label"], str)
            assert isinstance(item["text"], str) and len(item["text"]) > 0
            assert isinstance(item["plot_id"], str)
            assert isinstance(item["plot_name"], str)
            assert isinstance(item["chapter_num"], int)
            assert isinstance(item["status"], str)
            assert item["provenance"] == "canonical"
            assert item["staged"] is False


def test_bounded_projection_and_pagination():
    """有界投影与游标分页测试。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_paging"
        book_dir = root / "books" / bid

        # 写入 50 条 choices_made
        choices = [f"历史行动_{i:02d}" for i in range(50)]
        _write_json(book_dir / "facts" / "plot" / "p_bulk.json", {
            "plot_id": "p_bulk",
            "plot_name": "批量大段",
            "chapter_num": 1,
            "facts": {"choices_made": choices},
        })

        # 1. 默认 project 有界截断
        proj = fact_ledger.project(bid, recent_limit=15, root=root)
        assert len(proj["recent"]) == 15
        assert proj["archived_count"] == 35
        assert proj["truncated"] is True
        assert proj["history_cursor"] is not None
        assert isinstance(proj["revision"], str)

        # 2. page 分页查询
        p1 = fact_ledger.page(bid, cursor=None, limit=20, root=root)
        assert len(p1["recent"]) == 20
        assert p1["archived_count"] == 30
        assert p1["truncated"] is True
        c1 = p1["history_cursor"]
        assert c1 is not None

        p2 = fact_ledger.page(bid, cursor=c1, limit=20, root=root)
        assert len(p2["recent"]) == 20
        assert p2["archived_count"] == 10
        assert p2["truncated"] is True
        c2 = p2["history_cursor"]
        assert c2 is not None

        p3 = fact_ledger.page(bid, cursor=c2, limit=20, root=root)
        assert len(p3["recent"]) == 10
        assert p3["archived_count"] == 0
        assert p3["truncated"] is False
        assert p3["history_cursor"] is None

        # 保证连续三页无交集且拼接等于全量 50 条
        all_texts = [e["text"] for e in p1["recent"]] + [e["text"] for e in p2["recent"]] + [e["text"] for e in p3["recent"]]
        assert len(all_texts) == 50
        assert len(set(all_texts)) == 50

        # 3. 游标防伪/过期测试：revision 不匹配时抛出 ValueError
        bad_cursor = fact_ledger._encode_cursor("outdated_revision_hash", 10)
        try:
            fact_ledger.page(bid, cursor=bad_cursor, root=root)
            raise AssertionError("过期 cursor 应当被拒绝")
        except ValueError as exc:
            assert "history cursor 已过期" in str(exc)


def test_cache_fingerprint_and_invalidation():
    """文件缓存命中与修改后自动失效。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bid = "test_cache"
        book_dir = root / "books" / bid
        plot_path = book_dir / "facts" / "plot" / "p1.json"

        _write_json(plot_path, {
            "plot_id": "p1",
            "plot_name": "初版",
            "chapter_num": 1,
            "facts": {"choices_made": ["第一次选择"]},
        })

        # 首次读取
        s1 = fact_ledger.load_fact_sources(bid, root=root)
        assert s1["canonical"][0]["facts"]["choices_made"] == ["第一次选择"]
        rev1 = s1["revision"]

        # 二次读取命中缓存
        s2 = fact_ledger.load_fact_sources(bid, root=root)
        assert s2["revision"] == rev1

        # 修改文件内容与 mtime
        time.sleep(0.01)  # 保证 mtime 发生变化
        _write_json(plot_path, {
            "plot_id": "p1",
            "plot_name": "修改版",
            "chapter_num": 1,
            "facts": {"choices_made": ["修改后的选择"]},
        })

        s3 = fact_ledger.load_fact_sources(bid, root=root)
        assert s3["revision"] != rev1, "文件变化后 revision 指纹必须刷新"
        assert s3["canonical"][0]["facts"]["choices_made"] == ["修改后的选择"]


def test_real_book_003_if_present():
    """若存在真实书 book_003，测试实盘数据读取与格式规范。"""
    book_dir = ROOT / "books" / "book_003"
    if not (book_dir / "facts" / "plot").exists():
        print("[SKIP] book_003 canonical facts 不存在，跳过实盘测试")
        return

    proj = fact_ledger.project("book_003")
    assert isinstance(proj["revision"], str)
    assert proj["active"] == []
    assert len(proj["recent"]) > 0
    assert proj["provenance"]["sources"] == ["canonical"]

    sample = proj["recent"][0]
    for key in ("category", "label", "text", "plot_id", "plot_name", "chapter_num", "status", "provenance", "staged"):
        assert key in sample, f"真实数据缺少 {key}"

    assert sample["status"] in ("historical", "recent_question")
    assert sample["staged"] is False
    print(f"[OK] 真实书目 book_003 读取通过：事件总量 >={len(proj['recent'])}，revision={proj['revision']}")


def main():
    test_canonical_priority_and_legacy_fallback()
    print("[OK] canonical 优先与 legacy_memory 兜底")

    test_canonical_ignores_staged_for_same_plot_id()
    print("[OK] canonical 与 staged 同 plot_id 确定性覆盖")

    test_staged_strict_review_state_gating()
    print("[OK] staged 严格 review_state=accepted 门禁与保守排除")

    test_multi_version_deterministic_selection()
    print("[OK] 同 plot 多版本确定性选择")

    test_facts_deduplication_and_question_semantics()
    print("[OK] 安全去重与近期提出问题语义")

    test_entry_json_shape()
    print("[OK] entry JSON shape {category,label,text,plot_id,plot_name,chapter_num,status,provenance,staged}")

    test_bounded_projection_and_pagination()
    print("[OK] 有界投影与 cursor 翻页/防伪")

    test_cache_fingerprint_and_invalidation()
    print("[OK] 缓存命中与指纹自动失效")

    test_real_book_003_if_present()
    print("\n[ALL PASSED] fact_ledger 全部测试通过！")


if __name__ == "__main__":
    main()
