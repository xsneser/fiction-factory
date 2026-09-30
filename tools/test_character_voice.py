# -*- coding: utf-8 -*-
"""人物语音改造验收（2026-09-11）。纯规则、无 LLM、不落盘。

要保护的不变量：
1. **不出现「口头禅」字样**，也不把 catchphrase 当每次必说的台词注入；
2. 存量空 `speech_profile` 的角色能拿到**派生 voice basis**（不迁移旧书也能立即获益），
   且派生内容**不含固定台词**；
3. 关系态语音：同一个人对不同对象有不同档位；
4. 标志短语是**可选**的稀疏特征（不要求人人有）；
5. `habits` 里塞字面台词会被**提示**（不阻断）；
6. 短语重复检测**不做 speaker 归因**，且不把「报告」这类高频普通词当口癖。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    import agent_tools as at
    from libraries.continuity import ContinuityChecker
    from libraries.storyline import _norm_speech_profile, signature_phrases_of, \
        voice_keys_with_content

    # ── 1. signature_phrases 归一：完全可选、频次只降不升 ──
    sp = _norm_speech_profile({
        "rhythm": "短句", "logic": "算账", "emotion": "紧张少话", "social_register": "对下属用职务",
        "signature_phrases": [{"text": "情况是这样", "frequency": "rare", "contexts": ["部署"]},
                              {"text": "按规程来", "frequency": "BOGUS"},   # 非法 → rare
                              "数据不说谎",                                  # 字符串简写
                              {"text": "情况是这样"},                        # 重名去重
                              {"frequency": "often"}],                       # 空 text → 丢
    })
    assert len(sp["signature_phrases"]) == 3, sp["signature_phrases"]
    assert sp["signature_phrases"][1]["frequency"] == "rare"
    assert voice_keys_with_content(sp) == ["rhythm", "logic", "emotion", "social_register"]
    assert voice_keys_with_content(_norm_speech_profile({})) == []
    # catchphrase 降级为 rare 一条；没有 catchphrase 也不算错
    assert signature_phrases_of({"catchphrase": "X"})[0] == {
        "text": "X", "frequency": "rare", "contexts": []}
    assert signature_phrases_of({}) == []
    print("[1] signature_phrases 归一（可选 + 只降不升）OK")

    # ── 2. 空 speech_profile 的存量角色 → 派生 voice basis ──
    shen = {"name": "沈炽", "role": "主角", "personality": "沉着，先算后打",
            "catchphrase": "情况是这样",
            "behavior": {"communication_style": {"stranger": "公事公办", "friend": "话不多"},
                         "decision_style": {"under_pressure": "把恐慌摊上桌量化"}},
            "relations": [{"name": "周砺", "relation": "搭档、路线分歧者"}]}
    s = at._speech_compact(shen)
    assert "口头禅" not in s, s
    assert "派生" in s and "情况是这样" in s, s        # 既有设定推出来的 + 稀疏标志短语
    assert "沉着" in s, s                              # 真的用上了已有的人设
    # 有 speech_profile → 用生成规律四键，不重复渲染成台词表
    full = dict(shen, speech_profile=sp)
    s2 = at._speech_compact(full)
    assert "口头禅" not in s2 and "判断习惯" in s2 and "稀疏" in s2, s2
    # 两者皆无 → voice_missing（不假装有语音）
    assert at._speech_compact({"name": "路人"}) == "voice_missing"
    print("[2] voice basis 兜底 / 生成规律渲染 OK")

    # ── 3. 关系档位（用 book_002 的真实关系串做夹具）──
    def relmode(relation):
        return at._relationship_of({"name": "乙", "relations": [{"name": "甲", "relation": relation}]},
                                   "甲")["relationship_mode"]
    for text, want in (("老部下、最硬的一条胳膊", "subordinate"),
                       ("搭档/路线分歧到和解", "old_comrade"),
                       ("技术搭档/彼此最信任的战友", "old_comrade"),
                       ("垂涎其技术的帝国对手", "adversary"),   # 同时含「技术」与「对手」→ 必须先判敌对
                       ("所救的旧文明遗民/引路人", "neutral"),
                       ("搭档、路线分歧者", "old_comrade")):
        assert relmode(text) == want, (text, relmode(text), want)
    assert at._relationship_of({"name": "甲"}, "甲")["relationship_mode"] == "neutral"

    class _CS:
        events = [{"type": "trust_change", "to": "信任加深"},
                  {"type": "note", "to": "不该被当成关系张力"}]

    class _CSM:
        def get(self, _n):
            return _CS()
    assert at._relationship_of({"name": "乙"}, "甲", _CSM())["current_tension"] == "信任加深"
    print("[3] 关系档位 + current_tension（只认关系向事件）OK")

    # ── 4. 语音提示：不阻断、不要求 signature_phrases ──
    notes = at._character_voice_notes([shen, full])
    assert all(n["severity"] == "info" for n in notes), notes
    assert any(n["location"] == "沈炽" for n in notes), notes
    # 只给了 signature_phrases、没给生成规律的：仍提示补「怎么说话」
    only_phrase = {"name": "甲", "role": "配角",
                   "speech_profile": {"signature_phrases": [{"text": "随你便", "frequency": "rare"}]}}
    n2 = at._character_voice_notes([only_phrase])
    assert any("语言生成规律" in n["description"] for n in n2), n2
    assert not any("标志短语" in n["description"] for n in n2), "不得因缺/有标志短语而报警"
    # habits 塞字面台词 → 提示改法
    n3 = at._character_voice_notes([{"name": "甲", "role": "配角",
                                     "speech_profile": {"rhythm": "短句", "logic": "算账",
                                                        "habits": ["下命令前先说“情况是这样”"]}}])
    assert any("habits" in n["description"] for n in n3), n3
    print("[4] 语音提示（提示级 + 不强制标志短语）OK")

    # ── 5. 短语重复检测：不做 speaker 归因，长度 ≥4 ──
    ck = ContinuityChecker()
    chars = [{"name": "沈炽", "speech_profile": {"signature_phrases": ["情况是这样"]}},
             {"name": "方铁柱", "catchphrase": "报告"}]
    chapters = [{"num": 5, "content": "情况是这样。又是情况是这样。"},
                {"num": 6, "content": "情况是这样，他说。"},
                {"num": 7, "content": "报告！报告！"}]
    iss = ck.check_signature_phrase_repeat(chapters, chars)
    assert len(iss) == 1, iss
    assert "情况是这样" in iss[0]["description"]
    assert "说话人" not in iss[0]["description"]
    assert "报告" not in iss[0]["description"], "未登记/长度<4 的高频普通词不得进规则"
    assert iss[0]["severity"] == "warning"
    print("[5] 短语重复检测（全局短语级、长度≥4）OK")

    # ── 6. 检测器真的接进了 check_all（不只是存在）──
    from types import SimpleNamespace
    tl = SimpleNamespace(basic_info={"characters": chars}, promises=[])
    report = ck.check_all(tl, chapters, current_chapter=7, char_states=None)
    assert "signature_phrase_repeat" in report["checks"], report["checks"].keys()
    assert report["checks"]["signature_phrase_repeat"]["status"] == "warn", report["checks"]
    assert any(i["category"] == "signature_phrase_repeat" for i in report["issues"]), report["issues"]
    # 没有重复时是 ok，不虚报
    clean = ck.check_all(tl, [{"num": 1, "content": "无事发生。"}], current_chapter=1,
                         char_states=None)
    assert clean["checks"]["signature_phrase_repeat"]["status"] == "ok", clean["checks"]
    print("[6] 已接入 check_all（有重复 warn / 无重复 ok）OK")

    print()
    print("人物语音：全部通过")


if __name__ == "__main__":
    main()
