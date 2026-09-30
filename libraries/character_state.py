"""
角色状态自动机（Character State Machine）
追踪每个角色在每章后的动态状态变化
"""
from dataclasses import dataclass, field
import json


@dataclass
class CharacterState:
    """单个角色的动态状态"""
    name: str = ""                    # 角色名
    identity: str = ""                # 身份
    location: str = ""                # 当前位置
    mood: str = ""                    # 当前情绪
    goal: str = ""                    # 当前目标
    power_level: str = ""             # 当前实力/境界
    relationship_to_mc: str = ""      # 与主角关系
    gender: str = ""                  # 性别（男/女，防"她"字错误）
    personality: str = ""             # 性格
    catchphrase: str = ""             # 惯用语句/口头禅
    brief: str = ""                   # 简介
    last_appeared_chapter: int = 0    # 最近出场章节
    offline_chapters: int = 0         # 连续离线章节数
    arc_stage: str = ""               # 弧线阶段
    notes: str = ""                   # 其他备注
    # —— 推断字段（禁 agent 直写；secret 永不由 agent 设置，防自报污染人物）——
    conflict: str = ""
    secret: str = ""
    emotional_pressure: str = ""
    # 人物变化事件台账（剧情造成的变化）：每项 {type, from?, to?, reason?, chapter}
    events: list = field(default_factory=list)


# agent 可上报的剧情事件 type → 允许更新的字段（映射到的才被写；mood/conflict/secret/
# emotional_pressure 不在映射内 = 禁 agent 直写，仅作推断字段预留）。
_EVENT_FIELD = {
    "goal_shift": "goal",
    "power_shift": "power_level",
    "location_shift": "location",
    "arc_stage": "arc_stage",
    "relationship": "relationship_to_mc",
    "trust_change": "relationship_to_mc",   # 关系向变化（写 relationship_to_mc + 台账）
}
_ALLOWED_EVENT_TYPES = set(_EVENT_FIELD) | {"note"}


def project_character_roster(bible, csm=None, staged_events=None, writing_chapter=0):
    """合并静态人物与动态状态，生成供 UI 使用的全员角色投影。

    这是只读投影：不注册人物、不写盘，也不改变 Writer 使用的 cast_pack。
    ``staged_events`` 必须已经由调用方按评审状态过滤；这里只按白名单应用到
    内存副本，并以最近出场章节和稳定的 bible 顺序排序。
    """
    bible = list(bible or [])
    states = {c.name: c for c in ((csm.characters if csm else []) or []) if c.name}
    staged_events = list(staged_events or [])
    staged_by_name = {}
    for order, item in enumerate(staged_events):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        row = staged_by_name.setdefault(name, {"events": [], "order": order})
        row["order"] = order
        for event in item.get("events") or []:
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "").strip()
            if etype not in _ALLOWED_EVENT_TYPES:
                continue
            row["events"].append(dict(event))

    roster = []
    seen = set()
    for static_order, raw in enumerate(bible):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        state = states.get(name)
        dyn = {}
        latest_event = None
        if state is not None:
            for field_name in ("location", "goal", "power_level", "relationship_to_mc", "arc_stage", "mood"):
                value = getattr(state, field_name, "")
                if value is not None and str(value).strip():
                    dyn[field_name] = str(value).strip()
            events = [e for e in (getattr(state, "events", []) or []) if isinstance(e, dict)]
            if events:
                latest_event = dict(events[-1])

        staged = staged_by_name.get(name)
        if staged:
            for event in staged["events"]:
                etype = str(event.get("type") or "")
                target = _EVENT_FIELD.get(etype)
                if target and event.get("to") is not None:
                    dyn[target] = str(event.get("to") or "").strip()
                latest_event = dict(event)
            if latest_event:
                latest_event["staged"] = True

        last_chapter = int(getattr(state, "last_appeared_chapter", 0) or 0) if state else 0
        appearance_order = 0
        if staged and writing_chapter:
            last_chapter = max(last_chapter, int(writing_chapter))
            appearance_order = int(staged.get("order") or 0) + 1
        roster.append({
            "name": name,
            "role": str(raw.get("role") or ""),
            "importance": int(raw.get("importance") or 2),
            "identity": str(raw.get("identity") or raw.get("title") or ""),
            "title": str(raw.get("title") or ""),
            "dyn": dyn,
            "latest_event": latest_event or {},
            "last_appeared_chapter": last_chapter,
            "appearance_order": appearance_order,
            "static_order": static_order,
            "state_source": "accepted_staged" if staged else "canonical",
        })
    roster.sort(key=lambda c: (-c["last_appeared_chapter"], -c["appearance_order"], c["static_order"]))
    return roster


class CharacterStateMachine:
    """角色状态自动机"""

    def __init__(self):
        self.characters: list[CharacterState] = []

    def register(self, name: str, identity: str = "",
                 initial_location: str = "", power_level: str = "",
                 gender: str = "", personality: str = "",
                 catchphrase: str = "", brief: str = "",
                 relationship_to_mc: str = "") -> CharacterState:
        """注册新角色（重名返回已有，不覆盖动态状态）"""
        for c in self.characters:
            if c.name == name:
                # 只填空字段（不覆盖已有动态状态）
                if gender and not c.gender: c.gender = gender
                if personality and not c.personality: c.personality = personality
                if catchphrase and not c.catchphrase: c.catchphrase = catchphrase
                if brief and not c.brief: c.brief = brief
                if relationship_to_mc and not c.relationship_to_mc: c.relationship_to_mc = relationship_to_mc
                return c

        cs = CharacterState(
            name=name, identity=identity,
            location=initial_location, power_level=power_level,
            mood="正常", goal="",
            gender=gender, personality=personality,
            catchphrase=catchphrase, brief=brief,
            relationship_to_mc=relationship_to_mc,
        )
        self.characters.append(cs)
        return cs

    def _mc_name(self, bible) -> str:
        """bible（basic_info.characters）里取主角名（role==主角，importance==1 兜底）。"""
        for c in (bible or []):
            if str(c.get("role", "") or "").strip() == "主角" \
                    and str(c.get("name", "") or "").strip():
                return str(c["name"]).strip()
        for c in (bible or []):
            if str(c.get("name", "") or "").strip() and int(c.get("importance") or 0) == 1:
                return str(c["name"]).strip()
        return ""

    def ensure_registered(self, bible) -> None:
        """按 bible（basic_info.characters）就地注册全部角色（修 agent 薄工具流 csm 空机缺口）。

        未注册 → 用静态字段 bootstrap（含 relation_to_mc 推导）；已注册只填空字段不覆盖动态量。
        每章写盘前都调，保证 apply_events / update_from_chapter 有角色可写。
        """
        if not bible:
            return
        mc_name = self._mc_name(bible)
        for c in (bible or []):
            if not str(c.get("name", "") or "").strip():
                continue
            rel = ""
            if mc_name:
                for r in (c.get("relations") or []):
                    if isinstance(r, dict) and str(r.get("name", "") or "").strip() == mc_name:
                        rel = str(r.get("relation", "") or "")
                        break
            self.register(str(c["name"]).strip(),
                          identity=str(c.get("identity", "") or ""),
                          gender=str(c.get("gender", "") or ""),
                          personality=str(c.get("personality", "") or ""),
                          catchphrase=str(c.get("catchphrase", "") or ""),
                          brief=str(c.get("brief", "") or ""),
                          relationship_to_mc=rel)

    def apply_events(self, events, chapter_num, bible=None) -> list:
        """剧情造成的人物变化事件落账（纯规则，无 LLM；agent 按情节段上报）。

        events: [{name, events:[{type, from?, to?, reason?}]}]
          type ∈ goal_shift|power_shift|location_shift|arc_stage|relationship|trust_change|note
          → 白名单字段映射写入 + 全部 append 进角色 events 台账（补 chapter）。
          mood/conflict/secret/emotional_pressure 不在白名单 = 禁 agent 直写（推断字段）。
        bible: basic_info.characters 列表。未注册角色按其静态字段就地 bootstrap 注册
          （修 agent 薄工具流 csm 空机缺口）；已注册只填空字段不覆盖动态量。
        返回被触动的 CharacterState 列表。
        """
        self.ensure_registered(bible)
        if not events:
            return []
        applied = []
        for item in events:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            cs = self.get(name)
            if cs is None:                     # bible 与已注册都没有 → 不凭空造角色
                continue
            evs = item.get("events") or []
            if not isinstance(evs, list):
                continue
            touched = False
            for e in evs:
                if not isinstance(e, dict):
                    continue
                t = str(e.get("type", "") or "").strip()
                if t not in _ALLOWED_EVENT_TYPES:
                    continue
                to = e.get("to")
                frm = e.get("from")
                reason = str(e.get("reason", "") or "").strip()
                if t in _EVENT_FIELD and to is not None:
                    setattr(cs, _EVENT_FIELD[t], str(to).strip())
                    touched = True
                entry = {"type": t, "chapter": int(chapter_num or 0)}
                if frm is not None and str(frm).strip():
                    entry["from"] = str(frm).strip()
                if to is not None and str(to).strip():
                    entry["to"] = str(to).strip()
                if reason:
                    entry["reason"] = reason
                cs.events.append(entry)
                touched = True
            if touched:
                applied.append(cs)
        return applied

    def get(self, name: str) -> CharacterState | None:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    def update_from_chapter(self, chapter_num: int,
                             chapter_content: str) -> list[CharacterState]:
        """
        根据章节内容更新所有角色的状态

        纯规则版（不需要 LLM）：
        - 标记出场角色（在内容中出现的）→ 更新 last_appeared + 重置 offline
        - 未出场角色 → offline_chapters += 1
        """
        updated = []
        for cs in self.characters:
            if cs.name in chapter_content:
                cs.last_appeared_chapter = chapter_num
                cs.offline_chapters = 0
                updated.append(cs)
            else:
                cs.offline_chapters += 1

        return updated

    def build_context_prompt(self, active_only: bool = True,
                             chapter_num: int = 0) -> str:
        """生成注入写作 prompt 的角色状态文本"""
        chars = self.characters
        if active_only and chapter_num:
            # 只包含最近出场的角色 + 即将出场的重要角色
            chars = [c for c in chars
                     if c.last_appeared_chapter >= chapter_num - 10
                     or c.offline_chapters > 50]  # 离线太久需要提醒

        if not chars:
            return ""

        parts = ["【角色当前状态——写作时注意维持一致性】"]
        for c in chars:
            parts.append(f"\n--- {c.name} ---")
            if c.gender:
                parts.append(f"性别：{c.gender}")
            if c.identity:
                parts.append(f"身份：{c.identity}")
            if c.personality:
                parts.append(f"性格：{c.personality}")
            # `catchphrase` 不再以「惯用语句」注入：把它当每次都要说的台词是口癖标签化的
            # 源头（book_002 七章 15 次）。留作数据字段（UI/兼容），提示改走稀疏标志短语。
            # 这里只保留「与主角关系」等真正影响说话方式的状态量。
            if c.location:
                parts.append(f"位置：{c.location}")
            if c.mood:
                parts.append(f"情绪：{c.mood}")
            if c.goal:
                parts.append(f"目标：{c.goal}")
            if c.power_level:
                parts.append(f"实力：{c.power_level}")
            if c.relationship_to_mc:
                parts.append(f"与主角关系：{c.relationship_to_mc}")
            if c.offline_chapters:
                if c.offline_chapters > 50:
                    parts.append(f"⚠️ 已离线 {c.offline_chapters} 章，读者快忘了这人了")
                elif c.offline_chapters > 10:
                    parts.append(f"已离线 {c.offline_chapters} 章")
        return "\n".join(parts) + "\n"

    def warnings(self) -> list[str]:
        """返回状态警告"""
        msgs = []
        for c in self.characters:
            if c.offline_chapters > 50:
                msgs.append(f"角色「{c.name}」已离线 {c.offline_chapters} 章，需要重新引入或收尾")
        return msgs

    def to_dict(self) -> dict:
        return {"characters": [
            {k: v for k, v in c.__dict__.items()}
            for c in self.characters
        ]}

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterStateMachine":
        csm = CharacterStateMachine()
        csm.characters = [CharacterState(**c) for c in d.get("characters", [])]
        return csm

    def load(self, path: str):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.characters = [CharacterState(**c) for c in d.get("characters", [])]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
