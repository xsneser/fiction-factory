"""
一次性脚本：重置 book_001 的写作进度，为「按桥段 × 短句组」新引擎重写做准备。

执行的动作（幂等）：
  1. 删除 chapters/0001.json（断裂第一章：沈宁穿越 + 林尘被逐两个故事拼接）
  2. 把 timeline.json 中 written_chapter==1 的桥段重置为 0（回到未写状态）
  3. 删除 draft_chapter.json（进行中章节草稿，若有）
  4. 清空 character_states.json（源自断裂章节，保留空结构）
  5. book.json 的 current_chapter 置 0（chapter_count 保留）

cost.json 保留（实际花费账单）。

用法：python tools/reset_book_001.py [book_id]
"""
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOOK_ID = sys.argv[1] if len(sys.argv) > 1 else "book_001"
BOOK_DIR = Path("books") / BOOK_ID


def main():
    if not BOOK_DIR.exists():
        print(f"❌ 目录不存在: {BOOK_DIR}")
        return 1

    # 1. 删除全部章节文件
    ch_dir = BOOK_DIR / "chapters"
    deleted = 0
    if ch_dir.exists():
        for f in ch_dir.glob("*.json"):
            f.unlink()
            deleted += 1
    print(f"✔ 已删除 {deleted} 个章节文件" if deleted else "· 无章节文件")

    # 2. 重置所有已写桥段的 written_chapter → 0（回到未写状态）
    tl_path = BOOK_DIR / "timeline.json"
    if tl_path.exists():
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
        reset = 0
        for p in tl.get("plots", []):
            if int(p.get("written_chapter", 0) or 0) > 0:
                p["written_chapter"] = 0
                reset += 1
        tl_path.write_text(json.dumps(tl, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✔ 已重置 {reset} 个桥段的 written_chapter → 0")
    else:
        print("· timeline.json 不存在，跳过")

    # 3. 删除草稿
    draft = BOOK_DIR / "draft_chapter.json"
    if draft.exists():
        draft.unlink()
        print(f"✔ 已删除 {draft.relative_to('books')}")
    else:
        print("· 无章节草稿")

    # 4. 清空角色状态
    cs = BOOK_DIR / "character_states.json"
    cs.write_text(json.dumps({"characters": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✔ 已清空 character_states.json")

    # 5. current_chapter 置 0
    bk_path = BOOK_DIR / "book.json"
    if bk_path.exists():
        bk = json.loads(bk_path.read_text(encoding="utf-8"))
        bk["current_chapter"] = 0
        bk_path.write_text(json.dumps(bk, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✔ book.json current_chapter → 0（chapter_count={bk.get('chapter_count')}）")

    print("✅ 重置完成。重启后端后在写作台点『▶ 写下一个桥段』即可用新引擎从 plot_0004 重写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
