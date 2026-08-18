# -*- coding: utf-8 -*-
"""角色统一 characters 数组 + 角色原型库 单元测试（独立可跑：python tools/test_characters.py）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from types import SimpleNamespace
from libraries.storyline import (
    normalize_basic_info, get_characters, get_mc, relation_to_mc, merge_basic_info,
    BookStoryline,
)
from libraries.prompt_harness import PromptHarness
from libraries.engine import NovelEngine
from libraries.character import CharacterLibrary

FAIL = []


def check(name, cond, detail=""):
    print(("✅ " if cond else "❌ ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ─── 旧结构 → characters 迁移 ───
OLD_BI = {
    "protagonist": {"name": "萧晨", "identity": "赘婿", "personality": "隐忍坚韧",
                    "background": "前世程序员", "golden_finger": "打工人系统",
                    "gender": "男", "age": 25, "death_year": 0},
    "supporting_cast": [
        {"name": "李哥", "role": "主管", "relation": "同事", "gender": "男",
         "title": "李主管", "personality": "精明", "catchphrase": "这破公司", "brief": "吝啬上级"},
        {"name": "王五", "role": "师弟", "relation": "师弟", "gender": "男",
         "title": "", "personality": "热血", "catchphrase": "", "brief": ""},
    ],
    "world_building": {"era": "灵气复苏"},
}
HAND_CHARS = [
    {"name": "萧晨", "role": "主角", "identity": "赘婿", "gender": "男",
     "personality": "隐忍坚韧", "catchphrase": "", "brief": "前世程序员", "title": "",
     "golden_finger": "打工人系统", "age": 25, "death_year": 0,
     "archetype_id": "", "relations": []},
    {"name": "李哥", "role": "配角", "identity": "主管", "gender": "男",
     "personality": "精明", "catchphrase": "这破公司", "brief": "吝啬上级", "title": "李主管",
     "golden_finger": "", "age": 0, "death_year": 0,
     "archetype_id": "", "relations": [{"name": "萧晨", "relation": "同事"}]},
    {"name": "王五", "role": "配角", "identity": "师弟", "gender": "男",
     "personality": "热血", "catchphrase": "", "brief": "", "title": "",
     "golden_finger": "", "age": 0, "death_year": 0,
     "archetype_id": "", "relations": [{"name": "萧晨", "relation": "师弟"}]},
]


def test_migration():
    bi = normalize_basic_info(dict(OLD_BI))
    check("迁移: characters 数量", len(bi["characters"]) == 3, str(len(bi["characters"])))
    check("迁移: 旧键已删", "protagonist" not in bi and "supporting_cast" not in bi)
    check("迁移: 与手工 characters 等价", bi["characters"] == HAND_CHARS,
          "normalize 结果与手写 characters 不一致")
    # 幂等
    check("迁移: 幂等", normalize_basic_info(dict(bi)) == bi)


def test_get_mc_and_relation():
    bi = normalize_basic_info(dict(OLD_BI))
    mc = get_mc(bi)
    check("get_mc: 返回主角", mc.get("name") == "萧晨")
    check("get_mc: 无主角书返回 {}（严格）", get_mc({"characters": [{"name": "路人", "role": "配角"}]}) == {})
    check("relation_to_mc: 李哥→同事", relation_to_mc(bi["characters"][1], bi) == "同事")
    check("get_characters: 旧键兜底", len(get_characters(OLD_BI)) == 3)


def test_merge():
    existing = normalize_basic_info(dict(OLD_BI))
    gen = {"protagonist": {"name": "萧晨", "identity": "", "personality": "狂拽",
                           "background": "", "golden_finger": "", "gender": "", "age": 0, "death_year": 0},
           "supporting_cast": [{"name": "王五", "role": "师弟", "relation": "师弟"}],
           "world_building": {"era": "灵气复苏"}}
    m = merge_basic_info(existing, gen)
    names = [c["name"] for c in m["characters"]]
    check("merge: MC 保留用户非空 identity", m["characters"][0]["identity"] == "赘婿")
    check("merge: 非MC 现有优先（李哥在、王五不覆盖新增）", "李哥" in names and "王五" in names)
    check("merge: 生成空非MC→保留 existing", len([c for c in m["characters"] if c["name"] == "李哥"]) == 1)
    # 空 existing 非 MC → generated 填入
    m2 = merge_basic_info({"characters": [{"name": "萧晨", "role": "主角"}]}, gen)
    check("merge: 空非MC→generated 填入王五", "王五" in [c["name"] for c in m2["characters"]])


def test_bible_equivalence():
    """同一逻辑内容，旧形态 vs characters 形态 → bible / roles 渲染逐字符相等。"""
    def tl_for(bi):
        return BookStoryline.from_dict({"book_title": "测试", "genre": "都市",
                                        "basic_info": bi})
    old_tl = tl_for(dict(OLD_BI))
    new_tl = tl_for({"characters": HAND_CHARS, "world_building": {"era": "灵气复苏"}})
    h_old = PromptHarness(storyline=old_tl)
    h_new = PromptHarness(storyline=new_tl)
    check("bible 等价", h_old.build_book_bible() == h_new.build_book_bible())
    check("bible_condensed 等价", h_old.build_book_bible_condensed() == h_new.build_book_bible_condensed())

    # _roles_block：构造一个带 roles 的桥段
    from libraries.storyline import PlotSlot
    p_old = PlotSlot(id="p1", template_id="t", name="打脸", roles=["萧晨", "李哥"])
    p_new = PlotSlot(id="p1", template_id="t", name="打脸", roles=["萧晨", "李哥"])
    check("roles_block 等价", h_old._roles_block(p_old) == h_new._roles_block(p_new))

    # engine 注册等价
    class FakeStates:
        def __init__(self): self.regs = []
        def register(self, name, **kw): self.regs.append((name, kw))
    fake_old = SimpleNamespace(char_states=FakeStates())
    fake_new = SimpleNamespace(char_states=FakeStates())
    NovelEngine._register_storyline_characters(fake_old, old_tl)
    NovelEngine._register_storyline_characters(fake_new, new_tl)
    check("角色状态机注册等价", fake_old.char_states.regs == fake_new.char_states.regs,
          str(fake_old.char_states.regs) + " != " + str(fake_new.char_states.regs))


def test_character_library():
    lib = CharacterLibrary()
    check("角色库: 内置原型非空", len(lib.archetypes) >= 8, str(len(lib.archetypes)))
    check("角色库: 包含高冷毒舌", lib.get_by_id("char_001") is not None)
    check("角色库: categories 含性格/流派", "高冷" in lib.categories() or "玄幻" in lib.categories())
    check("角色库: search 按流派", len(lib.search(genre="玄幻")) > 0)
    check("角色库: search 按关键词", len(lib.search(kw="腹黑")) > 0)


if __name__ == "__main__":
    test_migration()
    test_get_mc_and_relation()
    test_merge()
    test_bible_equivalence()
    test_character_library()
    print("\n结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌")
    sys.exit(1 if FAIL else 0)
