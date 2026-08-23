#!/usr/bin/env python3
"""
NovelEngine 集成测试
模拟完整链路：创建新书 → 匹配模板 → 构建 prompt → 审查 → 去AI味 → 引擎路由
（不调用 LLM API，验证所有模块逻辑正确性）
"""
import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(__file__))

# Windows 控制台默认 GBK，直接 print 中文/emoji 会崩，强制走 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TMP_DIR = tempfile.gettempdir()

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.engine import NovelEngine, Op, BookMode, Phase


def test_print(phase, status="OK", detail=""):
    icon = "✅" if status == "OK" else "⚠️" if status == "WARN" else "❌"
    print(f"  {icon} {phase:<30s} {detail}")

errors = []
passed = 0
total = 0


def assert_ok(test_name, condition, detail=""):
    global passed, total
    total += 1
    if condition:
        passed += 1
        test_print(test_name, "OK", detail)
    else:
        errors.append(f"{test_name}: {detail}")
        test_print(test_name, "FAIL", detail)


# ══════════════════════════════════════════════
#  Phase 1: 四大库
# ══════════════════════════════════════════════
print("\n═══ Phase 1: 四大核心库 ═══")

plot = PlotLibrary()
assert_ok("桥段库-数量", len(plot.templates) >= 12, f"{len(plot.templates)} 模板")
assert_ok("桥段库-分类", len(plot.categories()) >= 6, f"{len(plot.categories())} 分类")
assert_ok("桥段库-搜索", len(plot.search(category="开篇")) >= 2)
assert_ok("桥段库-匹配", len(plot.match_for_chapter("主角在家族大会上被退婚，当众打脸立威", genre="爽文")) > 0)

struct = StructureLibrary()
assert_ok("大纲库-数量", len(struct.templates) >= 5)
assert_ok("大纲库-搜索", len(struct.search(genre="玄幻")) >= 1)

gag = GagLibrary()
assert_ok("笑点库-数量", len(gag.patterns) >= 10, f"{len(gag.patterns)} 模式")
assert_ok("笑点库-搜索", len(gag.search(scene="日常")) > 0)

# 内涵跟随 = 免费规则 THEME_PLOT_COMPAT（theme_lib 移除后的唯一数据源）
from libraries.storyline import PlotSlot as _PS, mount_themes_and_hooks as _mth, THEME_PLOT_COMPAT as _tpc
assert_ok("内涵-映射非空", len(_tpc) >= 6, f"{len(_tpc)} 个桥段模板")
_pc = _PS(id="c", template_id="plot_dating_001", name="退婚", category="爽文", outline_id="o", stage_index=0)
_mth(_pc, ["公平（Justice）", "牺牲（Sacrifice）"])
assert_ok("内涵-兼容桥段可挂", _pc.theme_hints == ["公平（Justice）"], str(_pc.theme_hints))
_pp = _PS(id="d", template_id="plot_dating_007", name="擂台", category="战斗", outline_id="o", stage_index=0)
_mth(_pp, ["公平（Justice）"])
assert_ok("内涵-不兼容桥段不挂", _pp.theme_hints == [], str(_pp.theme_hints))

# ══════════════════════════════════════════════
#  Phase 2: 笔名档案 + 图书管理
# ══════════════════════════════════════════════
print("\n═══ Phase 2: 笔名档案 + 图书管理 ═══")

pm = ProfileManager("profiles")
assert_ok("档案-预设笔名", len(pm.list_all()) >= 3)

profile = pm.get_by_name("枫落")
assert_ok("档案-查找笔名", profile is not None, "枫落")
assert_ok("档案-风格约束", len(profile.build_style_prompt()) > 100)

bm = BookManager("books")
# 清理残留：只删本测试曾创建的《系统修仙录》（title 唯一标识），绝不碰真实书
for b in bm.list_all():
    if b.title == "系统修仙录":
        bm.delete(b.book_id)

cfg = bm.create("系统修仙录", "枫落", genre="玄幻", sub_genre="系统流",
                structure_template_id="struct_xuanhuan_01",
                style_profile_id=profile.id)
assert_ok("图书-创建", cfg.book_id.startswith("book_"))

bm.save_chapter(cfg.book_id, 1, "第一章", "测试正文内容")
ch = bm.load_chapter(cfg.book_id, 1)
assert_ok("图书-章节", ch is not None and ch["title"] == "第一章")

bm.save_outline(cfg.book_id, {"structure": "struct_xuanhuan_01"})
assert_ok("图书-大纲", bm.get_outline(cfg.book_id) is not None)

# ══════════════════════════════════════════════
#  Phase 3: 写作核心统一（桥段写作 + 书名简介 + 开场模式，无 LLM）
# ══════════════════════════════════════════════
print("\n═══ Phase 3: 写作核心统一（无 LLM）═══")

from libraries.book_meta import build_title_prompt, build_synopsis_prompt, platform_constraints
from libraries.storyline_writer import opening_mode_active
from libraries.prompt_harness import PromptHarness
from libraries.storyline import OutlineSlot, PlotSlot

title_p = build_title_prompt("玄幻", "系统流", "fanqie", "正文占位" * 50)
assert_ok("书名-含题材", "玄幻" in title_p and "系统流" in title_p)
assert_ok("书名-含平台", "fanqie" in title_p)
syn_p = build_synopsis_prompt("都市", "重生", "fanqie", "正文占位" * 50)
assert_ok("简介-含题材", "都市" in syn_p and "重生" in syn_p)
pc = platform_constraints("fanqie")
assert_ok("平台-番茄约束", "开篇前 500 字必须有冲突或危机" in pc)
assert_ok("平台-未知平台", platform_constraints("xxx") == "")

assert_ok("开场-第1章前3桥段内", opening_mode_active(1, 0, 0) is True)
assert_ok("开场-超800字关闭", opening_mode_active(1, 800, 0) is False)
assert_ok("开场-超3桥段关闭", opening_mode_active(1, 0, 3) is False)
assert_ok("开场-非第1章关闭", opening_mode_active(2, 0, 0) is False)

h = PromptHarness(storyline=None, profile=None)
_o = OutlineSlot(id="o1", template_id="struct_urban_01", name="开篇",
                 start_chapter=1, end_chapter=3, stages=[{"name": "开局", "events": ["x"]}])
_p = PlotSlot(id="p1", template_id="plot_dating_011", name="开篇桥段", category="开篇",
              outline_id="o1", stage_index=0)
_item = {"outline": _o, "stage": {"name": "开局", "events": ["x"]}, "plot": _p}
open_p = h.render_bridge_prompt(_item, "", "", "", 300, is_opening=True)
assert_ok("开场-注入铁律", "开场模式" in open_p)
normal_p = h.render_bridge_prompt(_item, "", "", "", 300, is_opening=False)
assert_ok("非开场-不含铁律", "开场模式" not in normal_p)

# ══════════════════════════════════════════════
#  Phase 3.5: 线程穿插 + 桥段拆分（无 LLM）
# ══════════════════════════════════════════════
print("\n═══ Phase 3.5: 线程穿插 + 桥段拆分（无 LLM）═══")

from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
from libraries.storyline_writer import StorylineChapterWriter
from libraries.outline_generator import OutlineGenerator

_p0 = PlotSlot(id="x", template_id="t", name="n")
assert_ok("线程-缺省主线", _p0.thread_id == "主线" and _p0.resolves_plot_id == "")
_slA = BookStoryline(); _slA.plots = [_p0]
assert_ok("线程-往返一致", BookStoryline.from_dict(_slA.to_dict()).plots[0].thread_id == "主线")
_slA2 = BookStoryline.from_dict({"plots": [{"id": "y", "template_id": "t", "name": "n"}]})
assert_ok("线程-旧数据兼容", _slA2.plots[0].thread_id == "主线")

# 线程轮流排序（主线2:1；数据刻意按"先主线后副线"构造，穿插后顺序改变）
_slB = BookStoryline()
_oB = OutlineSlot(id="o1", template_id="t", name="弧", start_chapter=1, end_chapter=30,
                  stages=[{"name": "s%d" % i, "events": ["e"]} for i in range(6)])
_slB.outlines = [_oB]
def _mk(pid, st, order, tid):
    return PlotSlot(id=pid, template_id="t", name=pid, outline_id="o1", stage_index=st, order=order, thread_id=tid)
_slB.plots = [
    _mk("M1",0,0,"主线"),_mk("M2",0,1,"主线"),_mk("M3",1,0,"主线"),_mk("M4",1,1,"主线"),
    _mk("M5",2,0,"主线"),_mk("M6",2,1,"主线"),
    _mk("S1",3,0,"副线"),_mk("S2",3,1,"副线"),_mk("S3",4,0,"副线"),
    _mk("V1",4,1,"伏笔"),_mk("V2",5,0,"伏笔"),_mk("V3",5,1,"伏笔"),
]
_wB = StorylineChapterWriter(storyline=_slB)
_seqB = [p.id for p in _wB._threaded_ordered_plots()]
assert_ok("线程-轮流前7", _seqB[:7] == ["M1","M2","S1","V1","M3","M4","S2"], " ".join(_seqB[:7]))
for p in _slB.plots: p.thread_id = "主线"
_strict = [p.id for p in sorted(_slB.plots, key=lambda p: (0, p.stage_index, p.order))]
_seqB2 = [p.id for p in _wB._threaded_ordered_plots()]
assert_ok("线程-全主线=旧严格顺序", _seqB2 == _strict, " ".join(_seqB2))

# 收局创建（设局→收局两槽位）
_genB = OutlineGenerator(llm_client=None)
_setupB = PlotSlot(id="setup1", template_id="t", name="阴谋·设局", category="悬疑",
                   outline_id="o1", stage_index=0, order=0)
_slC = BookStoryline(); _slC.outlines = [_oB]; _slC.plots = [_setupB]
_evtsB = list(_genB._apply_split_payoffs(
    _slC, [{"plot_id": "setup1", "payoff_after_stage": 2, "payoff_name": "阴谋·收局"}]))
_payoffsB = [p for p in _slC.plots if p.resolves_plot_id]
assert_ok("线程-收局创建", len(_payoffsB) == 1, str(len(_payoffsB)))
assert_ok("线程-收局晚于设局", _payoffsB[0].stage_index > _setupB.stage_index, str(_payoffsB[0].stage_index))
assert_ok("线程-收局 plot_added 事件", len(_evtsB) == 1 and _evtsB[0][0] == "plot_added")

assert_ok("线程-分类兜底",
          _genB._default_thread_for_category("悬疑") == "伏笔阴谋线"
          and _genB._default_thread_for_category("情感") == "副线"
          and _genB._default_thread_for_category("爽文") == "主线")

# ══════════════════════════════════════════════
#  Phase 3.6: 叙事纪律 + 角色档案（无 LLM）
# ══════════════════════════════════════════════
print("\n═══ Phase 3.6: 叙事纪律 + 角色档案（无 LLM）═══")

from libraries.character_state import CharacterStateMachine
from libraries.storyline import annotate_plot_roles
from libraries.prompt_harness import PromptHarness
from libraries.storyline_writer import has_repeated_token

_csm = CharacterStateMachine()
_csm.register("李哥", "同事", gender="男", personality="老油条",
              catchphrase="这破公司", brief="工位老同事")
_ctx = _csm.build_context_prompt()
assert_ok("角色-注册性别性格", "性别：男" in _ctx and "性格：老油条" in _ctx)
assert_ok("角色-惯用语句", "这破公司" in _ctx)
import json as _json
_csm2 = CharacterStateMachine.from_dict(_json.loads(_json.dumps(_csm.to_dict())))
assert_ok("角色-序列化往返", _csm2.get("李哥").gender == "男" and _csm2.get("李哥").catchphrase == "这破公司")

_slR = BookStoryline()
_slR.plots = [PlotSlot(id="p1", template_id="t", name="退婚", outline_id="o1", stage_index=0, order=0, roles=["陈默", "赵婶"])]
assert_ok("roles-往返", BookStoryline.from_dict(_slR.to_dict()).plots[0].roles == ["陈默", "赵婶"])

_slA = BookStoryline()
_slA.basic_info = {"protagonist": {"name": "陈默"},
                   "supporting_cast": [{"name": "李哥"}, {"name": "赵婶"}, {"name": "周磊"}]}
_oA = OutlineSlot(id="o1", template_id="t", name="弧", start_chapter=1, end_chapter=30,
                  stages=[{"name": "开局", "events": ["李哥堵门", "退婚"]}])
_slA.outlines = [_oA]
_slA.plots = [PlotSlot(id="a", template_id="t", name="退婚", outline_id="o1", stage_index=0, order=0,
                       slots=[{"name": "对象", "default": "赵婶"}]),
              PlotSlot(id="b", template_id="t", name="职场", outline_id="o1", stage_index=0, order=1)]
annotate_plot_roles(_slA)
assert_ok("annotate-主角恒首", _slA.plots[0].roles[0] == "陈默")
assert_ok("annotate-配角按名命中", "赵婶" in _slA.plots[0].roles and "李哥" in _slA.plots[0].roles)
assert_ok("annotate-无关缺席", "周磊" not in _slA.plots[0].roles and "周磊" not in _slA.plots[1].roles)

assert_ok("重复词-底下底下", has_repeated_token("底下底下弹出一条灰字") is True)
assert_ok("重复词-正常", has_repeated_token("他猛地站起来") is False)
assert_ok("重复词-哈哈哈放行", has_repeated_token("哈哈哈，你逗我") is False)
assert_ok("重复词-的的的", has_repeated_token("的的的") is True)

_slBible = BookStoryline()
_slBible.basic_info = {"protagonist": {"name": "陈默", "identity": "重生程序员"},
                       "world_building": {"era": "现代都市 2008", "power_system": "系统"},
                       "supporting_cast": [{"name": "李哥", "gender": "男", "title": "李哥", "personality": "老油条", "catchphrase": "这破公司"}],
                       "pov": "第三人称", "era_language": ""}
_hB = PromptHarness(storyline=_slBible, profile=None)
_cond = _hB.build_book_bible_condensed()
assert_ok("bible-视角", "视角：第三人称" in _cond and "禁止" in _cond)
assert_ok("bible-时代语言", "搭子" in _cond and "内卷" in _cond)
_full = _hB.build_book_bible()
assert_ok("bible-配角性别口头禅", "男" in _full and "这破公司" in _full)

_sw = _genB._validate_storyline_math({"protagonist": {"name": "陈默", "death_year": 2010, "age": 25},
                                     "world_building": {"era": "现代都市 2015"}})
assert_ok("故事线-矛盾警告", any("重生故事线矛盾" in w for w in _sw))
_sw2 = _genB._validate_storyline_math({"protagonist": {"name": "陈默", "age": 25}, "world_building": {"era": "2008年"}})
assert_ok("故事线-自洽无警告", not any("矛盾" in w or "不自洽" in w for w in _sw2))

# ══════════════════════════════════════════════
#  Phase 4: 成本追踪
# ══════════════════════════════════════════════
print("\n═══ Phase 4: 成本追踪 ═══")

tracker = CostTracker(budget=50.0, model="deepseek-chat")
assert_ok("成本-初始余额", tracker.remaining() == 50.0)

cost_est = tracker.estimate("测试输入文本" * 100, 4096)
assert_ok("成本-预估", cost_est > 0, f"¥{cost_est}")

ok = tracker.check_budget("测试" * 1000, 4096)
assert_ok("成本-预算门控-pass", ok)

tracker.record("chapter_draft", "输入" * 500, output_tokens=3000)
assert_ok("成本-记录", tracker.spent > 0)
assert_ok("成本-摘要", "chapter_draft" in str(tracker.summary()))

# 测试超预算
small_tracker = CostTracker(budget=0.001, model="deepseek-chat")
assert_ok("成本-超预算", not small_tracker.check_budget("测试" * 5000, 8192))

# ══════════════════════════════════════════════
#  Phase 5: 去AI味引擎
# ══════════════════════════════════════════════
print("\n═══ Phase 5: 去AI味引擎 ═══")

de_ai = DeAIEngine()
sample = "他仿佛看到了什么，不禁微微一笑，心中暗道这一切似乎都太过巧合了。然而就在这时，一声巨响传来。"
result = de_ai.process_rule_based(sample)
assert_ok("去AI-规则处理", len(result.processed) > 0)
assert_ok("去AI-替换统计", result.word_replacements > 0, f"替换{result.word_replacements}处")
assert_ok("去AI-结果不同", result.processed != sample, "文本已变化")

# 注入约束
snippet = de_ai.build_deai_prompt_snippet()
assert_ok("去AI-约束注入", len(snippet) > 100)

# ══════════════════════════════════════════════
#  Phase 7: 角色状态机
# ══════════════════════════════════════════════
print("\n═══ Phase 7: 角色状态机 ═══")

csm = CharacterStateMachine()
csm.register("林风", "废柴家主", "林家府邸", "炼气三层")
csm.register("苏婉儿", "林家大小姐", "林家府邸", "筑基期")
csm.register("神秘老者", "隐世高人", "未知", "")

# 模拟林风出场
chapter_content = "林风站在林家府邸前，沉默地看着面前的一群人。苏婉儿从人群中走出..."
csm.update_from_chapter(1, chapter_content)

lin = csm.get("林风")
assert_ok("角色-出场追踪", lin.last_appeared_chapter == 1)
assert_ok("角色-离线追踪", csm.get("神秘老者").offline_chapters == 1)

ctx_prompt = csm.build_context_prompt(chapter_num=1)
assert_ok("角色-上下文生成", len(ctx_prompt) > 50)
assert_ok("角色-包含角色名", "林风" in ctx_prompt)

# ══════════════════════════════════════════════
#  Phase 8: 内容审查
# ══════════════════════════════════════════════
print("\n═══ Phase 8: 内容审查 ═══")

reviewer = ContentReviewer()

good_sample = """
林风看着面前的退婚书，面无表情。

「林家，还真是看得起我。」他把退婚书往桌上一丢。

苏家大小姐脸色一变。她没想到这个废物竟敢这样说话。

「你——」

「我什么我？退就退，别耽误我修炼。」林风转身就走。

身后传来一阵倒吸冷气的声音。三年了，第一次有人敢这么跟苏家说话。

真是他娘的痛快。
"""

rev = reviewer.review(good_sample, chapter_num=1, target_words=3000)
assert_ok("审查-通过", rev.score > 0, f"{rev.score}分")
assert_ok("审查-摘要", len(rev.summary) > 0)

# 测试 AI 痕迹检测
ai_sample = "他仿佛看到了什么，不禁微微一笑，心中暗道这一切似乎都太过巧合了。然而就在这时，一声巨响传来。与此同时，他不由得倒吸一口凉气。只见一道金光闪过。"
ai_issues = reviewer.check_ai_patterns(ai_sample)
assert_ok("审查-AI痕迹检测", len(ai_issues) > 0, f"{len(ai_issues)} 个问题")

# ══════════════════════════════════════════════
#  Phase 9: 引擎路由（纯逻辑，不调 LLM）
# ══════════════════════════════════════════════
print("\n═══ Phase 9: 引擎路由 ═══")

engine = NovelEngine()
# 引擎 v2 引入 book_mode + Phase 枚举，路由需在续写模式下验证
engine.state.book_mode = BookMode.CONTINUE
engine.state.genre = "玄幻"
engine.state.sub_genre = "系统流"
engine.state.total_chapters = 500

# 路由-需要大纲（无大纲且无章节）
inst = engine.route()
assert_ok("路由-需要大纲", inst.op == Op.PLAN_OUTLINE, str(inst.op))

# 路由-有内容待审查
engine.state.outline_data = {"structure": "test"}
engine.state.chapters = [{"num": 1, "title": "测试", "outline": ""}]
engine.state.current_chapter = 1
engine.state.current_content = "test content"
engine.state.phase = Phase.REVIEWING
inst = engine.route()
assert_ok("路由-待审查", inst.op == Op.REVIEW_CHAPTER, str(inst.op))

# 路由-审查通过 → 去AI味
engine.state.phase = Phase.DE_AI
inst = engine.route()
assert_ok("路由-去AI味", inst.op == Op.DE_AI_PASS, str(inst.op))

# 路由-写下一章
engine.state.current_content = ""
engine.state.phase = Phase.IDLE
inst = engine.route()
assert_ok("路由-写下一章", inst.op == Op.WRITE_CHAPTER, str(inst.op))

# 路由-完本
engine.state.current_chapter = 500
engine.state.current_content = "last"
inst = engine.route()
assert_ok("路由-完本", inst.op == Op.COMPLETE, str(inst.op))

# ══════════════════════════════════════════════
#  Phase 10: 数据持久化
# ══════════════════════════════════════════════
print("\n═══ Phase 10: 数据持久化 ═══")

# 成本序列化
tracker.save(os.path.join(TMP_DIR, "test_cost.json"))
loaded = CostTracker.load(os.path.join(TMP_DIR, "test_cost.json"))
assert_ok("持久-成本", loaded.spent == tracker.spent)

# 角色状态序列化
csm.save(os.path.join(TMP_DIR, "test_chars.json"))
loaded_csm = CharacterStateMachine()
loaded_csm.load(os.path.join(TMP_DIR, "test_chars.json"))
assert_ok("持久-角色", len(loaded_csm.characters) == 3)

# ══════════════════════════════════════════════
#  Phase 11: 新书启动规划态（设定先行流程纯逻辑，不调 LLM）
# ══════════════════════════════════════════════
print("\n═══ Phase 11: 新书启动规划态 ═══")
from libraries.storyline import BookStoryline, basic_info_world_done
from libraries.outline_generator import basic_info_is_rich

assert_ok("规划-一句话种子即算已设定",
          basic_info_world_done({"world_building": {"description": "x"}}) is True)
assert_ok("规划-主角名即算已设定",
          basic_info_world_done({"protagonist": {"name": "张三"}}) is True)
assert_ok("规划-已打标即算已设定",
          basic_info_world_done({"_world_generated": True}) is True)
assert_ok("规划-空设定不算已设定", basic_info_world_done({}) is False)
assert_ok("规划-rich=已打标", basic_info_is_rich({"_world_generated": True}) is True)
assert_ok("规划-薄设定不算rich", basic_info_is_rich({"world_building": {"description": "x"}}) is False)

_tb = bm.create(title="plan_test", pen_name="t", genre="玄幻")
_tbid = _tb.book_id
try:
    _raised = False
    try:
        NovelEngine().continue_book(_tbid)
    except ValueError:
        _raised = True
    assert_ok("规划-无storyline报错", _raised)

    _tl = BookStoryline(genre="玄幻")
    _tl.basic_info["world_building"]["description"] = "一句话种子"
    bm.save_storyline(_tbid, _tl)
    _eng = NovelEngine()
    _st = _eng.continue_book(_tbid)
    assert_ok("规划-空大纲可进入(写作者None)", _eng.storyline_writer is None)
    assert_ok("规划-phase为OUTLINE", _st.phase == Phase.OUTLINE, _st.phase.value)

    from libraries.storyline import OutlineSlot
    _tl.outlines.append(OutlineSlot(id="o1", template_id="", name="测试",
                                    start_chapter=1, end_chapter=5))
    bm.save_storyline(_tbid, _tl)
    _eng2 = NovelEngine()
    _eng2.continue_book(_tbid)
    assert_ok("规划-有大纲建写作者", _eng2.storyline_writer is not None)
finally:
    bm.delete(_tbid)

# ══════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════
print(f"\n{'='*55}")
print(f"  测试结果: {passed}/{total} 通过")
if errors:
    print(f"  失败: {len(errors)}")
    for e in errors:
        print(f"    ❌ {e}")
else:
    print(f"  ✅ 全部通过！")
print(f"{'='*55}")

# 清理
bm.delete(cfg.book_id)
import os
for f in [os.path.join(TMP_DIR, "test_cost.json"),
          os.path.join(TMP_DIR, "test_chars.json")]:
    if os.path.exists(f):
        os.remove(f)

sys.exit(0 if not errors else 1)
