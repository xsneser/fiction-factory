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
from libraries.theme import ThemeLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.new_book import NewBookPipeline, NewBookConfig, recommend_opening
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

theme = ThemeLibrary()
assert_ok("内涵库-数量", len(theme.entries) >= 6)

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
#  Phase 3: 新书流程
# ══════════════════════════════════════════════
print("\n═══ Phase 3: 新书专项流程 ═══")

pipeline = NewBookPipeline()
config = NewBookConfig(
    title="测试书名", pen_name="枫落", genre="玄幻",
    sub_genre="系统流", platform="fanqie",
    opening_template_id="plot_dating_011",
    golden_finger_template_id="plot_dating_012",
    structure_template_id="struct_xuanhuan_01",
)

plan = pipeline.plan_opening(config)
assert_ok("新书-开篇方案", plan["opening_plot"] is not None, plan["opening_plot"].name)
assert_ok("新书-大纲匹配", plan["structure"] is not None, plan["structure"].name)

ch1_prompt = pipeline.build_chapter1_prompt(config, plan, profile)
assert_ok("新书-第一章prompt", len(ch1_prompt["user"]) > 200)

ch2_prompt = pipeline.build_chapter2_prompt(config, "（第一章正文占位）", profile)
assert_ok("新书-第二章prompt", len(ch2_prompt["user"]) > 100)

ch3_prompt = pipeline.build_chapter3_prompt(config, "（前两章正文占位）", profile)
assert_ok("新书-第三章prompt", len(ch3_prompt["user"]) > 100)

recs = recommend_opening("玄幻", "系统流")
assert_ok("新书-推荐方案", len(recs) >= 2, f"{len(recs)} 个方案")

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
#  Phase 6: 节拍规划（现行 beat_writer 管线）
# ══════════════════════════════════════════════
print("\n═══ Phase 6: 节拍规划 ═══")

from libraries.beat_writer import BeatLibrary, ChapterPlanner

beat_lib = BeatLibrary()
planner = ChapterPlanner(beat_lib)
plan = planner.plan_chapter(1, "对手当众挑衅，主角爆发隐藏实力逆转",
                            target_words=3000, genre="都市")
assert_ok("节拍-数量", len(plan.beats) == 7, f"{len(plan.beats)} 节拍")
assert_ok("节拍-以钩子开头", plan.beats[0].beat_type == "hook")
assert_ok("节拍-以收尾结束", plan.beats[-1].beat_type == "close")
assert_ok("节拍-字数目标", plan.total_words_target == 3000)
assert_ok("节拍-含冲突节拍", any(b.beat_type == "conflict" for b in plan.beats))

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
