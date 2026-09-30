"""Provider model-catalog discovery for OpenAI-compatible APIs.

Discovery is intentionally separate from chat-completion testing.  A successful
chat request does not imply that a provider exposes a usable ``/models``
catalog, and a failed catalog request must never erase a user's manual models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode

import requests

from core.api_config import ROUTE_PREFIX
from core.models import APIProviderConfig, ProviderModel

MAX_PAGES = 10
MAX_MODELS = 1000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ID_LENGTH = 256
MAX_NAME_LENGTH = 256
SUPPORTED_DISCOVERY_PROTOCOLS = frozenset({"openai_chat"})


class DiscoveryError(RuntimeError):
    def __init__(self, message: str, code: str = "discovery_failed", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class DiscoveryResult:
    models: list[ProviderModel]
    endpoint: str
    page_count: int
    truncated: bool = False
    warnings: list[str] | None = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _safe_url(value: str) -> str:
    """Validate and normalize an endpoint without allowing embedded credentials."""
    raw = (value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DiscoveryError("API 地址必须是 http 或 https URL", "invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise DiscoveryError("API 地址不能包含嵌入式用户名或密码", "url_credentials")
    if parsed.fragment:
        raise DiscoveryError("API 地址不能包含 fragment", "invalid_url")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryError("API 地址端口无效", "invalid_url") from exc
    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), parsed.query, ""))


def _api_prefix(base_url: str, strict: bool = False) -> str:
    base = _safe_url(base_url)
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif path.endswith("/models"):
        path = path[: -len("/models")]
    # An explicit endpoint can already contain /v1; preserve it. Otherwise
    # OpenAI-compatible providers conventionally live below /v1.
    if not path and not strict:
        path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def resolve_provider_endpoint(provider: APIProviderConfig, operation: str = "models") -> str:
    if provider.protocol not in SUPPORTED_DISCOVERY_PROTOCOLS:
        raise DiscoveryError(
            f"协议 {provider.protocol or '未指定'} 暂不支持模型目录发现",
            "unsupported_protocol",
        )
    explicit = provider.models_endpoint if operation == "models" else provider.chat_completions_endpoint
    if explicit:
        raw = explicit.strip()
        if raw.startswith("/"):
            base = _api_prefix(provider.base_url, provider.url_strict)
            return _safe_url(urljoin(base.rstrip("/") + "/", raw.lstrip("/")))
        return _safe_url(raw)
    prefix = _api_prefix(provider.base_url, provider.url_strict)
    suffix = "/models" if operation == "models" else "/chat/completions"
    return prefix + suffix


def _bounded_text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _iter_items(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    if isinstance(payload, list):
        return payload, {}
    if not isinstance(payload, dict):
        raise DiscoveryError("模型目录响应不是 JSON 对象或数组", "unsupported_response_shape")
    items = payload.get("data")
    if items is None:
        items = payload.get("models")
    if not isinstance(items, list):
        raise DiscoveryError("模型目录响应缺少 data/models 数组", "unsupported_response_shape")
    return items, payload


def _normalize_model(raw: Any) -> ProviderModel | None:
    if isinstance(raw, str):
        model_id = raw.strip()
        if not model_id:
            return None
        return ProviderModel(id=model_id[:MAX_ID_LENGTH], name=model_id[:MAX_NAME_LENGTH], source="discovered")
    if not isinstance(raw, dict):
        return None
    model_id = _bounded_text(raw.get("id") or raw.get("name"), MAX_ID_LENGTH)
    if not model_id or model_id.startswith(ROUTE_PREFIX):
        return None
    name = _bounded_text(raw.get("name"), MAX_NAME_LENGTH) or model_id
    owned_by = _bounded_text(raw.get("owned_by"), MAX_NAME_LENGTH)
    created = raw.get("created")
    # Keep only conservative fields; do not infer context or reasoning support.
    model = ProviderModel(
        id=model_id,
        name=name,
        context_window=0,
        max_tokens=0,
        reasoning_efforts=["default"],
        source="discovered",
    )
    # Dynamic attributes are not serialized; these are useful to the endpoint
    # caller only when present in the normalized response.
    if owned_by:
        setattr(model, "owned_by", owned_by)
    if isinstance(created, int) and not isinstance(created, bool) and created >= 0:
        setattr(model, "created", created)
    return model


def _next_page(payload: dict[str, Any], endpoint: str) -> str | None:
    # Only follow same-origin absolute/relative continuation URLs.
    candidate = payload.get("next") or payload.get("next_url")
    if candidate:
        candidate = urljoin(endpoint, str(candidate))
    else:
        has_more = payload.get("has_more")
        cursor = payload.get("last_id") or payload.get("next_cursor")
        if has_more and cursor:
            parsed = urlsplit(endpoint)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query["after"] = [str(cursor)]
            candidate = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))
    if not candidate:
        return None
    safe = _safe_url(candidate)
    start, target = urlsplit(endpoint), urlsplit(safe)
    if (start.scheme, start.netloc) != (target.scheme, target.netloc):
        raise DiscoveryError("模型目录分页跳转到了不同域名", "unsafe_redirect")
    return safe


def _fetch_url(url: str, *, headers: dict, timeout: tuple[float, float],
               verify: bool, session: Any = None) -> requests.Response:
    """Fetch URL with Windows proxy resilience (direct first, environment proxy fallback)."""
    if session is not None and session is not requests:
        return session.get(url, headers=headers, timeout=timeout, verify=verify, allow_redirects=False)

    # First attempt: direct connection bypassing Windows system proxy
    direct_session = requests.Session()
    direct_session.trust_env = False
    try:
        return direct_session.get(url, headers=headers, timeout=timeout, verify=verify, allow_redirects=False)
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
        # Fallback: try environment-configured proxy
        env_session = requests.Session()
        env_session.trust_env = True
        return env_session.get(url, headers=headers, timeout=timeout, verify=verify, allow_redirects=False)


def discover_provider_models(provider: APIProviderConfig, *, api_key: str | None = None,
                              timeout: float = 30.0, session: Any = None) -> DiscoveryResult:
    endpoint = resolve_provider_endpoint(provider, "models")
    key = api_key if api_key is not None else provider.api_key
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    models: list[ProviderModel] = []
    seen: set[str] = set()
    warnings: list[str] = []
    current = endpoint
    page_count = 0
    truncated = False
    cursors: set[str] = set()
    req_timeout = (min(5.0, timeout), timeout)
    while current and page_count < MAX_PAGES:
        page_count += 1
        try:
            response = _fetch_url(
                current, headers=headers, timeout=req_timeout,
                verify=provider.verify_ssl, session=session,
            )
        except requests.RequestException as exc:
            raise DiscoveryError("请求模型目录失败，请检查地址、网络或 TLS 设置", "upstream_network") from exc
        if response.status_code < 200 or response.status_code >= 300:
            if response.status_code in (401, 403):
                code = "upstream_auth_failed"
                msg = "模型目录请求被上游拒绝，请检查 API Key"
            elif response.status_code == 404:
                code = "models_endpoint_not_found"
                msg = "上游未提供 OpenAI-compatible /models 接口"
            else:
                code = "upstream_http_error"
                msg = f"模型目录请求失败（HTTP {response.status_code}）"
            raise DiscoveryError(msg, code, response.status_code)
        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
            raise DiscoveryError("模型目录响应过大", "response_too_large")
        raw_body = response.content
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise DiscoveryError("模型目录响应过大", "response_too_large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DiscoveryError("上游模型目录不是有效 JSON", "invalid_json") from exc
        items, envelope = _iter_items(payload)
        for raw in items:
            model = _normalize_model(raw)
            if model is None or model.id in seen:
                continue
            seen.add(model.id)
            models.append(model)
            if len(models) >= MAX_MODELS:
                truncated = True
                warnings.append(f"模型数量超过 {MAX_MODELS}，已截断")
                break
        if truncated:
            break
        nxt = _next_page(envelope, current) if envelope else None
        if nxt in cursors or nxt == current:
            warnings.append("上游返回了重复分页游标，已停止")
            break
        if nxt:
            cursors.add(nxt)
        current = nxt
    if current and page_count >= MAX_PAGES:
        truncated = True
        warnings.append(f"分页超过 {MAX_PAGES} 页，已截断")
    if not models:
        warnings.append("上游返回的模型目录为空")
    return DiscoveryResult(models=models, endpoint=endpoint, page_count=page_count,
                           truncated=truncated, warnings=warnings)


def merge_discovered_models(existing: Iterable[ProviderModel], discovered: Iterable[ProviderModel]) -> list[ProviderModel]:
    """Non-destructively merge discovery results, preserving manual metadata."""
    discovered_by_id = {m.id: m for m in discovered if m.id}
    result: list[ProviderModel] = []
    seen: set[str] = set()
    for old in existing:
        if not old.id or old.id in seen:
            continue
        seen.add(old.id)
        fresh = discovered_by_id.get(old.id)
        if fresh is None or old.source == "manual":
            result.append(old)
            continue
        # Keep user-facing metadata if it was edited, while refreshing the
        # conservative discovered fields represented by the new record.
        result.append(fresh)
    for model in discovered:
        if model.id and model.id not in seen:
            seen.add(model.id)
            result.append(model)
    return result
