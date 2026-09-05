# -*- coding: utf-8 -*-
"""样本驱动风格的 md 源 / 参考样本读取(读磁盘,每次现读→手改即生效)。

- styles/<pen_name>.md              : 用户手写的风格文档(负约束/原则;可提交)。
- storage/style_refs/<pen_name>.reference.txt : STYLE REFERENCE 人工样本(整段场景+框架;
  版权样本不入库,故放 gitignored storage/)。缺文件 = 无样本(仅 md 模式)。
- 样本驱动判定:默认笔名存在 styles/<pen>.md → 服务端去 AI 词级替换跳过(见 de_ai)。
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STYLES = os.path.join(_ROOT, "styles")
_REFS = os.path.join(_ROOT, "storage", "style_refs")


def _pen(profile):
    return (getattr(profile, "pen_name", "") or "").strip()


def md_path(pen_name: str) -> str:
    return os.path.join(_STYLES, f"{pen_name}.md")


def ref_path(pen_name: str) -> str:
    return os.path.join(_REFS, f"{pen_name}.reference.txt")


def has_style_md(profile) -> bool:
    p = _pen(profile)
    return bool(p) and os.path.exists(md_path(p))


def read_style_md(profile):
    """返回 styles/<pen>.md 内容;无 md / 读失败 → None。"""
    p = _pen(profile)
    if not p:
        return None
    path = md_path(p)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def read_ref(profile):
    """返回 STYLE REFERENCE 样本全文(含框架);无样本文件 → None。"""
    p = _pen(profile)
    if not p:
        return None
    path = ref_path(p)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def default_profile():
    """默认笔名档案(DEFAULT_PROFILE_ID)——服务端去 AI 兜底读它,样本驱动判定同源。"""
    from .style_rules import StyleRuleLibrary, DEFAULT_PROFILE_ID
    from .profiles import ProfileManager
    return ProfileManager().get(DEFAULT_PROFILE_ID)
