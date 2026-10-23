"""模型路由（router）：配置驱动的模型注册表与路由策略

数据来源：gateway.yaml（providers / models / pricing），见 core/gateway_config.py
- Route：一条上游路由（provider 配置 + 协议类型 + 上游模型 + 可选覆盖）
- ModelEntry：逻辑模型（display_name / 策略 / 路由列表）
- 策略：
  - priority：按配置顺序故障转移
  - weighted_round_robin：平滑加权轮询选出首选，其余路由作为故障转移备用
- 入口能力（api）：chat | responses | both，声明该路由可服务的入口协议；
  缺省按 type 推导（chat/anthropic -> chat，responses -> responses）。
  仅 /v1/responses 入口按此校验，避免把仅支持 chat 的供应商伪装成 Responses API。
- get_routes(model_id)：返回按策略排序的候选路由（网关按序尝试，失败自动转移）
"""

from dataclasses import dataclass, field

from app.core.errors import ErrorCode, LLMGatewayError
from app.core.gateway_config import GATEWAY_CONFIG, get_provider_config
from app.services.upstream import AnthropicAdapter, BaseAdapter, ChatAdapter, ResponsesAdapter

# 协议类型 -> 默认端点路径（相对 provider base_url）
_ENDPOINT_BY_TYPE = {
    "chat": "/v1/chat/completions",
    "responses": "/responses",
    "anthropic": "/anthropic/v1/messages",
}

# 协议别名：reasoning 走 chat 适配器
_TYPE_ALIASES = {"reasoning": "chat"}

# 协议类型 -> 缺省入口能力（chat 与 anthropic 协议出口都归一为 Chat Completions）
_API_BY_TYPE = {"chat": "chat", "responses": "responses", "anthropic": "chat"}

# 允许的入口能力声明值
_VALID_APIS = {"chat", "responses", "both"}


@dataclass
class Route:
    """一条上游路由"""

    provider: str
    type: str                    # chat / responses / anthropic
    model: str                   # 上游模型名
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 120.0
    endpoint: str | None = None  # 覆盖默认端点路径
    weight: int = 1              # weighted_round_robin 权重
    api: str | None = None       # 入口能力声明（chat | responses | both），缺省按 type 推导

    @property
    def endpoint_path(self) -> str:
        return self.endpoint or _ENDPOINT_BY_TYPE.get(self.type, "/v1/chat/completions")

    @property
    def gateway_api(self) -> str:
        """该路由可服务的入口协议（显式声明优先，否则按协议类型推导）"""
        return self.api or _API_BY_TYPE.get(self.type, "chat")

    def supports_api(self, api_name: str) -> bool:
        """是否可用于指定入口协议（both 表示两者皆可）"""
        return self.gateway_api in ("both", api_name)

    @property
    def key(self) -> str:
        """熔断/统计维度标识"""
        return f"{self.provider}:{self.model}"

    def as_adapter_config(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout_seconds": self.timeout_seconds,
            "endpoint": self.endpoint,
        }


@dataclass
class ModelEntry:
    """逻辑模型条目"""

    id: str
    display_name: str
    strategy: str = "priority"   # priority | weighted_round_robin
    enabled: bool = True
    routes: list[Route] = field(default_factory=list)

    @property
    def api_type(self) -> str:
        return self.routes[0].type if self.routes else "chat"

    @property
    def provider(self) -> str:
        return self.routes[0].provider if self.routes else ""

    def supports_api(self, api_name: str) -> bool:
        """模型是否有任一路由声明可用于指定入口协议"""
        return any(r.supports_api(api_name) for r in self.routes)


def _build_route(model_id: str, raw: dict) -> Route:
    provider = raw.get("provider") or ""
    provider_cfg = get_provider_config(provider) or {}
    api = raw.get("api")
    if api is not None and api not in _VALID_APIS:
        raise LLMGatewayError(
            ErrorCode.INVALID_REQUEST,
            f"模型 {model_id} 的路由 api 声明非法: {api}（可选 chat | responses | both）",
        )
    return Route(
        provider=provider,
        type=_TYPE_ALIASES.get(raw.get("type", "chat"), raw.get("type", "chat")),
        model=raw.get("model") or model_id,
        base_url=raw.get("base_url") or provider_cfg.get("base_url") or "",
        api_key=raw.get("api_key") or provider_cfg.get("api_key") or "",
        timeout_seconds=float(raw.get("timeout_seconds") or provider_cfg.get("timeout_seconds") or 120.0),
        endpoint=raw.get("endpoint"),
        weight=int(raw.get("weight", 1)),
        api=api,
    )


def _build_registry() -> dict[str, ModelEntry]:
    entries: dict[str, ModelEntry] = {}
    for model_id, raw in (GATEWAY_CONFIG.get("models") or {}).items():
        raw = raw or {}
        routes = [_build_route(model_id, r) for r in (raw.get("routes") or [])]
        entries[model_id] = ModelEntry(
            id=model_id,
            display_name=raw.get("display_name") or model_id,
            strategy=raw.get("strategy", "priority"),
            enabled=bool(raw.get("enabled", True)),
            routes=routes,
        )
    return entries


# 模型注册表（SUPPORTED_MODELS 保持原名，调用方 `model in SUPPORTED_MODELS` 兼容）
SUPPORTED_MODELS: dict[str, ModelEntry] = _build_registry()


def get_model_config(model_id: str) -> ModelEntry | None:
    """获取模型条目"""
    return SUPPORTED_MODELS.get(model_id)


# 平滑加权轮询状态：{model_id: [current_weight,...]}（与 routes 对齐）
_wrr_state: dict[str, list[int]] = {}


def _wrr_order(entry: ModelEntry) -> list[Route]:
    """平滑加权轮询：选出的首选路由放最前，其余作为故障转移备用"""
    routes = entry.routes
    weights = [max(1, r.weight) for r in routes]
    current = _wrr_state.setdefault(entry.id, [0] * len(routes))
    total = sum(weights)

    picked = 0
    for i, w in enumerate(weights):
        current[i] += w
        if current[i] > current[picked]:
            picked = i
    current[picked] -= total

    return [routes[picked]] + [r for i, r in enumerate(routes) if i != picked]


def get_routes(model_id: str) -> list[Route]:
    """按策略返回候选路由（有序），供网关依次尝试（故障转移）"""
    entry = get_model_config(model_id)
    if not entry or not entry.enabled:
        raise LLMGatewayError(ErrorCode.MODEL_NOT_FOUND, f"不支持的模型: {model_id}")
    if not entry.routes:
        raise LLMGatewayError(ErrorCode.MODEL_NOT_FOUND, f"模型 {model_id} 未配置路由")
    if entry.strategy == "weighted_round_robin" and len(entry.routes) > 1:
        return _wrr_order(entry)
    return list(entry.routes)


_ADAPTER_CLASSES: dict[str, type[BaseAdapter]] = {
    "chat": ChatAdapter,
    "responses": ResponsesAdapter,
    "anthropic": AnthropicAdapter,
}


def get_adapter_for_route(model_id: str, route: Route) -> BaseAdapter:
    """为指定路由构建适配器实例"""
    adapter_cls = _ADAPTER_CLASSES.get(route.type, ChatAdapter)
    return adapter_cls(model_id, route.as_adapter_config())


def get_adapter(model_id: str) -> BaseAdapter:
    """兼容入口：取模型首选路由的适配器"""
    return get_adapter_for_route(model_id, get_routes(model_id)[0])
