"""
JSON 库基类 — 资产库（桥段/大纲/笑点/角色原型/摘录）共用的单例 + 磁盘读写样板。

子类只需声明：
  _instance     : 本类自己的单例槽位（必须覆写，否则四库共享同一实例）
  _list_attr    : 条目列表属性名（templates / patterns / entries）
  _key          : JSON 顶层键（templates / patterns / entries）
  _file_name    : 数据文件名（plots.json / structures.json / gags.json / characters.json / excerpts.json）
  _from_dict    : dict → 条目对象
  _builtin      : 内置条目列表（持久文件不存在时使用）
"""
from pathlib import Path

from core.json_store import read_json, write_json_atomic


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
            data = read_json(self._save_path, {})
            items = [self._from_dict(d) for d in data.get(self._key, [])]
        else:
            items = list(self._builtin())
        setattr(self, self._list_attr, items)

    def _save(self):
        write_json_atomic(
            self._save_path,
            {self._key: [t.to_dict() for t in getattr(self, self._list_attr)]},
        )

    def load(self, path: str):
        data = read_json(path, {})
        setattr(self, self._list_attr,
                [self._from_dict(d) for d in data.get(self._key, [])])

    def save(self, path: str):
        write_json_atomic(
            path,
            {self._key: [t.to_dict() for t in getattr(self, self._list_attr)]},
        )
