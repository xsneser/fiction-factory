"""api.json 的**唯一加载器** —— 全平台 API 配置真源。

背景：此前 6 处代码各自手写 `APIConfig(...)`，每处漏的字段都不一样
（`url_strict` / `max_tokens` / `context_budget_tokens` / `verify_ssl` 因此常年不生效），
设置页保存的配置在平台、工具脚本、dsh 侧栏之间各读各的。所有需要 API 配置的地方
一律走这里，不要再手写构造。

路径固定为**仓库根** `api.json`（与 cwd 无关），`ui/web_blueprints/settings.py`
的保存端点是唯一写入方。

字段语义：
  - `base_url`  —— **真实上游**地址（不是本地 token 代理；代理地址只是 dsh 的运行时传输细节）
  - `verify_ssl` —— 是否为上游校验 TLS 证书
  - `max_tokens` —— 0 = 用 `core.llm_client.DSH_MAX_TOKENS`；>0 时覆盖（逃生阀，不进 UI）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.json_store import read_json
from core.models import APIConfig
from core.safe_paths import parse_int

REPO_ROOT = Path(__file__).resolve().parent.parent
API_CONFIG_FILENAME = "api.json"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_HTTP_TIMEOUT_SECONDS = 300
DEFAULT_CONTEXT_BUDGET_TOKENS = 300000
DEFAULT_VERIFY_SSL = True
DEFAULT_URL_STRICT = False

# 示例配置里的占位 key 不算"已配置"，否则设置页会误报已就绪
PLACEHOLDER_API_KEYS = frozenset({"sk-your-api-key-here", "sk-xxx", "your-api-key"})

# 与 settings.py 的保存校验保持一致
TIMEOUT_MIN, TIMEOUT_MAX = 5, 1800
CONTEXT_BUDGET_MIN, CONTEXT_BUDGET_MAX = 8000, 2000000


def api_config_path(path: str | Path | None = None) -> Path:
    """api.json 的位置；始终是仓库根（传 path 仅供测试注入）。"""
    return Path(path) if path is not None else REPO_ROOT / API_CONFIG_FILENAME


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _as_bool(value: Any, default: bool) -> bool:
    """不要用 bool(value)：字符串 "false" 会被判成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return default


def load_api_config_raw(path: str | Path | None = None) -> dict:
    """读原始 JSON；文件缺失/损坏/不是对象时返回空 dict（不抛错、不联网）。"""
    cfg_path = api_config_path(path)
    if not cfg_path.exists():
        return {}
    data = read_json(str(cfg_path), {})
    return data if isinstance(data, dict) else {}


def api_config_from_mapping(data: Mapping[str, Any] | None) -> APIConfig:
    """把 api.json 的原始 dict 完整映射成 APIConfig（缺字段走默认值）。"""
    raw: Mapping[str, Any] = data or {}
    return APIConfig(
        api_key=_as_text(raw.get("api_key")),
        base_url=_as_text(raw.get("base_url")) or DEFAULT_BASE_URL,
        url_strict=_as_bool(raw.get("url_strict"), DEFAULT_URL_STRICT),
        model=_as_text(raw.get("model")) or DEFAULT_MODEL,
        # 0 = 交给 LLMClient 用 DSH_MAX_TOKENS（见 core/llm_client.py）
        max_tokens=max(parse_int(raw.get("max_tokens"), 0, min_value=0), 0),
        http_timeout_seconds=parse_int(
            raw.get("http_timeout_seconds"), DEFAULT_HTTP_TIMEOUT_SECONDS,
            min_value=TIMEOUT_MIN, max_value=TIMEOUT_MAX),
        context_budget_tokens=parse_int(
            raw.get("context_budget_tokens"), DEFAULT_CONTEXT_BUDGET_TOKENS,
            min_value=CONTEXT_BUDGET_MIN, max_value=CONTEXT_BUDGET_MAX),
        verify_ssl=_as_bool(raw.get("verify_ssl"), DEFAULT_VERIFY_SSL),
    )


def load_api_config(path: str | Path | None = None) -> APIConfig | None:
    """从 canonical api.json 构造完整 APIConfig；文件不存在返回 None。"""
    cfg_path = api_config_path(path)
    if not cfg_path.exists():
        return None
    return api_config_from_mapping(load_api_config_raw(path))


def is_api_configured(cfg: APIConfig | None) -> bool:
    """key + base_url 齐备且 key 不是示例占位值，才算真正可用。"""
    if cfg is None:
        return False
    if not cfg.api_key or cfg.api_key in PLACEHOLDER_API_KEYS:
        return False
    return bool(cfg.base_url)


def redact_secret(value: str, keep: int = 8) -> str:
    """掩码密钥：保留前 keep 位。任何日志/错误信息都不得出现明文 key。"""
    if not value:
        return ""
    return value[:keep] + "****"
