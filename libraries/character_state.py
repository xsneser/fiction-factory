"""
角色状态自动机（Character State Machine）
追踪每个角色在每章后的动态状态变化
"""
from dataclasses import dataclass, field
import json
from datetime import datetime


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
    last_appeared_chapter: int = 0    # 最近出场章节
    offline_chapters: int = 0         # 连续离线章节数
    arc_stage: str = ""               # 弧线阶段
    notes: str = ""                   # 其他备注


class CharacterStateMachine:
    """角色状态自动机"""

    def __init__(self):
        self.characters: list[CharacterState] = []

    def register(self, name: str, identity: str = "",
                 initial_location: str = "", power_level: str = "") -> CharacterState:
        """注册新角色"""
        # 去重
        for c in self.characters:
            if c.name == name:
                return c

        cs = CharacterState(
            name=name, identity=identity,
            location=initial_location, power_level=power_level,
            mood="正常", goal=""
        )
        self.characters.append(cs)
        return cs

    def get(self, name: str) -> CharacterState | None:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    def update_from_chapter(self, chapter_num: int,
                             chapter_content: str,
                             llm_client=None) -> list[CharacterState]:
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

        # 如有 LLM，可以提取更多状态变化
        if llm_client and updated:
            self._llm_extract_states(chapter_content, updated, llm_client)

        return updated

    def _llm_extract_states(self, content: str, characters: list[CharacterState],
                            llm_client):
        """用 LLM 从章节中提取角色状态变化"""
        char_names = [c.name for c in characters]
        prompt = f"""从以下章节中提取各角色的状态变化：

章节内容：
{content[:2000]}

需要追踪的角色：{', '.join(char_names)}

请以 JSON 格式返回：
{{"updates": [
  {{"name": "角色名", "location": "当前位置", "mood": "情绪",
    "goal": "当前目标", "power_level": "实力变化",
    "relationship_change": "与主角关系变化"}}
]}}"""

        try:
            from core.llm_client import extract_json
            raw = llm_client.call("你是一位精准的小说角色状态追踪员。",
                                  prompt, temperature=0.2, max_tokens=1024)
            data = json.loads(extract_json(raw))
            update_map = {u["name"]: u for u in data.get("updates", [])}

            for cs in characters:
                u = update_map.get(cs.name)
                if u:
                    if u.get("location"): cs.location = u["location"]
                    if u.get("mood"): cs.mood = u["mood"]
                    if u.get("goal"): cs.goal = u["goal"]
                    if u.get("power_level"): cs.power_level = u["power_level"]
        except Exception:
            pass

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
            if c.identity:
                parts.append(f"身份：{c.identity}")
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
