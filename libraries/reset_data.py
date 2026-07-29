"""一键重置四大库到内置初始状态"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

# 数据文件路径
data_dir = Path(__file__).parent / "data"

deleted = []
if data_dir.exists():
    for f in data_dir.iterdir():
        if f.suffix == ".json":
            f.unlink()
            deleted.append(f.name)

# 也清理运行时缓存
cache_dir = Path(__file__).parent.parent / "storage"
if cache_dir.exists():
    import shutil
    shutil.rmtree(cache_dir, ignore_errors=True)
    deleted.append("storage/")

if deleted:
    print(f"✅ 已清除: {', '.join(deleted)}")
    print("重启 Flask 后四大库将恢复为内置初始状态")
else:
    print("ℹ️ 没有需要清除的持久化数据（已是初始状态）")
