"""
角色状态自动机（Character State Machine）
追踪每个角色在每章后的动态状态变化
"""
from dataclasses import dataclass
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
            if c.catchphrase:
                parts.append(f"惯用语句：{c.catchphrase}")
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
