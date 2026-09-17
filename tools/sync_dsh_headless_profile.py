#!/usr/bin/env python3
"""同步 NovelEngine 的 dsh headless patch 到用户态 profile。"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent-sidecar" / "cordis.patch.yml"
TARGET = Path.home() / ".dsh" / "profiles" / "headless" / "cordis.patch.yml"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查是否一致")
    parser.add_argument("--apply", action="store_true", help="写入用户态 headless profile")
    args = parser.parse_args()
    if not SOURCE.exists():
        raise SystemExit(f"source profile missing: {SOURCE}")
    same = digest(SOURCE) == digest(TARGET)
    print(f"source: {SOURCE}")
    print(f"target: {TARGET}")
    print(f"一致: {'是' if same else '否'}")
    if args.apply:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, TARGET)
        print("已同步。重启下一次 dsh 任务后生效。")
        return 0
    if args.check or not args.apply:
        return 0 if same else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
