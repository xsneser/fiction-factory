"""NovelEngine MCP 服务器 — 把引擎全部 LLM 操作暴露为工具（stdio，供 Claude Code 等 MCP 客户端驱动）。

独立进程、与 Flask Web 服务并存：数据协调点是 books/<id>/ 文件 JSON（原子写），
不共享 Web 进程的内存引擎缓存。写作/生成类长操作一律「阻塞式工具调用」——
工具内迭代生成器到完成，返回最终 JSON（不透传 SSE）。

用法（项目根目录）：
    claude mcp add --scope project novel-engine -- python mcp_server.py

角色语义（agent 字段，与右侧栏 Agent 活动面板一致）：
    writing / outline / world / title / outline_agent / scout
"""
import sys
import os
import json
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# FastMCP 导入路径随 SDK 版本变化：mcp>=1.x 用 mcp.server.fastmcp，新版也可从 fastmcp 顶层导入。
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from fastmcp import FastMCP

from core.llm_client import LLMClient
from core.models import APIConfig
from core.json_store import read_json
from core.text_utils import count_prose_units
from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.example_lib import ExampleLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.storyline import (
    BookStoryline, save_storyline, load_storyline, StorylineBuilder,
    OutlineSlot, annotate_plot_roles,
)

mcp = FastMCP("novel-engine")

# ─── 全局单例（自建，不依赖 Flask/ctx；四大库与 Web 行为一致）───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
example_lib = ExampleLibrary()
profiles = ProfileManager(os.path.join(_ROOT, "profiles"))

_llm = None
_engines: dict[str, object] = {}          # book_id -> NovelEngine（进程内会话缓存）
_bm = None


def bm_() -> BookManager:
    global _bm
    if _bm is None:
        _bm = BookManager(os.path.join(_ROOT, "books"))
    return _bm


def get_llm():
    """读根目录 api.json 建 LLM 客户端（懒加载缓存）。"""
    global _llm
    if _llm is not None:
        return _llm
    cfg = read_json(os.path.join(_ROOT, "api.json"), {})
    if not cfg.get("api_key"):
        return None
    _llm = LLMClient(APIConfig(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
        http_timeout_seconds=cfg.get("http_timeout_seconds", 300),
        verify_ssl=cfg.get("verify_ssl", True),
    ))
    return _llm


def _require_llm():
    llm = get_llm()
    if not llm:
        raise RuntimeError("LLM 未配置：请在 api.json 填入 api_key，或先运行设置页保存配置")
    return llm


# ─── 故事线统一存取 ───

def _tl_path(book_id: str) -> str:
    return os.path.join(_ROOT, "books", book_id, "storyline.json")


def load_tl(book_id: str):
    return load_storyline(_tl_path(book_id))


def save_tl(book_id: str, tl) -> None:
    save_storyline(tl, _tl_path(book_id))


def _require_tl(book_id: str):
    tl = load_tl(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）。"
                           "请先 create_book + save_basic_info / generate_world 生成设定，"
                           "再 generate_full_outline 生成大纲。")
    return tl


# ─── 引擎会话（进程内缓存，同 desk 的 cont_<book_id> 模式）───

def get_engine(book_id: str):
    if book_id not in _engines:
        from libraries.engine import NovelEngine
        e = NovelEngine(llm_client=_require_llm())
        try:
            e.continue_book(book_id)
        except ValueError as ex:
            raise RuntimeError(f"「{book_id}」无法进入写作：{ex}\n"
                               "请先用 save_basic_info / generate_world / "
                               "generate_full_outline 生成设定与大纲。") from ex
        _engines[book_id] = e
    return _engines[book_id]


def _snapshot(book_id: str) -> dict:
    """book.json 当前进度快照（各写入工具返回前补上）。"""
    b = bm_().get(book_id)
    if not b:
        return {}
    return {
        "current_chapter": b.current_chapter,
        "chapter_count": b.chapter_count,
        "status": b.status,
        "total_words": b.total_words or 0,
    }


# ─── 阻塞式消费生成器（核心：把 SSE 流式改造成同步结果）───

def consume_dict_stream(gen):
    """迭代 yield-dict 生成器到完成；error 事件/异常转 RuntimeError。返回 (last_event, events)。"""
    last, events = None, []
    for evt in gen:
        if isinstance(evt, dict) and evt.get("type") == "error":
            raise RuntimeError(evt.get("message", "LLM 生成失败"))
        events.append(evt)
        last = evt
    return last, events


def consume_triple_stream(gen):
    """迭代 (event_type, message, data_dict) 生成器；error 事件转 RuntimeError。返回 (last_t, last_d, events)。"""
    last_t, last_d, events = None, None, []
    for t, msg, d in gen:
        if t == "error":
            raise RuntimeError(msg or "LLM 生成失败")
        events.append((t, msg, d))
        last_t, last_d = t, d
    return last_t, last_d, events


def _draft_read(book_id: str):
    p = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _profile_for(tl):
    if tl and tl.pen_name:
        try:
            return profiles.get_by_name(tl.pen_name)
        except Exception:
            return None
    return None


def _max_id_suffix(ids) -> int:
    import re as _re
    max_n = 0
    for i in ids:
        m = _re.search(r"_(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _seed_builder_counter(builder, ids) -> None:
    builder._counter = _max_id_suffix(ids)


# ═══════════════════════════════════════════════════
# 只读 / 建书类（无 LLM，供上下文供给与测试）
# ═══════════════════════════════════════════════════

@mcp.tool()
def list_books() -> list:
    """列出书库全部书籍的摘要（book_id/书名/流派/状态/进度）。"""
    rows = []
    for b in bm_().list_all():
        rows.append({
            "book_id": b.book_id,
            "title": b.title,
            "pen_name": b.pen_name,
            "genre": b.genre,
            "sub_genre": b.sub_genre,
            "status": b.status,
            "current_chapter": b.current_chapter,
            "chapter_count": b.chapter_count,
            "total_words": b.total_words or 0,
        })
    return rows


@mcp.tool()
def get_book_state(book_id: str) -> dict:
    """读取一本书的完整状态：book 配置、故事线、结构大纲、章节摘要、进行中草稿。"""
    bm = bm_()
    book = bm.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    tl = bm.load_storyline(book_id)
    outline = bm.get_outline(book_id)
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = bm.load_chapter(book_id, n)
        if ch:
            chapters.append({
                "num": n,
                "title": ch.get("title"),
                "summary": ch.get("summary"),
                "word_count": count_prose_units(ch.get("content") or ""),
            })
    return {
        "book": {
            "book_id": book.book_id, "title": book.title, "pen_name": book.pen_name,
            "genre": book.genre, "sub_genre": book.sub_genre, "platform": book.platform,
            "status": book.status, "current_chapter": book.current_chapter,
            "chapter_count": book.chapter_count, "total_words": book.total_words or 0,
        },
        "storyline": tl.to_dict() if tl else None,
        "outline": outline,
        "chapters": chapters,
        "draft": _draft_read(book_id),
    }


@mcp.tool()
def get_storyline(book_id: str) -> dict:
    """读取一本书的故事线（timeline）JSON：大纲/桥段/线程/母题/基础设定。"""
    tl = _require_tl(book_id)
    return tl.to_dict()


@mcp.tool()
def create_book(title: str, pen_name: str, genre: str = "", sub_genre: str = "",
                platform: str = "fanqie", basic_info: dict = None) -> dict:
    """创建一本新书（建目录 + book.json + 初始 storyline.json，phase=config），返回 book 配置。"""
    cfg = bm_().create(title=title, pen_name=pen_name, genre=genre or "",
                       sub_genre=sub_genre or "", platform=platform or "fanqie")
    tl = BookStoryline(
        book_title=title, genre=genre or "", sub_genre=sub_genre or "",
        pen_name=pen_name, platform=platform or "fanqie",
        basic_info=dict(basic_info or {}), phase="config",
    )
    save_tl(cfg.book_id, tl)
    return {
        "book_id": cfg.book_id, "title": cfg.title, "pen_name": cfg.pen_name,
        "genre": cfg.genre, "sub_genre": cfg.sub_genre, "platform": cfg.platform,
        "status": cfg.status, "chapter_count": cfg.chapter_count,
        "current_chapter": cfg.current_chapter,
    }


@mcp.tool()
def borrow_preview(source_book_id: str) -> dict:
    """预览将借鉴源书的哪些设定（从已有书 basic_info 抽取种子）。"""
    src = load_tl(source_book_id)
    if not src:
        raise RuntimeError(f"源书 {source_book_id} 不存在或无故事线")
    from libraries.world_builder import WorldBuildingGenerator
    seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if not seed:
        raise RuntimeError("源书没有可借鉴的设定")
    return {"seed": seed,
            "source_title": src.book_title or src.pen_name or source_book_id,
            "source_genre": src.genre}


# ═══════════════════════════════════════════════════
# 规划 / 编辑类（调 LLM，成功后使引擎会话过期）
# ═══════════════════════════════════════════════════

@mcp.tool()
def save_basic_info(book_id: str, basic_info: dict) -> dict:
    """保存基础设定（主角/世界观/配角/基调/目标读者，深合并保留已填值），可带 book_title。"""
    tl = _require_tl(book_id)
    bi = dict(tl.basic_info or {})
    for section in ("protagonist", "world_building"):
        incoming = basic_info.get(section)
        if isinstance(incoming, dict):
            base = dict(bi.get(section, {}) or {})
            for k, v in incoming.items():
                if v not in (None, ""):
                    base[k] = v
            bi[section] = base
    for field in ("supporting_cast", "tone", "target_audience", "pov", "era_language"):
        if basic_info.get(field) not in (None, ""):
            bi[field] = basic_info[field]
    tl.basic_info = bi
    if basic_info.get("book_title") not in (None, ""):
        tl.book_title = basic_info["book_title"]
        book = bm_().get(book_id)
        if book:
            book.title = basic_info["book_title"]
            bm_().update(book)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"ok": True}


@mcp.tool()
def generate_title(book_id: str) -> dict:
    """AI 生成书名（3-5 个候选，选第一个写入 book_title + book.json.title）。"""
    tl = _require_tl(book_id)
    llm = _require_llm()
    bi = tl.basic_info or {}
    protag = bi.get("protagonist") or {}
    world = bi.get("world_building") or {}
    ctx = f"流派：{tl.genre}{'/' + tl.sub_genre if tl.sub_genre else ''}"
    if protag.get("name"):
        ctx += f"；主角：{protag.get('name')}（{protag.get('identity','')}）"
    if world.get("description"):
        ctx += f"；世界观：{world['description']}"
    if bi.get("tone"):
        ctx += f"；基调：{bi['tone']}"
    prompt = (f"为下面这本网络小说起书名（3-5 个，2-10 字，朗朗上口、有网文味）。\n\n{ctx}\n\n"
              '返回 JSON：{"titles": ["书名1", "书名2", "书名3"]}')
    from core.llm_client import extract_json
    raw = llm.call("你是网文书名策划。只返回JSON。", prompt,
                   temperature=0.8, max_tokens=1024)
    data = json.loads(extract_json(raw))
    titles = [t for t in (data.get("titles") or [])
              if isinstance(t, str) and t.strip()]
    if not titles:
        raise RuntimeError("书名生成失败（LLM 无有效候选）")
    tl.book_title = titles[0]
    save_tl(book_id, tl)
    book = bm_().get(book_id)
    if book:
        book.title = titles[0]
        bm_().update(book)
    _engines.pop(book_id, None)
    return {"titles": titles, "chosen": titles[0]}


@mcp.tool()
def generate_outlines(book_id: str, mode: str = "ai", max_outlines: int = 5) -> dict:
    """生成大纲序列（mode=ai 用 LLM；rule 用流派模板确定性生成）。"""
    tl = _require_tl(book_id)
    llm = get_llm() if mode == "ai" else None
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=llm)
    if mode == "rule":
        tl.outlines = builder.build_outline_sequence(genre=tl.genre, mode="rule",
                                                     max_outlines=max_outlines)
    else:
        tl.outlines = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=(tl.basic_info or {}).get("world_building", {}).get("description", ""),
            max_outlines=max_outlines, mode="ai")
    tl.phase = "outlines"
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"ok": True, "count": len(tl.outlines)}


@mcp.tool()
def generate_full_outline(book_id: str) -> dict:
    """一键生成完整大纲（5 阶段：分析→大纲→桥段→内涵/吸睛→一致性），原地累加并逐步落盘。

    阻塞运行至完成（可能数分钟），返回最终 timeline 快照。
    """
    tl = _require_tl(book_id)
    llm = _require_llm()
    profile = _profile_for(tl)

    from libraries.outline_generator import OutlineGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile,
                            gag_lib=gag_lib, plot_lib=plot_lib)
    gen = OutlineGenerator(llm_client=llm, structure_lib=struct_lib,
                           plot_lib=plot_lib, gag_lib=gag_lib,
                           profile=profile, harness=harness)

    bi = tl.basic_info or {}
    world = bi.get("world_building", {}) or {}
    protag = bi.get("protagonist", {}) or {}
    ctx_parts = []
    if world.get("world_summary"):
        ctx_parts.append(f"世界观概述：{world['world_summary']}")
    if world.get("description"):
        ctx_parts.append(f"世界观：{world['description']}")
    if protag.get("name") or protag.get("identity"):
        ctx_parts.append(f"主角：{protag.get('name','')}（{protag.get('identity','')}）")
    if bi.get("storyline_hint"):
        ctx_parts.append(f"故事线想法：{bi['storyline_hint']}")
    custom_context = "；".join(ctx_parts) or (tl.book_title or "")

    last_t, last_d, events = consume_triple_stream(gen.generate(
        genre=tl.genre, sub_genre=tl.sub_genre,
        custom_context=custom_context, pen_name=tl.pen_name,
        words_per_chapter=tl.words_per_chapter,
        storyline=tl, on_save=lambda _tl: save_tl(book_id, _tl),
        skip_analyze=bool((tl.basic_info or {}).get("_world_generated"))))

    tl = load_tl(book_id)  # on_save 已逐步落盘，重新读取最终快照
    _engines.pop(book_id, None)
    return {"stats": last_d, "event_count": len(events),
            "timeline": tl.to_dict() if tl else None}


def _build_next_arc(builder, tl, mode="rule"):
    """故事线末尾追加下一段大纲弧（rule=模板循环；ai=单弧 LLM 再锚定）。镜像 storyline.py。"""
    if mode == "ai":
        seq = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=(tl.basic_info or {}).get("world_building", {}).get("description", ""),
            max_outlines=1, mode="ai")
        if not seq:
            return None
        arc = seq[0]
        max_end = max((o.end_chapter for o in tl.outlines), default=0)
        span = max(arc.end_chapter - arc.start_chapter + 1, 20)
        arc.start_chapter = max_end + 1
        arc.end_chapter = arc.start_chapter + span - 1
        if tl.outlines:
            arc.predecessor = tl.outlines[-1].id
            tl.outlines[-1].successor = arc.id
        return arc

    structs = struct_lib.search(genre=tl.genre) or struct_lib.templates
    if not structs:
        return None
    idx = len(tl.outlines) % len(structs)
    tmpl = structs[idx]
    max_end = max((o.end_chapter for o in tl.outlines), default=0)
    start = max_end + 1
    span = min(tmpl.total_chapters, 60)
    arc = OutlineSlot(
        id=builder._next_id("outline"),
        template_id=tmpl.id,
        name=f"{tmpl.name}(第{len(tl.outlines) + 1}部分)",
        start_chapter=start,
        end_chapter=start + span - 1,
        stages=[{"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                 "events": s.key_events[:5]}
                for s in tmpl.stages],
        predecessor=tl.outlines[-1].id if tl.outlines else "",
        transition_type="sequential",
    )
    if tl.outlines:
        tl.outlines[-1].successor = arc.id
    return arc


@mcp.tool()
def extend_outline(book_id: str, mode: str = "ai") -> dict:
    """续写时扩展故事线：末尾追加新大纲弧 + 填充桥段 + 加料，同步 bump 章节总数。"""
    tl = _require_tl(book_id)
    if not tl.outlines:
        raise RuntimeError("尚无故事线大纲，请先 generate_full_outline 后再扩展")
    llm = get_llm() if mode == "ai" else None
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=llm)
    _seed_builder_counter(builder,
                          [o.id for o in tl.outlines] + [p.id for p in tl.plots])

    new_arc = _build_next_arc(builder, tl, mode)
    if new_arc is None:
        raise RuntimeError("无可用大纲模板")
    tl.outlines.append(new_arc)
    new_plots = builder.fill_plots_for_outline(new_arc, tl)
    existing_ids = {p.id for p in tl.plots}
    added = [p for p in new_plots if p.id not in existing_ids]
    tl.plots.extend(added)
    builder.fill_themes_and_hooks(added, tl)
    annotate_plot_roles(tl)
    tl.phase = "ready"
    save_tl(book_id, tl)

    new_total = 0
    book = bm_().get(book_id)
    if book:
        new_total = max(book.chapter_count, new_arc.end_chapter)
        if new_total > book.chapter_count:
            book.chapter_count = new_total
            bm_().update(book)
    _engines.pop(book_id, None)
    return {"outline": {"id": new_arc.id, "name": new_arc.name,
                        "start_chapter": new_arc.start_chapter,
                        "end_chapter": new_arc.end_chapter},
            "plots_added": len(added), "total_chapters": new_total}


@mcp.tool()
def confirm_outlines(book_id: str) -> dict:
    """确认大纲序列，进入桥段编排阶段（phase → plots）。"""
    tl = _require_tl(book_id)
    tl.phase = "plots"
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"ok": True, "phase": tl.phase}


@mcp.tool()
def fill_plots(book_id: str) -> dict:
    """给每个大纲填充桥段（LLM），返回新增数量与累计总量。"""
    tl = _require_tl(book_id)
    if not tl.outlines:
        raise RuntimeError("请先生成大纲序列（generate_outlines / generate_full_outline）")
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=_require_llm())
    _seed_builder_counter(builder, [p.id for p in tl.plots])

    new_plots = []
    for o in tl.outlines:
        new_plots.extend(builder.fill_plots_for_outline(o, tl))

    existing_ids = {p.id for p in tl.plots}
    added = [p for p in new_plots if p.id not in existing_ids]
    tl.plots.extend(added)
    annotate_plot_roles(tl)
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"plots_added": len(added), "total_plots": len(tl.plots)}


@mcp.tool()
def fill_gags(book_id: str) -> dict:
    """给桥段挂载内涵（compatible_plots 规则）+ 标注吸睛点（规则，不调 LLM）。"""
    tl = _require_tl(book_id)
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib)
    builder.fill_themes_and_hooks(tl.plots, tl)
    annotate_plot_roles(tl)
    tl.phase = "ready" if tl.plots else "gags"
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"ok": True, "phase": tl.phase}


@mcp.tool()
def outline_agent(book_id: str, message: str) -> dict:
    """大纲助手：用自然语言调整故事线（改桥段/加笑点/增删桥段/改大纲等），直接落盘。"""
    tl = _require_tl(book_id)
    if not message.strip():
        raise RuntimeError("消息为空")
    llm = _require_llm()
    from libraries.outline_agent import OutlineAgent
    agent = OutlineAgent(llm=llm, structure_lib=struct_lib, plot_lib=plot_lib,
                         gag_lib=gag_lib)
    result = agent.handle(tl, message)
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return result


@mcp.tool()
def generate_world(book_id: str, mode: str = "one", idea: str = "",
                   source_book_id: str = "", tweak: str = "") -> dict:
    """生成世界观设定（mode=one 一句话生成；borrow 从源书借鉴+微调），返回 basic_info。"""
    tl = _require_tl(book_id)
    llm = _require_llm()
    mode = mode or "one"
    idea = (idea or "").strip()
    source_book_id = (source_book_id or "").strip()
    tweak = (tweak or "").strip()

    seed = None
    if mode == "borrow" and source_book_id:
        src = load_tl(source_book_id)
        if src:
            from libraries.world_builder import WorldBuildingGenerator
            seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if mode == "borrow":
        idea = tweak
    if not idea:
        idea = str((tl.basic_info or {}).get("world_building", {}).get("description", "") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)

    last_t, last_d, events = consume_triple_stream(gen.generate(
        genre=tl.genre, sub_genre=tl.sub_genre, idea=idea,
        pen_name=tl.pen_name, platform=tl.platform,
        seed_basic_info=seed, storyline=tl,
        on_save=lambda _tl: save_tl(book_id, _tl)))

    tl = load_tl(book_id)
    _engines.pop(book_id, None)
    return {"basic_info": tl.basic_info if tl else None,
            "event_count": len(events), "done_data": last_d}


@mcp.tool()
def world_candidates(book_id: str, idea: str = "") -> dict:
    """一次产出 2-3 个差异化世界观方向供选择（LLM）。"""
    tl = _require_tl(book_id)
    llm = _require_llm()
    idea = (idea or "").strip()
    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    candidates = gen.generate_candidates(genre=tl.genre, sub_genre=tl.sub_genre, idea=idea)
    if not candidates:
        raise RuntimeError("示例候选生成失败，请重试")
    return {"candidates": candidates}


@mcp.tool()
def confirm_world(book_id: str) -> dict:
    """确认世界观设定：basic_info 够充实则打标 _world_generated（后续大纲跳过 Phase 1 分析）。"""
    tl = _require_tl(book_id)
    tl.basic_info = tl.basic_info or {}
    from libraries.outline_generator import basic_info_is_rich
    if basic_info_is_rich(tl.basic_info):
        tl.basic_info["_world_generated"] = True
    else:
        tl.basic_info.pop("_world_generated", None)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _engines.pop(book_id, None)
    return {"ok": True, "world_generated": bool(tl.basic_info.get("_world_generated"))}


# ═══════════════════════════════════════════════════
# 写作 / 元数据类（调 LLM，保留引擎会话以支持断点续写）
# ═══════════════════════════════════════════════════

@mcp.tool()
def write_next_bridge(book_id: str) -> dict:
    """写「下一个」桥段（按桥段撰写，阻塞至该桥段写完）：本章满字数自动切章。"""
    engine = get_engine(book_id)
    last, events = consume_dict_stream(engine._write_next_bridge_stream())
    snap = _snapshot(book_id)
    if last is None:
        return {"status": "noop", **snap}
    t = last.get("type")
    if t == "chapter_done":
        return {"status": "chapter_done", "chapter": last.get("chapter"),
                "word_count": last.get("word_count"), "beats": last.get("beats"),
                "review": last.get("review"), "cost": last.get("cost"), **snap}
    if t == "chapter_progress":
        return {"status": "bridge_written", "chapter": last.get("chapter"),
                "words": last.get("words"), "target": last.get("target"),
                "draft": _draft_read(book_id), **snap}
    if t == "complete":
        return {"status": "complete", "message": last.get("message"), **snap}
    if t == "budget_paused":
        return {"status": "budget_paused", "message": last.get("message"), **snap}
    # 桥段已写但未切章（或草稿收尾）：给出已产出的桥段数
    return {"status": "bridge_written", "last_event": t,
            "bridge_done_count": sum(1 for e in events if e.get("type") == "bridge_done"),
            "draft": _draft_read(book_id), **snap}


@mcp.tool()
def write_chapter(book_id: str, chapter_num: int = 0) -> dict:
    """整章同步写作（按桥段驱动，一次写完一章），chapter_num=0 表示写下一章。"""
    from libraries.engine import Instruction, Op
    engine = get_engine(book_id)
    n = chapter_num or engine.state.current_chapter + 1
    result = engine.execute(Instruction(Op.WRITE_STORYLINE_CHAPTER, chapter_num=n))
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    out = dict(result or {})
    out.update(_snapshot(book_id))
    return out


@mcp.tool()
def generate_book_meta(book_id: str) -> dict:
    """基于第 1 章生成书名+简介并落盘（book.json / storyline.json / outline.json）。"""
    bm = bm_()
    if not bm.get(book_id):
        raise RuntimeError(f"书 {book_id} 不存在")
    ch1 = bm.load_chapter(book_id, 1)
    if not ch1 or not ch1.get("content"):
        raise RuntimeError("尚无第 1 章正文，请先 write_next_bridge / write_chapter 写作")
    llm = _require_llm()
    from libraries.engine import NovelEngine
    engine = NovelEngine(llm_client=llm)
    engine.continue_book(book_id)
    result = engine._generate_book_meta(ch1["content"])
    bm._cache.pop(book_id, None)          # 让后续读取看到新 title
    _engines.pop(book_id, None)
    return result


if __name__ == "__main__":
    mcp.run()   # stdio transport
