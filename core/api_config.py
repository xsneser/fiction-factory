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
from typing import Any, Mapping, Dict, List, Tuple

from core.json_store import read_json, write_json_atomic
from core.models import (
    APIConfig,
    ProviderModel,
    APIProviderConfig,
    AgentRouteConfig,
    APISettingsV2,
)
from core.safe_paths import parse_int

REPO_ROOT = Path(__file__).resolve().parent.parent
API_CONFIG_FILENAME = "api.json"

# 全局路径重定向（仅测试用）：置为 Path 后 api_config_path() 一律返回它。
# 否则测试里 patch 掉 REPO_ROOT 也改不动已绑定的模块常量，断言会读错文件。
API_PATH_OVERRIDE: Path | None = None

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

ROUTE_PREFIX = "novelengine-route:"

SUPPORTED_ROLES = (
    "orchestrator",
    "writer",
    "planner",
    "critic",
    "builder",
    "candidates",
    "scout",
    "publisher",
    "style",
)

ROLE_DESCRIPTIONS = {
    "orchestrator": ("主编排", "统领全文构思与收章决策，协调各专业子代理"),
    "writer": ("段落写手", "负责具体情节段与正文起草，执行文风与细节描写"),
    "critic": ("审查判决", "独立质量门禁，把关剧情逻辑硬伤与人物一致性"),
    "planner": ("剧情规划", "情节弧推演、故事线边界延伸与大纲重规划"),
    "builder": ("建书深化", "世界观差异化命题、核心矛盾与角色设定构建"),
    "candidates": ("选题初创", "新书灵感发散与故事候选生成"),
    "scout": ("侦察抓取", "外部热榜爆款分析与长篇小说顺序扫读提炼"),
    "publisher": ("上架合规", "完本与上架规则体检、导出投稿包"),
    "style": ("文风分析", "笔名风格规则提炼与全局样文池管理"),
    "backend": ("后端服务", "Python 后端直连服务与离线工具"),
}


def api_config_path(path: str | Path | None = None) -> Path:
    """api.json 的位置；默认仓库根（testing 可设本模块的 API_PATH_OVERRIDE 全局重定向）。"""
    if path is not None:
        return Path(path)
    if API_PATH_OVERRIDE is not None:
        return Path(API_PATH_OVERRIDE)
    return REPO_ROOT / API_CONFIG_FILENAME


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


def _parse_provider_model(raw: Any) -> ProviderModel | None:
    """Parse one model without inventing a model for malformed persisted data."""
    if isinstance(raw, str):
        mid = raw.strip()
        return ProviderModel(id=mid, name=mid) if mid else None
    if not isinstance(raw, dict):
        return None
    mid = _as_text(raw.get("id"))
    if not mid:
        return None
    name = _as_text(raw.get("name")) or mid
    efforts = raw.get("reasoning_efforts")
    if not isinstance(efforts, list) or not efforts:
        efforts = ["default"]
    clean_efforts = list(dict.fromkeys(_as_text(e) for e in efforts if _as_text(e))) or ["default"]
    return ProviderModel(
        id=mid,
        name=name,
        context_window=parse_int(raw.get("context_window"), 128000, min_value=0),
        max_tokens=parse_int(raw.get("max_tokens"), 8192, min_value=0),
        reasoning_efforts=clean_efforts,
        source=_as_text(raw.get("source")) or "manual",
    )


def _parse_provider(pid: str, raw: Mapping[str, Any]) -> APIProviderConfig:
    models_raw = raw.get("models")
    models: List[ProviderModel] = []
    if isinstance(models_raw, list):
        for m in models_raw:
            parsed = _parse_provider_model(m)
            if parsed is not None and parsed.id and not any(existing.id == parsed.id for existing in models):
                models.append(parsed)
    default_model = _as_text(raw.get("default_model"))
    if not default_model and models:
        default_model = models[0].id
    if not default_model:
        default_model = DEFAULT_MODEL
    if not any(m.id == default_model for m in models):
        models.insert(0, ProviderModel(id=default_model, name=default_model))

    return APIProviderConfig(
        id=pid,
        name=_as_text(raw.get("name")) or pid,
        enabled=_as_bool(raw.get("enabled"), True),
        protocol=_as_text(raw.get("protocol")) or "openai_chat",
        # Missing v2 provider URLs must stay missing so validation can fail closed.
        base_url=_as_text(raw.get("base_url")).rstrip("/"),
        api_key=_as_text(raw.get("api_key")),
        url_strict=_as_bool(raw.get("url_strict"), DEFAULT_URL_STRICT),
        verify_ssl=_as_bool(raw.get("verify_ssl"), DEFAULT_VERIFY_SSL),
        http_timeout_seconds=parse_int(
            raw.get("http_timeout_seconds"), DEFAULT_HTTP_TIMEOUT_SECONDS,
            min_value=TIMEOUT_MIN, max_value=TIMEOUT_MAX
        ),
        default_model=default_model,
        reasoning_wire=_as_text(raw.get("reasoning_wire")) or "default",
        chat_completions_endpoint=_as_text(raw.get("chat_completions_endpoint")),
        models_endpoint=_as_text(raw.get("models_endpoint")),
        models=models,
    )


def _parse_agent_route(raw: Any, default_provider: str, default_model: str) -> AgentRouteConfig:
    if not isinstance(raw, dict):
        return AgentRouteConfig(
            provider_id=default_provider,
            model=default_model,
            max_tokens=0,
            reasoning_effort="default",
        )
    return AgentRouteConfig(
        provider_id=_as_text(raw.get("provider_id")) or default_provider,
        model=_as_text(raw.get("model")) or default_model,
        max_tokens=max(parse_int(raw.get("max_tokens"), 0, min_value=0), 0),
        reasoning_effort=_as_text(raw.get("reasoning_effort")) or "default",
    )


def api_settings_from_mapping(data: Mapping[str, Any] | None) -> APISettingsV2:
    """把原始 JSON 映射成完整的 APISettingsV2（自动识别 v1 legacy 或 v2）。"""
    raw: Mapping[str, Any] = data or {}
    providers_raw = raw.get("providers")

    if isinstance(providers_raw, dict) and providers_raw:
        # v2 格式
        providers: Dict[str, APIProviderConfig] = {}
        for pid, pdata in providers_raw.items():
            if isinstance(pdata, dict):
                pid_clean = _as_text(pid)
                if pid_clean:
                    providers[pid_clean] = _parse_provider(pid_clean, pdata)

        def_prov = _as_text(raw.get("default_provider"))
        if not def_prov or def_prov not in providers:
            # 选第一个启用的 provider
            enabled_keys = [k for k, p in providers.items() if p.enabled]
            def_prov = enabled_keys[0] if enabled_keys else (list(providers.keys())[0] if providers else "primary")

        default_model = providers[def_prov].default_model if def_prov in providers else DEFAULT_MODEL

        # 解析 subagents 路由
        subagents_raw = raw.get("subagents")
        subagents: Dict[str, AgentRouteConfig] = {}
        if isinstance(subagents_raw, dict):
            for role in SUPPORTED_ROLES:
                if role in subagents_raw:
                    subagents[role] = _parse_agent_route(subagents_raw[role], def_prov, default_model)
        # 补全缺失的 roles
        for role in SUPPORTED_ROLES:
            if role not in subagents:
                subagents[role] = AgentRouteConfig(
                    provider_id=def_prov,
                    model=default_model,
                    max_tokens=0,
                    reasoning_effort="default",
                )

        # 解析 backend 路由
        backend_raw = raw.get("backend")
        backend = _parse_agent_route(backend_raw, def_prov, default_model)

        return APISettingsV2(
            version=2,
            default_provider=def_prov,
            providers=providers,
            subagents=subagents,
            backend=backend,
        )

    # v1 格式兼容：从顶层合成一个 primary provider 并绑定所有角色
    legacy_model = _as_text(raw.get("model")) or DEFAULT_MODEL
    legacy_provider = APIProviderConfig(
        id="primary",
        name="默认供应商",
        enabled=True,
        protocol="openai_chat",
        base_url=_as_text(raw.get("base_url")).rstrip("/") or DEFAULT_BASE_URL,
        api_key=_as_text(raw.get("api_key")),
        url_strict=_as_bool(raw.get("url_strict"), DEFAULT_URL_STRICT),
        verify_ssl=_as_bool(raw.get("verify_ssl"), DEFAULT_VERIFY_SSL),
        http_timeout_seconds=parse_int(
            raw.get("http_timeout_seconds"), DEFAULT_HTTP_TIMEOUT_SECONDS,
            min_value=TIMEOUT_MIN, max_value=TIMEOUT_MAX
        ),
        default_model=legacy_model,
        reasoning_wire="default",
        models=[ProviderModel(id=legacy_model, name=legacy_model)],
    )

    subagents = {
        role: AgentRouteConfig(
            provider_id="primary",
            model=legacy_model,
            max_tokens=max(parse_int(raw.get("max_tokens"), 0, min_value=0), 0),
            reasoning_effort="default",
        )
        for role in SUPPORTED_ROLES
    }
    backend = AgentRouteConfig(
        provider_id="primary",
        model=legacy_model,
        max_tokens=max(parse_int(raw.get("max_tokens"), 0, min_value=0), 0),
        reasoning_effort="default",
    )

    return APISettingsV2(
        version=2,
        default_provider="primary",
        providers={"primary": legacy_provider},
        subagents=subagents,
        backend=backend,
    )


def load_api_settings(path: str | Path | None = None) -> APISettingsV2:
    """加载完整 v2 API 设置对象（自动向下兼容）。"""
    raw = load_api_config_raw(path)
    return api_settings_from_mapping(raw)


def resolve_agent_route(
    role_or_model: str,
    settings: APISettingsV2 | None = None,
    path: str | Path | None = None,
) -> Tuple[APIProviderConfig, AgentRouteConfig]:
    """根据子代理角色或模型请求名解析出实际供应商与路由配置。

    规则：
      - 若 role_or_model 以 `novelengine-route:` 开头，提取角色名；
      - 若角色名在 subagents 或 backend 中，使用该角色的路由；
      - 否则使用 orchestrator 或 default_provider 路由；
      - 若目标 provider 禁用或不存在，安全回退到 default_provider 或首个可用 provider。
    """
    if settings is None:
        settings = load_api_settings(path)

    role = role_or_model.strip()
    if role.startswith(ROUTE_PREFIX):
        role = role[len(ROUTE_PREFIX):].strip()

    if role == "backend":
        route = settings.backend
    elif role in settings.subagents:
        route = settings.subagents[role]
    else:
        route = settings.subagents.get("orchestrator") or AgentRouteConfig(
            provider_id=settings.default_provider,
            model=DEFAULT_MODEL,
        )

    # 查 provider
    provider = settings.providers.get(route.provider_id)
    if not provider or not provider.enabled:
        # 回退到默认 provider
        provider = settings.providers.get(settings.default_provider)
        if not provider or not provider.enabled:
            # 选第一个启用的
            enabled = [p for p in settings.providers.values() if p.enabled]
            provider = enabled[0] if enabled else (
                list(settings.providers.values())[0] if settings.providers else APIProviderConfig()
            )

    # 模型确认：provider 回退后不能携带原 provider 的模型 ID。
    actual_model = route.model or provider.default_model or DEFAULT_MODEL
    if provider.models and not any(m.id == actual_model for m in provider.models):
        actual_model = provider.default_model or (provider.models[0].id if provider.models else "")
    # 创建解析后的实际路由副本
    effective_route = AgentRouteConfig(
        provider_id=provider.id,
        model=actual_model,
        max_tokens=route.max_tokens,
        reasoning_effort=route.reasoning_effort,
    )
    return provider, effective_route


def resolve_role_to_api_config(
    role: str = "backend",
    settings: APISettingsV2 | None = None,
    path: str | Path | None = None,
) -> APIConfig:
    """把指定角色解析为标准 APIConfig（给 Python 侧 LLMClient 使用）。"""
    if settings is None:
        settings = load_api_settings(path)
    provider, route = resolve_agent_route(role, settings)
    raw = load_api_config_raw(path)
    ctx_budget = parse_int(
        raw.get("context_budget_tokens"), DEFAULT_CONTEXT_BUDGET_TOKENS,
        min_value=CONTEXT_BUDGET_MIN, max_value=CONTEXT_BUDGET_MAX
    )
    return APIConfig(
        api_key=provider.api_key,
        base_url=provider.base_url,
        url_strict=provider.url_strict,
        model=route.model,
        max_tokens=route.max_tokens,
        http_timeout_seconds=provider.http_timeout_seconds,
        context_budget_tokens=ctx_budget,
        verify_ssl=provider.verify_ssl,
        real_base_url="",
        chat_completions_endpoint=provider.chat_completions_endpoint,
        provider_id=provider.id,
        reasoning_effort=route.reasoning_effort,
        reasoning_wire=provider.reasoning_wire,
    )


def api_settings_to_dict(settings: APISettingsV2, existing_raw: dict | None = None) -> dict:
    """序列化 APISettingsV2 为 dict，同时注入顶层影子字段保持向后兼容。"""
    providers_dict = {}
    for pid, p in settings.providers.items():
        providers_dict[pid] = {
            "id": p.id,
            "name": p.name,
            "enabled": p.enabled,
            "protocol": p.protocol,
            "base_url": p.base_url,
            "api_key": p.api_key,
            "url_strict": p.url_strict,
            "verify_ssl": p.verify_ssl,
            "http_timeout_seconds": p.http_timeout_seconds,
            "default_model": p.default_model,
            "reasoning_wire": p.reasoning_wire,
            "chat_completions_endpoint": p.chat_completions_endpoint,
            "models_endpoint": p.models_endpoint,
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "context_window": m.context_window,
                    "max_tokens": m.max_tokens,
                    "reasoning_efforts": m.reasoning_efforts,
                    "source": m.source,
                }
                for m in p.models
            ],
        }

    subagents_dict = {}
    for role, r in settings.subagents.items():
        subagents_dict[role] = {
            "provider_id": r.provider_id,
            "model": r.model,
            "max_tokens": r.max_tokens,
            "reasoning_effort": r.reasoning_effort,
        }

    backend_dict = {
        "provider_id": settings.backend.provider_id,
        "model": settings.backend.model,
        "max_tokens": settings.backend.max_tokens,
        "reasoning_effort": settings.backend.reasoning_effort,
    }

    # 查默认 provider 用于写入顶层影子字段
    def_p = settings.providers.get(settings.default_provider)
    if not def_p and settings.providers:
        def_p = list(settings.providers.values())[0]

    shadow_key = def_p.api_key if def_p else ""
    shadow_base = def_p.base_url if def_p else DEFAULT_BASE_URL
    shadow_model = def_p.default_model if def_p else DEFAULT_MODEL
    shadow_strict = def_p.url_strict if def_p else DEFAULT_URL_STRICT
    shadow_verify = def_p.verify_ssl if def_p else DEFAULT_VERIFY_SSL
    shadow_timeout = def_p.http_timeout_seconds if def_p else DEFAULT_HTTP_TIMEOUT_SECONDS

    ctx_budget = DEFAULT_CONTEXT_BUDGET_TOKENS
    if existing_raw:
        ctx_budget = parse_int(
            existing_raw.get("context_budget_tokens"), DEFAULT_CONTEXT_BUDGET_TOKENS,
            min_value=CONTEXT_BUDGET_MIN, max_value=CONTEXT_BUDGET_MAX
        )

    # 未暴露在 v2 UI 的旧字段仍从原文件保留，避免升级设置页时把 max_tokens
    # 逃生阀 / real_base_url 悄悄重置。
    legacy_max_tokens = 0
    legacy_real_base_url = ""
    if existing_raw:
        legacy_max_tokens = max(parse_int(existing_raw.get("max_tokens"), 0, min_value=0), 0)
        legacy_real_base_url = _as_text(existing_raw.get("real_base_url"))

    out = {
        "version": 2,
        "default_provider": settings.default_provider,
        "providers": providers_dict,
        "subagents": subagents_dict,
        "backend": backend_dict,
        # 顶层影子字段（兼容存量读取代码与脚本）
        "api_key": shadow_key,
        "base_url": shadow_base,
        "model": shadow_model,
        "url_strict": shadow_strict,
        "verify_ssl": shadow_verify,
        "http_timeout_seconds": shadow_timeout,
        "context_budget_tokens": ctx_budget,
        "max_tokens": legacy_max_tokens,
        "real_base_url": legacy_real_base_url,
    }
    return out


def save_api_settings(
    settings: APISettingsV2,
    path: str | Path | None = None,
    context_budget_tokens: int | None = None,
) -> None:
    """原子保存完整 v2 API 设置到 api.json。

    context_budget_tokens 单独作为可选参数，是因为它不属于某一供应商/角色；提供时
    在同一次 atomic write 中写入，避免设置页先写旧格式再写 v2 造成双写窗口。
    """
    cfg_path = api_config_path(path)
    existing = load_api_config_raw(path)
    if context_budget_tokens is not None:
        existing = dict(existing)
        existing["context_budget_tokens"] = int(context_budget_tokens)
    payload = api_settings_to_dict(settings, existing)
    write_json_atomic(str(cfg_path), payload)


def sanitize_api_settings_for_ui(settings: APISettingsV2) -> dict:
    """返回用于前端设置页的数据（API Key 掩码，附加角色说明和元数据）。"""
    providers_sanitized = {}
    for pid, p in settings.providers.items():
        providers_sanitized[pid] = {
            "id": p.id,
            "name": p.name,
            "enabled": p.enabled,
            "protocol": p.protocol,
            "base_url": p.base_url,
            "api_key_masked": redact_secret(p.api_key),
            "api_key_configured": bool(p.api_key and p.api_key not in PLACEHOLDER_API_KEYS),
            "url_strict": p.url_strict,
            "verify_ssl": p.verify_ssl,
            "http_timeout_seconds": p.http_timeout_seconds,
            "default_model": p.default_model,
            "reasoning_wire": p.reasoning_wire,
            "chat_completions_endpoint": p.chat_completions_endpoint,
            "models_endpoint": p.models_endpoint,
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "context_window": m.context_window,
                    "max_tokens": m.max_tokens,
                    "reasoning_efforts": m.reasoning_efforts,
                    "source": m.source,
                }
                for m in p.models
            ],
        }

    subagents_data = {}
    for role, r in settings.subagents.items():
        desc = ROLE_DESCRIPTIONS.get(role, (role, ""))
        subagents_data[role] = {
            "role": role,
            "title": desc[0],
            "description": desc[1],
            "provider_id": r.provider_id,
            "model": r.model,
            "max_tokens": r.max_tokens,
            "reasoning_effort": r.reasoning_effort,
        }

    backend_desc = ROLE_DESCRIPTIONS.get("backend", ("后端服务", ""))
    backend_data = {
        "role": "backend",
        "title": backend_desc[0],
        "description": backend_desc[1],
        "provider_id": settings.backend.provider_id,
        "model": settings.backend.model,
        "max_tokens": settings.backend.max_tokens,
        "reasoning_effort": settings.backend.reasoning_effort,
    }

    return {
        "version": 2,
        "default_provider": settings.default_provider,
        "providers": providers_sanitized,
        "subagents": subagents_data,
        "backend": backend_data,
        "supported_roles": list(SUPPORTED_ROLES),
    }


def api_config_from_mapping(data: Mapping[str, Any] | None) -> APIConfig:
    """把 api.json 的原始 dict 完整映射成 APIConfig（向后兼容）。"""
    raw: Mapping[str, Any] = data or {}
    if "providers" in raw and isinstance(raw["providers"], dict):
        # 从 v2 结构中解析 backend / default 的 APIConfig。
        # context_budget 必须取本次传入的 mapping，而不能让
        # resolve_role_to_api_config() 再读当前磁盘文件（调用方可能正在校验尚未落盘的草稿）。
        settings = api_settings_from_mapping(raw)
        cfg = resolve_role_to_api_config("backend", settings)
        cfg.context_budget_tokens = parse_int(
            raw.get("context_budget_tokens"), DEFAULT_CONTEXT_BUDGET_TOKENS,
            min_value=CONTEXT_BUDGET_MIN, max_value=CONTEXT_BUDGET_MAX)
        return cfg

    # 纯 v1 结构
    return APIConfig(
        api_key=_as_text(raw.get("api_key")),
        base_url=_as_text(raw.get("base_url")).rstrip("/") or DEFAULT_BASE_URL,
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
        # 旧版字段：仅当 base_url 指向本地 token 代理时才被代理当成上游
        real_base_url=_as_text(raw.get("real_base_url")),
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
