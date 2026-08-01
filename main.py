"""
NovelEngine — 完整 API 服务器
FastAPI 后端，全部端点对齐 show-me-the-story v3.0.1
"""
import os
import json
import logging
import threading
import time
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.models import (
    Progress, ChapterState, ChapterStatus, ProjectSettings,
    Character, WorldviewEntry, Organization, APIConfig, Skill,
    Arc, Foreshadow, ForeshadowStatus, ForeshadowEvent,
    PostProcessState, PostProcessExecuteOptions, BookDiagnosis,
)
from core.llm_client import LLMClient
from core import storage, writing, inject, foreshadow, skills as skills_mod, arcs, reconcile
from core.writing import generate_outline, generate_chapter_full_pipeline, confirm_chapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("novel-engine")

# ─── App State ───

class AppState:
    def __init__(self):
        self.prog_dir = ""
        self.api_cfg = APIConfig()
        self.api_cfg_path = ""
        self.project_name = ""
        self.cfg = {}
        self.cfg_path = ""
        self.state = Progress()
        self.progress_path = ""
        self.settings = ProjectSettings()
        self.settings_path = ""
        self.llm_client: Optional[LLMClient] = None
        self.task_running = False
        self.task_lock = threading.Lock()
        self.auto_confirm = False
        self.version = "0.1.0-py"
        self.postprocess = PostProcessState()
        self.postprocess_path = ""

    def project_dir(self) -> str:
        if not self.project_name:
            return self.prog_dir
        return str(Path(self.prog_dir) / "storys" / self.project_name)

    def storys_dir(self) -> str:
        return str(Path(self.prog_dir) / "storys")

    def try_start_task(self) -> bool:
        with self.task_lock:
            if self.task_running:
                return False
            self.task_running = True
            return True

    def end_task(self):
        with self.task_lock:
            self.task_running = False


state = AppState()


def init_app(prog_dir: str):
    state.prog_dir = prog_dir
    storage.ensure_dir(state.storys_dir())
    api_path = os.path.join(prog_dir, "api.json")
    state.api_cfg = storage.load_api_config(api_path)
    state.api_cfg_path = api_path
    state.llm_client = LLMClient(state.api_cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_app(os.environ.get("NOVEL_ENGINE_DIR", os.getcwd()))
    yield


app = FastAPI(title="NovelEngine", version="0.1.0", lifespan=lifespan)
# CORS 收窄到本地面板 + Streamlit 实验台，不再向任意来源开放
LOCAL_ALLOWED_ORIGINS = [
    "http://localhost:58080",
    "http://127.0.0.1:58080",
    "http://localhost:8501",
    "http://127.0.0.1:8501",
]
app.add_middleware(CORSMiddleware,
                   allow_origins=LOCAL_ALLOWED_ORIGINS,
                   allow_methods=["*"],
                   allow_headers=["*"])

# 静态文件（原版 Svelte 前端）
frontend_dir = Path(__file__).parent / "frontend"
_has_frontend = frontend_dir.exists()
if _has_frontend:
    app.mount("/assets", StaticFiles(directory=str(frontend_dir / "assets")), name="assets")


# ─── Helpers ───

def _ensure(): 
    if not state.project_name: raise HTTPException(400, "请先选择项目")

def _check(): 
    if state.task_running: raise HTTPException(409, "AI任务运行中")

def _json(data, code=200):
    return JSONResponse(data, status_code=code)


async def _run_blocking(fn, *args, **kwargs):
    """在后台线程执行阻塞函数，避免占用事件循环（LLM 调用最长 300s）"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ─── Pydantic Models for Request Bodies ───

class ProjReq(BaseModel): name: str; language: str = "zh"
class APIUp(BaseModel):
    api_key: str = ""; base_url: str = ""; model: str = ""
    url_strict: bool = False; http_timeout_seconds: int = 300
    context_budget_tokens: int = 300000
class APITest(BaseModel): api_key: str; base_url: str; model: str
class StoryUp(BaseModel):
    type: str = ""; title: str = ""; chapter_count: int = 12
    target_words_per_chapter: int = 3000; writing_style: str = ""
    writing_pov: str = ""; story_synopsis: str = ""
class CharReq(BaseModel):
    name: str; age: str = ""; appearance: str = ""; personality: str = ""
    background: str = ""; motivation: str = ""; abilities: str = ""; notes: str = ""
class CharUp(BaseModel):
    name: str = ""; age: str = ""; appearance: str = ""; personality: str = ""
    background: str = ""; motivation: str = ""; abilities: str = ""; notes: str = ""
class WorldReq(BaseModel): name: str; category: str = ""; description: str; tags: str = ""
class OrgReq(BaseModel): name: str; type: str = ""; description: str
class Feedback(BaseModel): feedback: str
class FSCreate(BaseModel):
    name: str; description: str; plant_chapter: int = 0; target_chapter: int = 0
class FSUp(BaseModel):
    name: str = ""; description: str = ""; plant_chapter: int = 0
    target_chapter: int = 0; status: str = ""; resolution: str = ""
class AutoC(BaseModel): enabled: bool
class ConflictR(BaseModel): action: str
class OutlineContinue(BaseModel): chapter_count: int = 5
class OutlineCharConfirm(BaseModel): characters: list[dict]


# ─── UI兼容端点 (原版Svelte前端路径, 必须在模型定义之后) ───

@app.get("/api/config")
def get_config_alias(): _ensure(); return state.cfg

@app.put("/api/config")
def put_config_alias(req: StoryUp):
    _ensure(); _check()
    story = state.cfg.setdefault("story", {})
    for k, v in {"type": req.type, "title": req.title, "chapter_count": req.chapter_count,
                  "target_words_per_chapter": req.target_words_per_chapter,
                  "writing_style": req.writing_style, "writing_pov": req.writing_pov,
                  "story_synopsis": req.story_synopsis}.items():
        if v: story[k] = v
    storage.save_story_config(state.cfg_path, state.cfg)
    return state.cfg

@app.get("/api/config/pending-changes")
def pending_changes(): return {"changes": []}

@app.delete("/api/config/pending-changes")
def clear_pending(): return {"status": "cleared"}

@app.get("/api/import/status")
def import_status(): return {"active": False}

@app.get("/api/autoconfirm")
def autoconfirm_alias(): return {"enabled": state.auto_confirm}

@app.put("/api/autoconfirm")
def autoconfirm_put_alias(req: AutoC):
    state.auto_confirm = req.enabled; return {"enabled": state.auto_confirm}

@app.get("/api/chat/sessions")
def chat_sessions(): return []

@app.post("/api/chat/sessions")
def create_chat_session(): return {"id": "1", "title": "新对话"}

@app.get("/api/chat/sessions/{sid}")
def chat_session_detail(sid: str): return {"id": sid, "title": "对话", "messages": []}

@app.post("/api/chat/sessions/{sid}/messages")
def chat_send_message(sid: str, req: dict = None):
    return {"role": "assistant", "content": "AI聊天功能开发中..."}

@app.get("/api/postprocess")
def get_postprocess(): return {"phase": "", "diagnosis": "", "roadmap": {"items": []}}

# ─── Svelte 前端路径别名 (单数/复数兼容) ───
@app.post("/api/chapter/generate")
async def chapter_generate_singular():
    return await generate_chapter_api()

@app.post("/api/chapter/confirm")
def chapter_confirm_singular():
    return confirm_chapter_api()

@app.post("/api/chapter/revise")
async def chapter_revise_singular(req: Feedback):
    return await revise_chapter_api(req)

@app.post("/api/outline/generate-continuation")
async def outline_continue_alias(req: OutlineContinue):
    return await outline_continuation(req)

@app.get("/api/events")
def events_sse(request: Request):
    import asyncio
    async def gen():
        import time
        while True:
            if await request.is_disconnected(): break
            yield f"data: {json.dumps({'type':'heartbeat','time':time.time()})}\n\n"
            await asyncio.sleep(5)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ─── Projects ───

@app.get("/api/projects")
def list_projects():
    projs = storage.list_projects(state.storys_dir())
    for p in projs:
        p["compatibility"] = "supported"
        p["phase"] = ""
        pp = Path(state.storys_dir()) / p["name"] / "progress.json"
        if pp.exists():
            d = storage.read_json(str(pp))
            if d: p["phase"] = d.get("phase", "")
    return projs

@app.get("/api/projects/current")
def current(): return {"name": state.project_name, "language": state.cfg.get("language", "zh") if state.cfg else "zh"}

@app.post("/api/projects")
def create_project(req: ProjReq):
    if storage.create_project(state.storys_dir(), req.name, req.language):
        return {"status": "created", "name": req.name}
    raise HTTPException(409, "项目已存在")

@app.post("/api/projects/select")
def select_project(req: ProjReq):
    pd = Path(state.storys_dir()) / req.name
    if not pd.is_dir(): raise HTTPException(404, "项目不存在")
    state.project_name = req.name
    state.cfg_path = str(pd / "config.json")
    state.progress_path = str(pd / "progress.json")
    state.settings_path = str(pd / "settings.json")
    state.cfg = storage.load_story_config(state.cfg_path)
    state.state = storage.load_progress(state.progress_path)
    state.settings = storage.load_settings(state.settings_path)
    logger.info(f"切换到项目: {req.name}")
    return {"status": "selected", "name": req.name}

@app.delete("/api/projects/{name}")
def delete_project(name: str):
    _check()
    if storage.delete_project(state.storys_dir(), name):
        if state.project_name == name: state.project_name = ""; state.state = Progress()
        return {"status": "deleted"}
    raise HTTPException(404, "项目不存在")


# ─── API Config ───

@app.get("/api/config/api")
def get_api():
    return {"api_key": state.api_cfg.api_key[:8] + "***" if state.api_cfg.api_key else "",
            "base_url": state.api_cfg.base_url, "model": state.api_cfg.model,
            "http_timeout_seconds": state.api_cfg.http_timeout_seconds,
            "context_budget_tokens": state.api_cfg.context_budget_tokens}

@app.put("/api/config/api")
def put_api(req: APIUp):
    _check()
    state.api_cfg = APIConfig(api_key=req.api_key or state.api_cfg.api_key,
                              base_url=req.base_url, model=req.model, url_strict=req.url_strict,
                              http_timeout_seconds=req.http_timeout_seconds,
                              context_budget_tokens=req.context_budget_tokens)
    storage.save_api_config(state.api_cfg_path, state.api_cfg)
    state.llm_client = LLMClient(state.api_cfg)
    return {"status": "saved"}

@app.post("/api/config/api/test")
async def test_api(req: APITest):
    c = LLMClient(APIConfig(api_key=req.api_key, base_url=req.base_url, model=req.model))
    r = await _run_blocking(c.test_connection)
    if r["success"]: return {"success": True, "message": "连接成功", "sample": r["sample"]}
    return {"success": False, "error": r["error"]}  # 返回200让前端看到错误详情


# ─── Story Config ───

@app.get("/api/config/story")
def get_story(): _ensure(); return state.cfg

@app.put("/api/config/story")
def put_story(req: StoryUp):
    _ensure(); _check()
    story = state.cfg.setdefault("story", {})
    story.update({"type": req.type, "title": req.title, "chapter_count": req.chapter_count,
                  "target_words_per_chapter": req.target_words_per_chapter,
                  "writing_style": req.writing_style, "writing_pov": req.writing_pov,
                  "story_synopsis": req.story_synopsis})
    storage.save_story_config(state.cfg_path, state.cfg)
    return state.cfg


# ─── Progress ───

@app.get("/api/progress")
def get_progress(): return state.state.to_dict()

@app.delete("/api/progress")
def delete_progress():
    _ensure(); _check()
    storage.reset_progress(state.progress_path)
    state.state = Progress()
    return {"status": "reset"}


# ─── Outline ───

@app.post("/api/outline/generate")
async def generate_outline_api():
    _ensure(); _check()
    for ch in state.state.chapters:
        if ch.status in (ChapterStatus.WRITING, ChapterStatus.REVIEW):
            raise HTTPException(409, "有章节正在写作/审核中")
        if ch.status == ChapterStatus.ACCEPTED:
            raise HTTPException(409, "有已确认章节")

    state.task_running = True
    try:
        title, core_prompt, synopsis, chapters = await _run_blocking(
            generate_outline, state.llm_client, state.cfg, state.settings, state.state)
        state.state.title = title; state.state.core_prompt = core_prompt
        state.state.story_synopsis = synopsis; state.state.chapters = chapters
        state.state.phase = "outline"
        storage.save_progress(state.progress_path, state.state)
        return state.state.to_dict()
    except Exception as e:
        logger.error(f"大纲生成失败: {e}")
        raise HTTPException(500, str(e))
    finally:
        state.task_running = False

@app.post("/api/outline/confirm")
def confirm_outline_api():
    _ensure()
    if state.state.phase != "outline": raise HTTPException(400, "不在大纲阶段")
    if not state.state.chapters: raise HTTPException(400, "大纲为空")
    state.state.phase = "writing"; state.state.current_chapter_index = 0
    storage.save_progress(state.progress_path, state.state)
    return state.state.to_dict()

@app.post("/api/outline/revise")
async def revise_outline_api(req: Feedback):
    _ensure(); _check()
    state.task_running = True
    try:
        title, core_prompt, synopsis, chapters = await _run_blocking(
            generate_outline, state.llm_client, state.cfg, state.settings, state.state)
        state.state.chapters = chapters
        storage.save_progress(state.progress_path, state.state)
        return state.state.to_dict()
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.delete("/api/outline")
def delete_outline_api():
    _ensure(); _check()
    state.state.title = ""; state.state.core_prompt = ""; state.state.story_synopsis = ""
    state.state.chapters = []; state.state.arcs = []
    state.state.current_chapter_index = 0; state.state.phase = "outline"
    storage.save_progress(state.progress_path, state.state)
    return state.state.to_dict()

@app.post("/api/outline/continuation")
async def outline_continuation(req: OutlineContinue):
    """生成后续大纲（已有章节后追加）"""
    _ensure(); _check()
    if not state.state.chapters:
        raise HTTPException(400, "无已有章节，请先生成大纲")
    state.task_running = True
    try:
        existing = len(state.state.chapters)
        # 重新生成更多章节
        story = state.cfg.get("story", {})
        story["chapter_count"] = existing + req.chapter_count
        title, core_prompt, synopsis, chapters = await _run_blocking(
            generate_outline, state.llm_client, state.cfg, state.settings, state.state)
        # 只保留新的
        new_chapters = [c for c in chapters if c.num > existing]
        state.state.chapters.extend(new_chapters)
        storage.save_progress(state.progress_path, state.state)
        return state.state.to_dict()
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.put("/api/outline/chapters/{num}")
def edit_chapter_outline(num: int, req: dict = None):
    _ensure(); _check()
    import json as _json
    body = req or {}
    for ch in state.state.chapters:
        if ch.num == num:
            if ch.status == ChapterStatus.ACCEPTED:
                raise HTTPException(400, "已确认章节不可修改大纲")
            ch.title = body.get("title", ch.title)
            ch.outline = body.get("outline", ch.outline)
            storage.save_progress(state.progress_path, state.state)
            return {"num": num, "title": ch.title, "outline": ch.outline}
    raise HTTPException(404, "章节不存在")


# ─── Chapters ───

@app.get("/api/chapters/{num}")
def get_chapter(num: int):
    for ch in state.state.chapters:
        if ch.num == num:
            return {"num": ch.num, "title": ch.title, "outline": ch.outline,
                    "content": ch.content, "summary": ch.summary,
                    "status": ch.status.value, "word_count": ch.word_count}
    raise HTTPException(404, "章节不存在")

@app.post("/api/chapters/generate")
async def generate_chapter_api():
    _ensure(); _check()
    if state.state.phase != "writing": raise HTTPException(400, "不在写作阶段")
    state.task_running = True
    try:
        result = await _run_blocking(
            generate_chapter_full_pipeline, state.llm_client, state.cfg, state.state,
            state.settings, state.progress_path, state.project_dir())
        # 写完更新伏笔
        idx = state.state.current_chapter_index
        if state.state.foreshadows:
            await _run_blocking(
                foreshadow.update_foreshadows_after_chapter,
                state.llm_client, state.state, idx)
            foreshadow_roadmap = foreshadow.build_foreshadow_roadmap(state.state)
            Path(state.project_dir(), "Foreshadows.md").write_text(foreshadow_roadmap, encoding='utf-8')
        ch = result["chapter"]
        return {"num": ch.num, "title": ch.title, "content": ch.content,
                "summary": ch.summary, "status": ch.status.value, "word_count": ch.word_count}
    except Exception as e:
        logger.error(f"章节生成失败: {e}")
        raise HTTPException(500, str(e))
    finally:
        state.task_running = False

@app.post("/api/chapters/confirm")
def confirm_chapter_api():
    _ensure()
    if state.state.phase != "writing": raise HTTPException(400, "不在写作阶段")
    confirm_chapter(state.state, state.progress_path)
    return state.state.to_dict()

@app.post("/api/chapters/revise")
async def revise_chapter_api(req: Feedback):
    _ensure(); _check()
    state.task_running = True
    try:
        result = await _run_blocking(
            generate_chapter_full_pipeline, state.llm_client, state.cfg, state.state,
            state.settings, state.progress_path, state.project_dir())
        return {"status": "revised"}
    finally: state.task_running = False


# ─── Characters ───

@app.get("/api/settings")
def get_settings():
    _ensure()
    return {"characters": [{"id": c.id, "name": c.name, "age": c.age, "appearance": c.appearance,
                            "personality": c.personality, "background": c.background,
                            "motivation": c.motivation, "abilities": c.abilities, "notes": c.notes,
                            "relationships": c.relationships}
                           for c in state.settings.characters],
            "worldview": [{"id": w.id, "name": w.name, "category": w.category,
                           "description": w.description, "tags": w.tags}
                          for w in state.settings.worldview],
            "organizations": [{"id": o.id, "name": o.name, "type": o.type,
                               "description": o.description, "members": o.members}
                              for o in state.settings.organizations]}

@app.post("/api/settings/characters")
def create_char(req: CharReq):
    _ensure(); _check()
    cid = state.settings.next_character_id()
    char = Character(id=cid, name=req.name, age=req.age, appearance=req.appearance,
                     personality=req.personality, background=req.background,
                     motivation=req.motivation, abilities=req.abilities, notes=req.notes)
    state.settings.characters.append(char)
    storage.save_settings(state.settings_path, state.settings)
    return {"id": cid, "name": req.name}

@app.put("/api/settings/characters/{cid}")
def update_char(cid: str, req: CharUp):
    _ensure(); _check()
    for c in state.settings.characters:
        if c.id == cid:
            for f in ["name","age","appearance","personality","background","motivation","abilities","notes"]:
                v = getattr(req, f, None) or ""
                if v: setattr(c, f, v)
            storage.save_settings(state.settings_path, state.settings)
            return {"id": cid}
    raise HTTPException(404, "角色不存在")

@app.delete("/api/settings/characters/{cid}")
def delete_char(cid: str):
    _ensure(); _check()
    state.settings.characters = [c for c in state.settings.characters if c.id != cid]
    storage.save_settings(state.settings_path, state.settings)
    return {"status": "deleted"}


# ─── Worldview ───

@app.post("/api/settings/worldview")
def create_worldview(req: WorldReq):
    _ensure(); _check()
    wid = state.settings.next_worldview_id()
    wv = WorldviewEntry(id=wid, name=req.name, category=req.category,
                        description=req.description, tags=req.tags)
    state.settings.worldview.append(wv)
    storage.save_settings(state.settings_path, state.settings)
    return {"id": wid}

@app.put("/api/settings/worldview/{wid}")
def update_worldview(wid: str, req: WorldReq):
    _ensure(); _check()
    for w in state.settings.worldview:
        if w.id == wid:
            if req.name: w.name = req.name
            if req.category: w.category = req.category
            if req.description: w.description = req.description
            if req.tags: w.tags = req.tags
            storage.save_settings(state.settings_path, state.settings)
            return {"id": wid}
    raise HTTPException(404, "不存在")

@app.delete("/api/settings/worldview/{wid}")
def delete_worldview(wid: str):
    _ensure(); _check()
    state.settings.worldview = [w for w in state.settings.worldview if w.id != wid]
    storage.save_settings(state.settings_path, state.settings)
    return {"status": "deleted"}


# ─── Organizations ───

@app.post("/api/settings/organizations")
def create_org(req: OrgReq):
    _ensure(); _check()
    oid = state.settings.next_org_id()
    org = Organization(id=oid, name=req.name, type=req.type, description=req.description)
    state.settings.organizations.append(org)
    storage.save_settings(state.settings_path, state.settings)
    return {"id": oid}

@app.delete("/api/settings/organizations/{oid}")
def delete_org(oid: str):
    _ensure(); _check()
    state.settings.organizations = [o for o in state.settings.organizations if o.id != oid]
    storage.save_settings(state.settings_path, state.settings)
    return {"status": "deleted"}


# ─── Foreshadows ───

@app.get("/api/foreshadows")
def get_foreshadows():
    return [{"id": f.id, "name": f.name, "description": f.description,
             "plant_chapter": f.plant_chapter, "target_chapter": f.target_chapter,
             "status": f.status.value, "events": [{"chapter": e.chapter, "note": e.note} for e in f.events],
             "resolution": f.resolution} for f in state.state.foreshadows]

@app.get("/api/foreshadows/roadmap")
def get_roadmap():
    _ensure()
    md = foreshadow.build_foreshadow_roadmap(state.state)
    return {"markdown": md}

@app.post("/api/foreshadows/suggest")
async def suggest_foreshadows():
    _ensure(); _check()
    if not state.state.chapters: raise HTTPException(400, "请先生成大纲")
    state.task_running = True
    try:
        suggestions = await _run_blocking(
            foreshadow.suggest_foreshadows, state.llm_client, state.state)
        return {"foreshadows": suggestions}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.post("/api/foreshadows")
def create_foreshadow(req: FSCreate):
    _ensure(); _check()
    fs = Foreshadow(id=foreshadow.next_foreshadow_id(state.state.foreshadows),
                    name=req.name, description=req.description,
                    plant_chapter=req.plant_chapter, target_chapter=req.target_chapter)
    state.state.foreshadows.append(fs)
    storage.save_progress(state.progress_path, state.state)
    return {"id": fs.id, "name": fs.name}

@app.put("/api/foreshadows/{fid}")
def update_foreshadow(fid: int, req: FSUp):
    _ensure(); _check()
    for f in state.state.foreshadows:
        if f.id == fid:
            if req.name: f.name = req.name
            if req.description: f.description = req.description
            if req.plant_chapter: f.plant_chapter = req.plant_chapter
            if req.target_chapter: f.target_chapter = req.target_chapter
            if req.status:
                try: f.status = ForeshadowStatus(req.status)
                except: pass
            if req.resolution: f.resolution = req.resolution
            storage.save_progress(state.progress_path, state.state)
            return {"id": fid}
    raise HTTPException(404, "伏笔不存在")

@app.delete("/api/foreshadows/{fid}")
def delete_foreshadow(fid: int):
    _ensure(); _check()
    state.state.foreshadows = [f for f in state.state.foreshadows if f.id != fid]
    storage.save_progress(state.progress_path, state.state)
    return {"status": "deleted"}

@app.post("/api/foreshadows/confirm")
def confirm_foreshadows(req: dict):
    _ensure(); _check()
    for item in req.get("foreshadows", []):
        fs = Foreshadow(id=foreshadow.next_foreshadow_id(state.state.foreshadows),
                        name=item.get("name",""), description=item.get("description",""),
                        plant_chapter=item.get("plant_chapter",0),
                        target_chapter=item.get("target_chapter",0))
        state.state.foreshadows.append(fs)
    storage.save_progress(state.progress_path, state.state)
    return {"status": "confirmed"}


# ─── Task / Auto Confirm ───

@app.get("/api/auto-confirm")
def get_auto():
    return {"enabled": state.auto_confirm}

@app.put("/api/auto-confirm")
def put_auto(req: AutoC):
    state.auto_confirm = req.enabled
    return {"enabled": state.auto_confirm}

@app.post("/api/task/stop")
def stop_task():
    state.task_running = False
    return {"status": "stopping"}


# ─── Export / Version / Status ───

@app.get("/api/version")
def version(): return {"version": state.version}

@app.get("/api/status")
def status():
    return {"phase": state.state.phase, "title": state.state.title,
            "total_chapters": len(state.state.chapters),
            "is_task_running": state.task_running,
            "project_language": state.cfg.get("language","zh") if state.cfg else "zh",
            "auto_confirm": state.auto_confirm}

@app.get("/api/export/txt")
def export_txt():
    _ensure()
    lines = [state.state.title or state.project_name]
    for ch in state.state.chapters:
        if ch.content:
            lines.append(f"\n\n第 {ch.num} 章 {ch.title}\n\n{ch.content}")
    return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")

@app.get("/api/health")
def health():
    return {"status": "ok", "project": state.project_name or "(未选择)"}


# ─── SSE ───

@app.get("/api/sse")
def sse_stream(request: Request):
    import asyncio
    async def generate():
        yield "data: {}\n\n".format(json.dumps({"type":"connected"}))
    return StreamingResponse(generate(), media_type="text/event-stream")


# ─── Arc/卷系统 ───

@app.post("/api/arcs/skeleton")
async def arc_skeleton():
    _ensure(); _check()
    for ch in state.state.chapters:
        if ch.status in (ChapterStatus.ACCEPTED, ChapterStatus.WRITING, ChapterStatus.REVIEW):
            raise HTTPException(409, "存在已确认/写作中的章节")
    state.task_running = True
    try:
        state.state.arcs = await _run_blocking(
            arcs.generate_arc_skeleton, state.llm_client, state.cfg, state.state, state.settings)
        state.state.chapters = []
        state.state.current_chapter_index = 0
        state.state.phase = "outline"
        storage.save_progress(state.progress_path, state.state)
        return {"arcs": [{"id": a.id, "title": a.title, "goal": a.goal, "start_ch": a.start_ch, "end_ch": a.end_ch} for a in state.state.arcs]}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.post("/api/arcs/{arc_id}/outline")
async def arc_outline(arc_id: int, req: dict = None):
    _ensure(); _check()
    body = req or {}
    state.task_running = True
    try:
        chapters = await _run_blocking(
            arcs.generate_arc_outline, state.llm_client, state.cfg, state.state,
            state.settings, arc_id, body.get("requirements", ""))
        # 替换该卷范围内的章节
        arc_obj = state.state.arcs[arcs.arc_index_by_id(state.state, arc_id)]
        kept = [c for c in state.state.chapters if c.num < arc_obj.start_ch or c.num > arc_obj.end_ch]
        kept.extend(chapters)
        kept.sort(key=lambda c: c.num)
        state.state.chapters = kept
        storage.save_progress(state.progress_path, state.state)
        return state.state.to_dict()
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.post("/api/arcs/append")
async def arc_append(req: dict = None):
    _ensure(); _check()
    body = req or {}
    state.task_running = True
    try:
        arc = await _run_blocking(
            arcs.append_arc, state.llm_client, state.cfg, state.state, state.settings,
            body.get("title",""), body.get("goal",""), body.get("chapter_count", 20))
        storage.save_progress(state.progress_path, state.state)
        return {"id": arc.id, "title": arc.title, "start_ch": arc.start_ch, "end_ch": arc.end_ch}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False


# ─── 技能系统 ───

@app.get("/api/skills")
def get_skills():
    all_skills = skills_mod.load_all_skills(state.project_dir(), state.cfg.get("language", "zh"))
    sc = state.cfg.get("skill_config", {})
    enabled = sc.get("enabled_skills", {})
    return [{"id": s.id, "name": s.name, "description": s.description,
             "category": s.category, "enabled": enabled.get(s.id, False),
             "lang": s.lang, "source": s.source} for s in all_skills]

@app.put("/api/skills/{skill_id}")
def toggle_skill(skill_id: str, req: dict = None):
    _ensure(); _check()
    body = req or {}
    sc = state.cfg.setdefault("skill_config", {})
    enabled = sc.setdefault("enabled_skills", {})
    enabled[skill_id] = body.get("enabled", not enabled.get(skill_id, False))
    storage.save_story_config(state.cfg_path, state.cfg)
    return {"id": skill_id, "enabled": enabled[skill_id]}


# ─── 大纲角色检查 ───

@app.post("/api/outline/characters/check")
async def outline_character_check():
    _ensure(); _check()
    state.task_running = True
    try:
        report = await _run_blocking(
            reconcile.check_outline_characters, state.llm_client, state.state, state.settings, state.cfg)
        return {"has_suggestions": report.has_suggestions, "suggestions": [{"name": s.name, "chapter_num": s.chapter_num, "description": s.description, "role": s.role} for s in report.suggestions], "summary": report.summary}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False


# ─── 设定协调 ───

@app.post("/api/settings/reconcile")
async def settings_reconcile(req: StoryUp):
    _ensure(); _check()
    state.task_running = True
    try:
        new_cfg = {"type": req.type, "title": req.title, "writing_style": req.writing_style, "writing_pov": req.writing_pov, "story_synopsis": req.story_synopsis}
        explanation = await _run_blocking(
            reconcile.reconcile_settings, state.llm_client, state.cfg, state.state, new_cfg, state.settings)
        story = state.cfg.setdefault("story", {})
        for k, v in new_cfg.items():
            if v: story[k] = v
        storage.save_story_config(state.cfg_path, state.cfg)
        return {"explanation": explanation, "status": "reconciled"}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False


# ─── 全书优化 ───

@app.post("/api/book/diagnosis")
async def book_diagnosis_api():
    _ensure(); _check()
    state.task_running = True
    try:
        diag = await _run_blocking(
            reconcile.book_diagnosis, state.llm_client, state.state, state.settings, state.cfg)
        return {"diagnosis": diag}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.post("/api/book/consistency")
async def book_consistency_api():
    _ensure(); _check()
    state.task_running = True
    try:
        check = await _run_blocking(
            reconcile.book_consistency_check, state.llm_client, state.state, state.settings, state.cfg)
        return {"consistency": check}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.post("/api/book/roadmap")
async def book_roadmap_api(req: dict = None):
    _ensure(); _check()
    body = req or {}
    state.task_running = True
    try:
        roadmap = await _run_blocking(
            reconcile.book_roadmap, state.llm_client, body.get("diagnosis", ""), body.get("consistency", ""))
        return {"items": [{"chapter_num": i.chapter_num, "type": i.type, "priority": i.priority, "feedback": i.feedback, "selected": i.selected} for i in roadmap.items]}
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False


# ─── 写作增强 ───

@app.post("/api/chapters/polish")
async def polish_chapter(req: dict = None):
    """单章润色（去AI味）"""
    _ensure(); _check()
    body = req or {}
    chapter_num = body.get("chapter_num", state.state.current_chapter_index + 1)
    state.task_running = True
    try:
        for ch in state.state.chapters:
            if ch.num == chapter_num and ch.content:
                # 加载启用的润色技能
                sc = state.cfg.get("skill_config", {})
                enabled = sc.get("enabled_skills", {})
                all_s = skills_mod.load_all_skills(state.project_dir(), state.cfg.get("language","zh"))
                polish_skills = [s for s in all_s if s.category == "polish" and enabled.get(s.id)]
                skills_content = skills_mod.format_skills_content(polish_skills)
                if not skills_content:
                    raise HTTPException(400, "未启用润色技能")
                user_prompt = f"""请根据以下规则对下面的章节正文进行去AI味处理，输出修改后的完整正文。不要添加章节标题、章节号、\"本章完\"等任何元信息或说明性文字。

## 润色规则

{skills_content}

## 待处理正文

{ch.content}"""
                result = await _run_blocking(
                    state.llm_client.call,
                    "你是一位专业小说润色编辑。请只输出润色后的正文。",
                    user_prompt, temperature=0.3, max_tokens=8192)
                ch.content = inject.strip_chapter_meta(result)
                storage.save_chapter_markdown(state.project_dir(), ch, state.state.title)
                storage.save_progress(state.progress_path, state.state)
                return {"num": ch.num, "word_count": inject.count_prose_units(ch.content)}
        raise HTTPException(404, f"章节 {chapter_num} 不存在或无内容")
    except Exception as e: raise HTTPException(500, str(e))
    finally: state.task_running = False

@app.delete("/api/chapters/{num}")
def delete_chapter(num: int):
    """删除指定章节及之后所有章节"""
    _ensure(); _check()
    idx = -1
    for i, ch in enumerate(state.state.chapters):
        if ch.num == num: idx = i; break
    if idx == -1: raise HTTPException(404, "章节不存在")
    for i in range(idx, len(state.state.chapters)):
        if state.state.chapters[i].status == ChapterStatus.WRITING:
            raise HTTPException(409, f"第{state.state.chapters[i].num}章正在写作中，无法删除")
    state.state.chapters = state.state.chapters[:idx]
    if state.state.current_chapter_index >= idx:
        state.state.current_chapter_index = idx
    storage.save_progress(state.progress_path, state.state)
    return {"status": "deleted", "remaining": len(state.state.chapters)}

@app.get("/api/chapters/{num}/conflict")
def get_chapter_conflict(num: int):
    return {"conflict": {
        "chapter_num": state.state.pending_writing_conflict.chapter_num if state.state.pending_writing_conflict else 0,
        "issues": state.state.pending_writing_conflict.issues if state.state.pending_writing_conflict else [],
    }}

@app.post("/api/chapters/{num}/conflict/resolve")
def resolve_chapter_conflict(num: int, req: ConflictR):
    _ensure(); _check()
    if not state.state.pending_writing_conflict:
        raise HTTPException(400, "无待处理的冲突")
    if req.action == "force_review":
        idx = state.state.current_chapter_index
        if idx < len(state.state.chapters):
            state.state.chapters[idx].status = ChapterStatus.REVIEW
            state.state.pending_writing_conflict = None
            storage.save_progress(state.progress_path, state.state)
            return {"status": "kept_for_review"}
    elif req.action == "retry":
        state.state.pending_writing_conflict = None
        storage.save_progress(state.progress_path, state.state)
        return {"status": "retry"}
    raise HTTPException(400, "不支持的操作")


# ─── SPA 兜底（必须在所有 API 路由之后）───

if _has_frontend:
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """Serve Svelte SPA for all non-API/non-asset routes"""
        if full_path.startswith("api/"):
            raise HTTPException(404)
        fp = frontend_dir / (full_path or "index.html")
        if fp.exists() and fp.is_file():
            return FileResponse(str(fp))
        return FileResponse(str(frontend_dir / "index.html"))


# ─── Run ───

if __name__ == "__main__":
    import uvicorn
    # 默认只监听本机；如需局域网访问设置环境变量 NOVEL_HOST=0.0.0.0
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=58080)
