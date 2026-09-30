"""角色全员只读投影与最近出场排序测试。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libraries.character_state import CharacterStateMachine, project_character_roster


def main():
    bible = [
        {"name": "甲", "role": "主角", "importance": 1, "identity": "队长"},
        {"name": "乙", "role": "配角", "importance": 2, "identity": "医师"},
        {"name": "丙", "role": "配角", "importance": 3, "identity": "侦察员"},
        {"name": "丁", "role": "其他", "importance": 4, "identity": "后勤"},
    ]
    csm = CharacterStateMachine()
    csm.register("甲", identity="队长")
    csm.register("乙", identity="医师")
    csm.register("丙", identity="侦察员")
    csm.get("甲").last_appeared_chapter = 2
    csm.get("乙").last_appeared_chapter = 5
    csm.get("乙").location = "北门"
    csm.get("乙").goal = "寻找药材"
    csm.get("乙").power_level = "二阶"
    csm.get("乙").events.append({"type": "location_shift", "to": "北门", "chapter": 5})

    roster = project_character_roster(bible, csm)
    assert [x["name"] for x in roster] == ["乙", "甲", "丙", "丁"], roster
    assert roster[-1]["name"] == "丁" and roster[-1]["dyn"] == {}
    assert roster[0]["dyn"]["location"] == "北门"
    assert roster[0]["latest_event"]["type"] == "location_shift"

    staged = [{"name": "丙", "events": [{"type": "location_shift", "to": "地下站"},
                                           {"type": "goal_shift", "to": "接应甲"}]}]
    roster = project_character_roster(bible, csm, staged_events=staged, writing_chapter=6)
    assert [x["name"] for x in roster] == ["丙", "乙", "甲", "丁"], roster
    staged_card = roster[0]
    assert staged_card["state_source"] == "accepted_staged"
    assert staged_card["dyn"]["location"] == "地下站"
    assert staged_card["dyn"]["goal"] == "接应甲"
    assert staged_card["latest_event"]["staged"] is True
    print("[ALL PASSED] character roster projection")


if __name__ == "__main__":
    main()
