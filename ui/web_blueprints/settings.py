"""设置 + 状态任务 + 调试 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

from core.api_config import (
    api_config_path, api_config_from_mapping, is_api_configured, load_api_config_raw,
    load_api_settings, save_api_settings, sanitize_api_settings_for_ui,
    ROUTE_PREFIX, SUPPORTED_ROLES, ROLE_DESCRIPTIONS,
    DEFAULT_MODEL, DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_CONTEXT_BUDGET_TOKENS, DEFAULT_VERIFY_SSL, DEFAULT_URL_STRICT,
    CONTEXT_BUDGET_MIN, CONTEXT_BUDGET_MAX, PLACEHOLDER_API_KEYS,
)
from core.models import (
    APIConfig, APISettingsV2, APIProviderConfig, AgentRouteConfig, ProviderModel,
)
from core.provider_discovery import (
    DiscoveryError, discover_provider_models, merge_discovered_models,
)

bp = Blueprint("settings", __name__)

@bp.route("/api/status/tasks")
def status_tasks():
    """返回当前运行中的任务列表（供右侧状态栏轮询）"""
    from plugins import task_manager
    tasks = task_manager.get_tasks()
    return jsonify(tasks)


@bp.route("/api/status/tasks/close", methods=["POST"])
def status_tasks_close():
    """关闭/删除指定任务（运行中的任务自动取消，已完成/失败的直接移除）"""
    from plugins import task_manager
    data = request.get_json()
    task_id = data.get("id", "")
    if task_id:
        # 如果还在运行则先取消
        task_manager.cancel(task_id)
        # 从列表移除
        task_manager.remove(task_id)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "missing id"})


def _mask_api_key(key: str) -> str:
    """掩码 API key：保留前 8 位，其余用 **** 代替（含 **** 即视为"未修改"）"""
    if not key:
        return ""
    return key[:8] + "****"


def _safe_error(error, secret="") -> str:
    """Return bounded diagnostics without echoing credentials or huge upstream bodies."""
    text = str(error or "请求失败")[:500]
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def _proxy_self_guard() -> str:
    """本地代理地址前缀（用来阻止用户把供应商地址填成代理自身 —— 那会形成转发自环）。"""
    try:
        from libraries.token_proxy import token_proxy_base_url
        return token_proxy_base_url()
    except Exception:
        return "http://127.0.0.1:58082"


@bp.route("/settings")
def settings_page():
    """设置页面 — 纯静态渲染，不发起 API 请求。

    页面主体由前端 settings.js 接管：这里只把**已脱敏**的 v2 配置快照序列化进
    HTML（没有明文 key），首屏无需额外请求。
    """
    settings = load_api_settings()
    view = sanitize_api_settings_for_ui(settings)
    typed = api_config_from_mapping(load_api_config_raw())

    return render_template("settings.html",
        settings_view=view,
        role_order=list(SUPPORTED_ROLES) + ["backend"],
        role_titles={r: ROLE_DESCRIPTIONS.get(r, (r, ""))[0]
                     for r in list(SUPPORTED_ROLES) + ["backend"]},
        role_descs={r: ROLE_DESCRIPTIONS.get(r, ("", ""))[1]
                    for r in list(SUPPORTED_ROLES) + ["backend"]},
        context_budget_tokens=typed.context_budget_tokens,
        llm_ok=_settings_ready(settings),
    )


@bp.route("/api/settings/config")
def settings_config():
    """返回脱敏后的完整 v2 配置（供应商 + 子代理路由），**绝不含明文 key**。"""
    settings = load_api_settings()
    view = sanitize_api_settings_for_ui(settings)
    typed = api_config_from_mapping(load_api_config_raw())
    view["context_budget_tokens"] = typed.context_budget_tokens
    view["configured"] = _settings_ready(settings)
    return jsonify({"ok": True, "config": view})


def _parse_bool(value, default=True):
    """Parse JSON booleans without treating the string ``false`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _parse_int_field(value, *, default, minimum=0, maximum=None, field="字段"):
    """Return (value, issue); reject booleans and malformed client values."""
    if value is None or value == "":
        return default, None
    if isinstance(value, bool):
        return default, f"{field}必须是整数"
    try:
        # Do not accept floats such as 1.5 or containers via int().
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError):
        return default, f"{field}必须是整数"
    if parsed < minimum or (maximum is not None and parsed > maximum):
        limit = f"，范围 {minimum}~{maximum}" if maximum is not None else f"，不得小于 {minimum}"
        return default, f"{field}无效{limit}"
    return parsed, None


def _parse_models(raw_models, issues=None, path="models") -> list:
    """把前端模型列表解析成 ProviderModel，遇到坏输入报告而非 500。"""
    out = []
    if raw_models is None:
        return out
    if not isinstance(raw_models, list):
        if issues is not None:
            issues.append({"path": path, "code": "invalid_list", "message": "模型列表必须是数组"})
        return out
    seen = set()
    for idx, m in enumerate(raw_models):
        item_path = f"{path}[{idx}]"
        if isinstance(m, str):
            mid = m.strip()
            if mid:
                out.append(ProviderModel(id=mid, name=mid))
            continue
        if not isinstance(m, dict):
            if issues is not None:
                issues.append({"path": item_path, "code": "invalid_model", "message": "模型项必须是对象"})
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            if issues is not None:
                issues.append({"path": f"{item_path}.id", "code": "empty_model_id", "message": "模型 ID 不能为空"})
            continue
        if mid in seen:
            if issues is not None:
                issues.append({"path": f"{item_path}.id", "code": "duplicate_model_id", "message": f"模型 ID 重复：{mid}"})
            continue
        seen.add(mid)
        context_window, context_issue = _parse_int_field(
            m.get("context_window"), default=128000, minimum=0, maximum=100000000,
            field="上下文窗口")
        max_tokens, max_issue = _parse_int_field(
            m.get("max_tokens"), default=8192, minimum=0, maximum=100000000,
            field="最大输出")
        if issues is not None:
            if context_issue:
                issues.append({"path": f"{item_path}.context_window", "code": "invalid_integer", "message": context_issue})
            if max_issue:
                issues.append({"path": f"{item_path}.max_tokens", "code": "invalid_integer", "message": max_issue})
        efforts = m.get("reasoning_efforts")
        if efforts is None or efforts == []:
            efforts = ["default"]
        if not isinstance(efforts, list):
            if issues is not None:
                issues.append({"path": f"{item_path}.reasoning_efforts", "code": "invalid_list", "message": "思考强度必须是数组"})
            efforts = ["default"]
        efforts = list(dict.fromkeys(str(e).strip() for e in efforts if str(e).strip())) or ["default"]
        out.append(ProviderModel(
            id=mid,
            name=str(m.get("name") or mid).strip() or mid,
            context_window=context_window,
            max_tokens=max_tokens,
            reasoning_efforts=efforts,
            source=str(m.get("source") or "manual").strip() or "manual",
        ))
    return out


def _merge_provider_secret(raw_key, old_key: str, clear: bool) -> str:
    """API Key 合并：掩码/空值 = 保留原 key；`clear_api_key` 显式清空。

    掩码（含 `****`）是"用户没动它"的信号 —— 浏览器里从来没有明文，回传的只能是掩码。
    把它当成新 key 存下去，会把真 key 覆盖成一串星号（这就是旧端点里那个
    `if "****" in key: pop` 想防的事，这里做成显式三分支）。
    """
    if clear:
        return ""
    key = str(raw_key or "").strip()
    if not key or "****" in key:
        return old_key or ""
    return key


def _validate_settings(settings: APISettingsV2) -> list:
    """保存前校验；返回问题列表（空 = 通过）。"""
    issues = []

    if not settings.providers:
        issues.append("至少需要一个供应商")

    for pid, p in settings.providers.items():
        if pid.startswith(ROUTE_PREFIX):
            issues.append(f"供应商 ID「{pid}」不得以保留前缀 {ROUTE_PREFIX} 开头")
        elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
            issues.append(f"供应商 ID「{pid}」非法：只能用字母/数字/下划线/短横线（1-64 位）")
        if p.protocol != "openai_chat":
            issues.append(f"供应商「{p.name}」协议 {p.protocol} 暂不支持（当前仅 openai_chat）")
        base = (p.base_url or "").strip()
        if not base:
            issues.append(f"供应商「{p.name}」缺少 API 地址")
        elif not base.startswith(("http://", "https://")):
            issues.append(f"供应商「{p.name}」地址必须以 http:// 或 https:// 开头")
        elif _is_self_proxy(base):
            issues.append(f"供应商「{p.name}」地址指向本地 token 代理自身（会形成转发自环）")
        elif "@" in base.split("://", 1)[-1].split("/", 1)[0]:
            issues.append(f"供应商「{p.name}」地址不能包含用户名或密码")
        for endpoint_name, endpoint in (("模型目录接口", p.models_endpoint),
                                        ("聊天接口", p.chat_completions_endpoint)):
            if endpoint and not (endpoint.startswith("/") or endpoint.startswith(("http://", "https://"))):
                issues.append(f"供应商「{p.name}」{endpoint_name}必须是绝对 http(s) URL 或相对路径")
            if endpoint and "@" in endpoint.split("://", 1)[-1].split("/", 1)[0]:
                issues.append(f"供应商「{p.name}」{endpoint_name}不能包含用户名或密码")
        if not (1 <= len(p.models) <= 1000):
            issues.append(f"供应商「{p.name}」模型列表必须包含 1-1000 个模型")
        ids = [m.id for m in p.models]
        if any(not isinstance(mid, str) or not mid.strip() for mid in ids):
            issues.append(f"供应商「{p.name}」包含空模型 ID")
        if len(ids) != len(set(ids)):
            issues.append(f"供应商「{p.name}」模型 ID 不能重复")
        for m in p.models:
            if m.context_window < 0 or m.max_tokens < 0:
                issues.append(f"供应商「{p.name}」模型「{m.id}」元数据不能为负数")
            if not m.reasoning_efforts:
                issues.append(f"供应商「{p.name}」模型「{m.id}」至少需要一个思考强度")
        if not p.default_model:
            issues.append(f"供应商「{p.name}」未指定默认模型")
        elif not any(m.id == p.default_model for m in p.models):
            issues.append(f"供应商「{p.name}」默认模型 {p.default_model} 不在其模型列表内")

    if settings.default_provider not in settings.providers:
        issues.append(f"默认供应商「{settings.default_provider}」不存在")

    routes = dict(settings.subagents)
    routes["backend"] = settings.backend
    valid_efforts = {"default", "off", "low", "medium", "high", "max"}
    for role, r in routes.items():
        if role not in SUPPORTED_ROLES and role != "backend":
            issues.append(f"未知角色「{role}」")
            continue
        prov = settings.providers.get(r.provider_id)
        if prov is None:
            issues.append(f"角色「{role}」指向的供应商「{r.provider_id}」不存在")
            continue
        if not prov.enabled:
            issues.append(f"角色「{role}」使用的供应商「{prov.name}」已禁用")
        model_meta = next((m for m in prov.models if m.id == r.model), None)
        if r.model and model_meta is None:
            issues.append(f"角色「{role}」选择的模型「{r.model}」不在供应商「{prov.name}」的模型列表内")
        if r.reasoning_effort not in valid_efforts:
            issues.append(f"角色「{role}」思考强度「{r.reasoning_effort}」无效")
        elif model_meta and model_meta.reasoning_efforts:
            declared = set(model_meta.reasoning_efforts)
            if r.reasoning_effort not in declared:
                issues.append(f"角色「{role}」的模型「{r.model}」不支持思考强度「{r.reasoning_effort}」")
    return issues


def _settings_ready(settings: APISettingsV2) -> bool:
    """Readiness reflects every enabled route, not only the default provider."""
    routes = list(settings.subagents.values()) + [settings.backend]
    for route in routes:
        provider = settings.providers.get(route.provider_id)
        if not provider or not provider.enabled or not provider.base_url:
            return False
        if not provider.api_key or provider.api_key in PLACEHOLDER_API_KEYS:
            return False
        if provider.models and route.model and not any(m.id == route.model for m in provider.models):
            return False
    return bool(settings.providers)


def _is_self_proxy(url: str) -> bool:
    try:
        from libraries.token_proxy import is_token_proxy_url
        return bool(is_token_proxy_url(url))
    except Exception:
        return False


def _legacy_flat_to_v2(data: dict, old: APISettingsV2):
    """旧扁平保存载荷 → (providers dict, 完整 data dict)。

    把 {api_key, base_url, model, ...} 解释为「修改默认供应商」，其余供应商原样保留。
    返回 None 表示当前无任何供应商、无法折算。
    """
    default_id = old.default_provider
    if not default_id or default_id not in old.providers:
        return None
    prov = old.providers[default_id]

    providers = {}
    for pid, p in old.providers.items():
        providers[pid] = {
            "id": p.id, "name": p.name, "enabled": p.enabled, "protocol": p.protocol,
            "base_url": p.base_url, "api_key": _mask_api_key(p.api_key),
            "url_strict": p.url_strict, "verify_ssl": p.verify_ssl,
            "http_timeout_seconds": p.http_timeout_seconds,
            "default_model": p.default_model, "reasoning_wire": p.reasoning_wire,
            "chat_completions_endpoint": p.chat_completions_endpoint,
            "models_endpoint": p.models_endpoint,
            "models": [{"id": m.id, "name": m.name, "context_window": m.context_window,
                        "max_tokens": m.max_tokens, "reasoning_efforts": m.reasoning_efforts,
                        "source": m.source}
                       for m in p.models],
        }

    target = providers[default_id]
    for field in ("base_url", "model", "api_key", "url_strict"):
        if field not in data:
            continue
        if field == "model":
            new_model = str(data["model"]).strip()
            if new_model:
                target["default_model"] = new_model
                if not any(m["id"] == new_model for m in target["models"]):
                    target["models"].insert(0, {"id": new_model, "name": new_model})
        else:
            target[field] = data[field]

    return providers, {
        "providers": providers,
        "default_provider": default_id,
        "subagents": {
            role: {"provider_id": r.provider_id, "model": r.model,
                   "max_tokens": r.max_tokens, "reasoning_effort": r.reasoning_effort}
            for role, r in old.subagents.items()
        },
        "backend": {"provider_id": old.backend.provider_id, "model": old.backend.model,
                    "max_tokens": old.backend.max_tokens,
                    "reasoning_effort": old.backend.reasoning_effort},
        "context_budget_tokens": data.get("context_budget_tokens"),
    }


@bp.route("/api/settings/save", methods=["POST"])
def settings_save_v2():
    """保存 v2 多供应商 + 子代理路由配置（原子写入 + 校验）。

    也接受旧扁平载荷（`{api_key, base_url, model}`）—— 老脚本/书签提交的是那种形状，
    直接 400 会让它们静默失败。扁平载荷按「改默认供应商」处理。
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象",
                        "issues": [{"path": "$", "code": "invalid_object"}]}), 400
    old = load_api_settings()

    # 旧扁平载荷 → 折算成对默认供应商的字段覆盖
    raw_providers = data.get("providers")
    if not isinstance(raw_providers, dict):
        if any(k in data for k in ("base_url", "model", "api_key", "url_strict")):
            legacy = _legacy_flat_to_v2(data, old)
            if legacy is None:
                return jsonify({"ok": False, "error": "api.json 未配置任何供应商"}), 400
            raw_providers = legacy[0]
            data = legacy[1]
        else:
            return jsonify({"ok": False, "error": "缺少 providers 字段"}), 400

    providers = {}
    parse_issues = []
    for pid, pdata in raw_providers.items():
        pid = str(pid or "").strip()
        if not pid or not isinstance(pdata, dict):
            parse_issues.append({"path": f"providers.{pid or '<empty>'}", "code": "invalid_provider", "message": "供应商项必须是对象"})
            continue
        old_prov = old.providers.get(pid)
        models = _parse_models(pdata.get("models"), parse_issues, f"providers.{pid}.models")
        default_model = str(pdata.get("default_model") or "").strip()
        if not default_model and models:
            default_model = models[0].id
        timeout, timeout_issue = _parse_int_field(
            pdata.get("http_timeout_seconds"), default=DEFAULT_HTTP_TIMEOUT_SECONDS,
            minimum=5, maximum=1800, field="超时")
        if timeout_issue:
            parse_issues.append({"path": f"providers.{pid}.http_timeout_seconds", "code": "invalid_integer", "message": timeout_issue})
        providers[pid] = APIProviderConfig(
            id=pid,
            name=str(pdata.get("name") or pid).strip() or pid,
            enabled=_parse_bool(pdata.get("enabled"), True),
            protocol=str(pdata.get("protocol") or "openai_chat").strip(),
            base_url=str(pdata.get("base_url") or "").strip().rstrip("/"),
            api_key=_merge_provider_secret(
                pdata.get("api_key"),
                old_prov.api_key if old_prov else "",
                _parse_bool(pdata.get("clear_api_key"), False),
            ),
            url_strict=_parse_bool(pdata.get("url_strict"), DEFAULT_URL_STRICT),
            verify_ssl=_parse_bool(pdata.get("verify_ssl"), DEFAULT_VERIFY_SSL),
            http_timeout_seconds=timeout,
            default_model=default_model or DEFAULT_MODEL,
            reasoning_wire=str(pdata.get("reasoning_wire") or "default").strip(),
            chat_completions_endpoint=str(pdata.get("chat_completions_endpoint") or "").strip(),
            models_endpoint=str(pdata.get("models_endpoint") or "").strip(),
            models=models,
        )

    default_provider = str(data.get("default_provider") or "").strip()
    if not default_provider and providers:
        default_provider = next(iter(providers))

    def _route(raw, fallback_prov: str, role: str) -> AgentRouteConfig:
        raw = raw if isinstance(raw, dict) else {}
        models = providers.get(fallback_prov).models if fallback_prov in providers else []
        max_tokens, route_issue = _parse_int_field(
            raw.get("max_tokens"), default=0, minimum=0, maximum=100000000,
            field="最大输出")
        if route_issue:
            parse_issues.append({"path": f"{role}.max_tokens", "code": "invalid_integer", "message": route_issue})
        return AgentRouteConfig(
            provider_id=str(raw.get("provider_id") or fallback_prov).strip(),
            model=str(raw.get("model") or (models[0].id if models else DEFAULT_MODEL)).strip(),
            max_tokens=max_tokens,
            reasoning_effort=str(raw.get("reasoning_effort") or "default").strip(),
        )

    raw_subagents = data.get("subagents")
    raw_subagents = raw_subagents if isinstance(raw_subagents, dict) else {}
    subagents = {}
    for role in SUPPORTED_ROLES:
        subagents[role] = _route(raw_subagents.get(role),
                                 default_provider or (next(iter(providers)) if providers else ""),
                                 f"subagents.{role}")
    backend = _route(data.get("backend"), default_provider or (next(iter(providers)) if providers else ""), "backend")

    if parse_issues:
        return jsonify({"ok": False, "error": "设置字段校验失败",
                        "issues": parse_issues[:100]}), 400

    settings = APISettingsV2(
        version=2,
        default_provider=default_provider,
        providers=providers,
        subagents=subagents,
        backend=backend,
    )

    issues = _validate_settings(settings)
    if issues:
        return jsonify({"ok": False, "error": "；".join(issues[:5])}), 400

    # context_budget 不在 v2 路由模型里，但保存时必须原样留住用户的值；
    # 由 save_api_settings 在同一次 atomic write 中写入，避免双写窗口。
    raw = load_api_config_raw()
    budget_value = data.get("context_budget_tokens")
    if budget_value is None:
        budget_value = raw.get("context_budget_tokens")
    budget, budget_issue = _parse_int_field(
        budget_value, default=DEFAULT_CONTEXT_BUDGET_TOKENS,
        minimum=CONTEXT_BUDGET_MIN, maximum=CONTEXT_BUDGET_MAX,
        field="上下文预算")
    if budget_issue:
        return jsonify({"ok": False, "error": budget_issue,
                        "issues": [{"path": "context_budget_tokens", "code": "invalid_integer",
                                     "message": budget_issue}]}), 400

    try:
        save_api_settings(settings, context_budget_tokens=budget)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入失败: {e}"}), 500

    # 配置变了：清缓存 + 清预检缓存（缓存 key 里含 model/upstream，但角色路由变了）
    invalidate_llm()
    try:
        from libraries.token_proxy import clear_probe_cache
        clear_probe_cache()
    except Exception:
        pass

    return jsonify({"ok": True, "message": "设置已保存"})


@bp.route("/api/settings/models/health", methods=["GET"])
def settings_models_health():
    """Return non-secret catalog readiness for each configured provider."""
    settings = load_api_settings()
    providers = {}
    for pid, provider in settings.providers.items():
        models = provider.models or []
        providers[pid] = {
            "status": "healthy" if len(models) > 1 else ("stale" if models else "empty"),
            "count": len(models),
            "default_model": provider.default_model,
            "protocol": provider.protocol,
            "base_url": provider.base_url.split("?", 1)[0],
            "configured": bool(provider.api_key and provider.api_key not in PLACEHOLDER_API_KEYS),
        }
    return jsonify({"ok": True, "default_provider": settings.default_provider, "providers": providers})


@bp.route("/api/settings/models/refresh", methods=["POST"])
def settings_refresh_models():
    """Discover models and optionally persist to api.json atomically."""
    data = request.get_json(silent=True) or {}
    pid = str(data.get("provider_id") or "").strip()
    persist = _parse_bool(data.get("persist"), True)

    settings = load_api_settings()
    if not pid:
        pid = settings.default_provider
    provider = settings.providers.get(pid)
    if not provider:
        return jsonify({"ok": False, "error": f"供应商「{pid}」不存在"}), 404
    if not provider.base_url:
        return jsonify({"ok": False, "error": "缺少 API 地址"}), 400

    key = provider.api_key
    if key and key in PLACEHOLDER_API_KEYS:
        key = ""

    try:
        result = discover_provider_models(provider, api_key=key, timeout=min(provider.http_timeout_seconds, 30))
    except DiscoveryError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": exc.code, "status": exc.status}), 400

    # Non-destructive merge
    merged = merge_discovered_models(provider.models, result.models)
    provider.models = merged
    if not provider.default_model and merged:
        provider.default_model = merged[0].id

    if persist:
        try:
            save_api_settings(settings)
            invalidate_llm()
        except Exception as e:
            return jsonify({"ok": False, "error": f"保存发现结果失败: {e}"}), 500

    sanitized = sanitize_api_settings_for_ui(settings)
    typed = api_config_from_mapping(load_api_config_raw())
    sanitized["context_budget_tokens"] = typed.context_budget_tokens
    sanitized["configured"] = _settings_ready(settings)

    return jsonify({
        "ok": True,
        "provider_id": pid,
        "count": len(result.models),
        "total_models": len(merged),
        "endpoint": result.endpoint,
        "config": sanitized,
        "warnings": result.warnings,
    })


@bp.route("/api/settings/models/discover", methods=["POST"])
@bp.route("/api/settings/discover-models", methods=["POST"])
def settings_discover_models():
    """Discover an OpenAI-compatible provider catalog without mutating api.json."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象",
                        "code": "invalid_object"}), 400
    old = load_api_settings()
    pid = str(data.get("provider_id") or "").strip()
    draft = data.get("provider") if isinstance(data.get("provider"), dict) else None
    if draft is None:
        saved = old.providers.get(pid)
        if saved is None:
            return jsonify({"ok": False, "error": "供应商不存在", "code": "provider_not_found"}), 404
        provider = saved
    else:
        old_prov = old.providers.get(pid)
        parse_issues = []
        models = _parse_models(draft.get("models"), parse_issues, f"providers.{pid}.models")
        timeout, timeout_issue = _parse_int_field(
            draft.get("http_timeout_seconds"), default=DEFAULT_HTTP_TIMEOUT_SECONDS,
            minimum=5, maximum=1800, field="超时")
        if timeout_issue:
            parse_issues.append({"path": "provider.http_timeout_seconds", "code": "invalid_integer", "message": timeout_issue})
        if parse_issues:
            return jsonify({"ok": False, "error": "供应商字段校验失败", "code": "invalid_provider",
                            "issues": parse_issues[:100]}), 400
        provider = APIProviderConfig(
            id=pid,
            name=str(draft.get("name") or pid).strip() or pid,
            enabled=_parse_bool(draft.get("enabled"), True),
            protocol=str(draft.get("protocol") or "openai_chat").strip(),
            base_url=str(draft.get("base_url") or (old_prov.base_url if old_prov else "")).strip().rstrip("/"),
            api_key=_merge_provider_secret(draft.get("api_key"), old_prov.api_key if old_prov else "", False),
            url_strict=_parse_bool(draft.get("url_strict"), old_prov.url_strict if old_prov else DEFAULT_URL_STRICT),
            verify_ssl=_parse_bool(draft.get("verify_ssl"), old_prov.verify_ssl if old_prov else DEFAULT_VERIFY_SSL),
            http_timeout_seconds=timeout,
            default_model=str(draft.get("default_model") or (old_prov.default_model if old_prov else DEFAULT_MODEL)).strip(),
            reasoning_wire=str(draft.get("reasoning_wire") or (old_prov.reasoning_wire if old_prov else "default")).strip(),
            chat_completions_endpoint=str(draft.get("chat_completions_endpoint") or (old_prov.chat_completions_endpoint if old_prov else "")).strip(),
            models_endpoint=str(draft.get("models_endpoint") or (old_prov.models_endpoint if old_prov else "")).strip(),
            models=models,
        )
    if not provider.base_url:
        return jsonify({"ok": False, "error": "缺少 API 地址", "code": "invalid_url"}), 400
    if _is_self_proxy(provider.base_url):
        return jsonify({"ok": False, "error": "该地址指向本地 token 代理自身", "code": "self_proxy"}), 400
    key = provider.api_key
    if key and key in PLACEHOLDER_API_KEYS:
        key = ""
    try:
        result = discover_provider_models(provider, api_key=key, timeout=min(provider.http_timeout_seconds, 30))
    except DiscoveryError as exc:
        body = {"ok": False, "error": str(exc), "code": exc.code, "provider_id": pid}
        if exc.status is not None:
            body["status"] = exc.status
        return jsonify(body), 400 if exc.status in (None, 401, 403, 404) else 502
    models = []
    for model in result.models:
        item = {
            "id": model.id, "name": model.name,
            "context_window": model.context_window,
            "max_tokens": model.max_tokens,
            "reasoning_efforts": model.reasoning_efforts,
            "source": "discovered",
        }
        if hasattr(model, "owned_by"):
            item["owned_by"] = model.owned_by
        if hasattr(model, "created"):
            item["created"] = model.created
        models.append(item)
    return jsonify({"ok": True, "provider_id": pid, "protocol": provider.protocol,
                    "endpoint": result.endpoint.split("?", 1)[0], "models": models,
                    "count": len(models), "page_count": result.page_count,
                    "truncated": result.truncated, "warnings": result.warnings})


@bp.route("/api/settings/test", methods=["POST"])
def settings_test_v2():
    """测试某个供应商的连接（支持未保存的草稿）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    old = load_api_settings()

    pid = str(data.get("provider_id") or "").strip()
    draft = data.get("provider") if isinstance(data.get("provider"), dict) else None
    source = draft or {}
    old_prov = old.providers.get(pid)

    base_url = str(source.get("base_url") or (old_prov.base_url if old_prov else "")).strip()
    if not base_url:
        return jsonify({"ok": False, "error": "缺少 API 地址"}), 400
    if _is_self_proxy(base_url):
        return jsonify({"ok": False, "error": "该地址指向本地 token 代理自身"}), 400

    api_key = _merge_provider_secret(
        source.get("api_key"), old_prov.api_key if old_prov else "", False)
    if not api_key or api_key in PLACEHOLDER_API_KEYS:
        return jsonify({"ok": False, "error": "未配置有效 API Key"}), 400

    model = str(data.get("model") or source.get("model") or "").strip()
    if not model:
        models = _parse_models(source.get("models")) if draft else []
        model = ((models[0].id if models else "")
                 or (old_prov.default_model if old_prov else "")
                 or DEFAULT_MODEL)

    cfg = APIConfig(
        api_key=api_key,
        base_url=base_url,
        url_strict=_parse_bool(source.get("url_strict"), old_prov.url_strict if old_prov else DEFAULT_URL_STRICT),
        model=model,
        max_tokens=0,
        http_timeout_seconds=10,   # 连接测试不该按生成用的 300s 干等
        verify_ssl=_parse_bool(source.get("verify_ssl"), old_prov.verify_ssl if old_prov else DEFAULT_VERIFY_SSL),
        chat_completions_endpoint=str(source.get("chat_completions_endpoint") or (old_prov.chat_completions_endpoint if old_prov else "")),
        reasoning_wire=str(source.get("reasoning_wire") or "default"),
    )

    started = time.time()
    try:
        from core.llm_client import LLMClient
        result = LLMClient(cfg).test_connection()
    except Exception as e:
        return jsonify({"ok": False, "error": _safe_error(e, api_key)}), 400
    elapsed_ms = int((time.time() - started) * 1000)
    if result.get("success"):
        return jsonify({"ok": True, "model": model, "elapsed_ms": elapsed_ms,
                        "response": (result.get("sample") or "")[:60]})
    return jsonify({"ok": False, "error": result.get("error", "连接失败"),
                    "elapsed_ms": elapsed_ms}), 400



@bp.route("/api/debug/search-test")
def debug_search_test():
    """调试：测试搜索"""
    from plugins.fanqie_scout import FanqieCrawler
    title = request.args.get("q", "这个游戏不对劲，我挖矿成神！")
    try:
        c = FanqieCrawler()
        # 检查文件修改时间
        import os
        mtime = os.path.getmtime(os.path.join(os.path.dirname(__file__), "..", "plugins", "fanqie_scout.py"))
        from datetime import datetime
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        novel = c.search_novel(title)
        if novel:
            return jsonify({
                "ok": True,
                "title": novel.title,
                "author": novel.author,
                "book_id": novel.book_id,
                "chapters": novel.chapter_count,
                "file_time": mtime_str,
            })
        return jsonify({"ok": False, "message": "not found", "file_time": mtime_str})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()})



