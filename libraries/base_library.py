"""
JSON 库基类 — 四大资产库（桥段/大纲/笑点/内涵）共用的单例 + 磁盘读写样板。

子类只需声明：
  _instance     : 本类自己的单例槽位（必须覆写，否则四库共享同一实例）
  _list_attr    : 条目列表属性名（templates / patterns / entries）
  _key          : JSON 顶层键（templates / patterns / entries）
  _file_name    : 数据文件名（plots.json / structures.json / gags.json / themes.json）
  _from_dict    : dict → 条目对象
  _builtin      : 内置条目列表（持久文件不存在时使用）
"""
import json
from pathlib import Path


class JsonLibrary:
    _instance = None
    _list_attr = "items"
    _key = "items"
    _file_name = "items.json"

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, data_dir: str = ""):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        if data_dir:
            self._save_path = Path(data_dir) / self._file_name
        else:
            self._save_path = self._default_path()
        setattr(self, self._list_attr, [])
        self._load()

    def _default_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / self._file_name

    def _load(self):
        """优先读持久文件；文件不存在时回退到内置条目（保持原语义）。"""
        if self._save_path.exists():
            with open(self._save_path, encoding="utf-8") as f:
                data = json.load(f)
            items = [self._from_dict(d) for d in data.get(self._key, [])]
        else:
            items = list(self._builtin())
        setattr(self, self._list_attr, items)

    def _save(self):
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump(
                {self._key: [t.to_dict() for t in getattr(self, self._list_attr)]},
                f, ensure_ascii=False, indent=2,
            )

    def load(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        setattr(self, self._list_attr,
                [self._from_dict(d) for d in data.get(self._key, [])])

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {self._key: [t.to_dict() for t in getattr(self, self._list_attr)]},
                f, ensure_ascii=False, indent=2,
            )
