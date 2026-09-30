"""数据模型 — v2 引擎共用的最小模型集合。"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ProviderModel:
    """供应商模型元数据。

    ``source`` is deliberately informational: discovered models and hand-entered
    models share the same route/validation path, while the UI can explain what a
    refresh will preserve.  It is optional so old api.json files remain valid.
    """
    id: str = ""
    name: str = ""
    context_window: int = 128000
    max_tokens: int = 8192
    reasoning_efforts: List[str] = field(default_factory=lambda: ["default"])
    source: str = "manual"  # manual | discovered


@dataclass
class APIProviderConfig:
    """单个 API 供应商配置"""
    id: str = ""
    name: str = ""
    enabled: bool = True
    protocol: str = "openai_chat"  # 目前支持 openai_chat
    base_url: str = ""
    api_key: str = ""
    url_strict: bool = False
    verify_ssl: bool = True
    http_timeout_seconds: int = 300
    default_model: str = ""
    reasoning_wire: str = "default"  # default | none | openai | deepseek
    # Optional explicit endpoints. Empty values are derived from base_url.
    chat_completions_endpoint: str = ""
    models_endpoint: str = ""
    models: List[ProviderModel] = field(default_factory=list)


@dataclass
class AgentRouteConfig:
    """单个子代理/角色的路由绑定"""
    provider_id: str = ""
    model: str = ""
    max_tokens: int = 0  # 0 为跟随模型/系统默认
    reasoning_effort: str = "default"  # default | off | low | medium | high | max


@dataclass
class APISettingsV2:
    """v2 完整多供应商与子代理路由配置"""
    version: int = 2
    default_provider: str = ""
    providers: Dict[str, APIProviderConfig] = field(default_factory=dict)
    subagents: Dict[str, AgentRouteConfig] = field(default_factory=dict)
    backend: AgentRouteConfig = field(default_factory=AgentRouteConfig)


@dataclass
class APIConfig:
    """LLM API 配置（向后兼容单配置视图）"""
    api_key: str = ""
    base_url: str = ""
    url_strict: bool = False
    model: str = ""
    max_tokens: int = 0
    http_timeout_seconds: int = 300
    context_budget_tokens: int = 300000
    verify_ssl: bool = True  # 是否校验 TLS 证书（默认开启，关闭仅用于兼容旧证书环境）
    # 旧版配置遗留：当 base_url 指向本地 token 代理自身时，代理用它作为上游。
    # 新配置的 base_url 直接就是真实上游，本字段留空即可。
    real_base_url: str = ""
    # Optional explicit chat endpoint; empty means derive from base_url.
    chat_completions_endpoint: str = ""
    # 扩展元数据（可选，供已升级调用方读取路由上下文）
    provider_id: str = ""
    reasoning_effort: str = "default"
    reasoning_wire: str = "default"
