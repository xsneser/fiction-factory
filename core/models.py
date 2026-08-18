"""数据模型 — v2 引擎共用的最小模型集合。"""

from dataclasses import dataclass


@dataclass
class APIConfig:
    """LLM API 配置"""
    api_key: str = ""
    base_url: str = ""
    url_strict: bool = False
    model: str = ""
    max_tokens: int = 0
    http_timeout_seconds: int = 300
    context_budget_tokens: int = 300000
    verify_ssl: bool = True  # 是否校验 TLS 证书（默认开启，关闭仅用于兼容旧证书环境）
