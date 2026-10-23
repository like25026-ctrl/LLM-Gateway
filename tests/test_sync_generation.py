"""网关功能点验证脚本（重点覆盖同步生成 /api/v1/chat）

覆盖范围（全部功能点）：
  A. 配置层       gateway.yaml 插值 / tuning 参数 / pricing 计价（含缓存命中）
  B. 路由层       SUPPORTED_MODELS 注册表 / priority / weighted_round_robin / 未知与禁用模型
  C. 韧性        熔断状态机 / 限流滑动窗口 / 重试退避封顶 / 上游状态码映射
  D. 结构化输出  schema_from_request / content_from_response / validate_response / repair_instruction
  E. 适配器       三种协议的 payload / headers / 响应归一化 / 流式事件解析
  F. 可观测       UsageEvent 落库 / 查询 / 合并用量 / 三协议用量字段提取
  G. 提示词       模板版本 CRUD / 变量渲染 / 版本引用
  H. HTTP 端点    同步生成（4 模型）/ 结构化 / 故障转移 / 错误码 / 模板引用 / 流式 / 健康检查 / 用量查询

用法：
    python tests/test_sync_generation.py              # 全量（离线单测 + 真实上游 E2E）
    python tests/test_sync_generation.py --offline    # 仅离线单测，不联网
    GATEWAY_LIVE=0 python tests/test_sync_generation.py
    pytest tests/test_sync_generation.py              # 若已安装 pytest

说明：
  - E2E 部分用 httpx.ASGITransport 在进程内直接调用 ASGI 应用，无需手动起服务/占用端口；
    对上游（api.deepseek.com）仍走真实网络，用于验证同步生成的真实链路。
  - 未配置 DEEPSEEK_API_KEY 或设置 GATEWAY_LIVE=0 时，E2E 用例自动跳过。
"""

import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# 保证脚本可独立运行（无需设置 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.auth import require_api_key  # noqa: E402
from app.core.circuit_breaker import CircuitBreaker  # noqa: E402
from app.core.errors import ErrorCode, LLMGatewayError, StructuredOutputError  # noqa: E402
from app.core.gateway_config import (  # noqa: E402
    GATEWAY_CONFIG,
    _interpolate,
    get_api_keys,
    get_pricing,
    get_provider_config,
    tuning,
)
from app.core.rate_limit import check_rate_limit, reset_rate_limit  # noqa: E402
from app.core.security import check_sensitive_words, filter_sensitive_words  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import gateway, prompts  # noqa: E402
from app.services.gateway import _backoff, _upstream_status_error  # noqa: E402
from app.services.router import (  # noqa: E402
    SUPPORTED_MODELS,
    ModelEntry,
    Route,
    _build_route,
    _wrr_order,
    get_adapter,
    get_routes,
)
from app.services.structured_output import (  # noqa: E402
    content_from_response,
    json_schema_instruction,
    repair_instruction,
    schema_from_request,
    strip_fences,
    validate_response,
)
from app.services.upstream import (  # noqa: E402
    AnthropicAdapter,
    ChatAdapter,
    ResponsesAdapter,
    UnifiedRequest,
)
from app.services.usage import (  # noqa: E402
    UsageEvent,
    calculate_cost,
    merge_usage,
    usage_from_payload,
    usage_repository,
)
from app.services.upstream import Usage  # noqa: E402

# ==================== 基础设施 ====================

LIVE_ENABLED = os.getenv("GATEWAY_LIVE", "1") != "0"
_ADAPTER_CFG = {"model": "m", "base_url": "http://upstream.local", "api_key": "test-key",
                "timeout_seconds": 30, "endpoint": None}
_JSON_SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}


class SkipTest(Exception):
    """用例跳过（不视为失败）"""


def _ensure_tables() -> None:
    Base.metadata.create_all(bind=engine)


def _live_ready() -> bool:
    return LIVE_ENABLED and bool(settings.DEEPSEEK_API_KEY)


@contextmanager
def _patch(obj, name, value):
    """临时替换对象属性，退出时还原"""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


# ==================== A. 配置层 ====================

def test_gateway_config_interpolation():
    """${VAR} / ${VAR:-default} 插值：环境变量优先、缺省回退、递归生效"""
    os.environ["__PROBE_VAR"] = "probe-value"
    try:
        assert _interpolate("${__PROBE_VAR}") == "probe-value"
        assert _interpolate("${__MISSING_VAR:-fallback}") == "fallback"
        assert _interpolate("${__MISSING_VAR}") == ""
        assert _interpolate({"a": "${__MISSING_VAR:-x}", "b": [1, "${__PROBE_VAR}"]}) == {
            "a": "x", "b": [1, "probe-value"]
        }
    finally:
        os.environ.pop("__PROBE_VAR", None)

    provider = get_provider_config("deepseek")
    assert provider and provider["base_url"].startswith("http")
    # 未配置时回退全局 settings 的 Key
    assert provider["api_key"] == settings.DEEPSEEK_API_KEY


def test_gateway_tuning_and_pricing():
    """tuning 参数合法 + pricing 查询可用"""
    assert tuning.retry.max_attempts >= 1
    assert tuning.retry.max_delay_seconds > 0
    assert 429 in tuning.retry.retry_statuses and 503 in tuning.retry.retry_statuses
    assert tuning.circuit_breaker.failure_threshold >= 1
    assert tuning.circuit_breaker.cooldown_seconds >= 0
    assert tuning.structured_output_retries >= 0
    assert isinstance(tuning.rate_limit.enabled, bool)
    assert get_pricing("deepseek-chat"), "gateway.yaml pricing 应包含 deepseek-chat"
    assert GATEWAY_CONFIG.get("models"), "gateway.yaml models 不应为空"


def test_gateway_api_keys_and_tuning():
    """调用方鉴权配置：api_keys 解析（空值过滤 -> 空列表即关闭鉴权）"""
    keys = get_api_keys()
    assert isinstance(keys, list)
    assert all(k.strip() for k in keys), "空串应被过滤"
    # gateway.yaml 提供 api_keys 段（未设置 GATEWAY_API_KEY 时为空 -> 开发模式不校验）
    assert "api_keys" in GATEWAY_CONFIG

    # 逗号分隔字符串也支持（GATEWAY_CONFIG 是 dict，需按键临时替换）
    original = GATEWAY_CONFIG["api_keys"]
    try:
        GATEWAY_CONFIG["api_keys"] = " a , b ,, "
        assert get_api_keys() == ["a", "b"]
        GATEWAY_CONFIG["api_keys"] = []
        assert get_api_keys() == []
    finally:
        GATEWAY_CONFIG["api_keys"] = original


def test_pricing_calculation_with_cache():
    """费用计算：缓存命中部分按缓存价，未配置缓存价时回退 input 价"""
    # deepseek-chat: input 1.0 / output 2.0 / cached 0.2（元/1M）
    cost = calculate_cost("deepseek-chat", 1000, 1000, cached_tokens=500)
    assert abs(cost - 0.0026) < 1e-9, cost  # 500*1.0 + 500*0.2 + 1000*2.0 = 2600e-6
    # deepseek-reasoner 未配置 cached 价 -> 按 input 价
    assert abs(calculate_cost("deepseek-reasoner", 1000, 0) - 0.001) < 1e-9
    # cached 超过 prompt 时被裁剪
    assert abs(calculate_cost("deepseek-chat", 1000, 0, cached_tokens=99999) - 0.0002) < 1e-9
    # 未定价模型 -> 0
    assert calculate_cost("__no_pricing__", 1000, 1000) == 0.0


def test_usage_from_payload_three_protocols():
    """三协议用量字段命名兼容（OpenAI / Responses / Anthropic）"""
    assert usage_from_payload({"usage": {"prompt_tokens": 3, "completion_tokens": 2}}) == (3, 2, 0)
    assert usage_from_payload({"usage": {"input_tokens": 4, "output_tokens": 1}}) == (4, 1, 0)
    assert usage_from_payload(
        {"usage": {"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": 5}}}
    ) == (9, 0, 5)


# ==================== B. 路由层 ====================

def test_router_registry():
    """模型注册表：四模型 + 别名，字段与端点映射正确"""
    for mid in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-flash"):
        entry = SUPPORTED_MODELS[mid]
        assert isinstance(entry, ModelEntry) and entry.enabled and entry.routes
        assert entry.provider == "deepseek"

    assert SUPPORTED_MODELS["deepseek-chat"].api_type == "chat"
    assert SUPPORTED_MODELS["deepseek-v4-pro"].api_type == "responses"
    assert SUPPORTED_MODELS["deepseek-flash"].api_type == "anthropic"

    assert SUPPORTED_MODELS["deepseek-chat"].routes[0].key == "deepseek:deepseek-chat"
    assert SUPPORTED_MODELS["deepseek-chat"].routes[0].endpoint_path == "/v1/chat/completions"
    assert SUPPORTED_MODELS["deepseek-v4-pro"].routes[0].endpoint_path == "/responses"
    assert SUPPORTED_MODELS["deepseek-flash"].routes[0].endpoint_path == "/anthropic/v1/messages"
    cfg = SUPPORTED_MODELS["deepseek-chat"].routes[0].as_adapter_config()
    assert set(cfg) == {"model", "base_url", "api_key", "timeout_seconds", "endpoint"}


def test_router_priority_and_alias():
    """priority 策略按配置顺序返回候选路由"""
    routes = get_routes("smart")
    assert len(routes) == 2, "smart 应为双路由故障转移别名"
    assert routes[0].base_url == "http://127.0.0.1:9", "首选路由应为不可达演示地址"
    assert get_routes("deepseek-flash")[0].type == "anthropic"


def test_router_weighted_round_robin_order():
    """平滑加权轮询：权重 3:1 时 8 次首选应为 6:2，且非首选作为备用保留"""
    entry = ModelEntry(
        id="__wrr__", display_name="t", strategy="weighted_round_robin",
        routes=[Route(provider="a", type="chat", model="m1", weight=1),
                Route(provider="b", type="chat", model="m2", weight=3)],
    )
    seq = [_wrr_order(entry)[0].provider for _ in range(8)]
    assert seq.count("b") == 6 and seq.count("a") == 2, seq
    assert len(_wrr_order(entry)) == 2


def test_router_unknown_and_disabled_model():
    """未知模型 / 禁用模型 -> MODEL_NOT_FOUND"""
    try:
        get_routes("__no_such_model__")
        raise AssertionError("未知模型应抛错")
    except LLMGatewayError as err:
        assert err.code == ErrorCode.MODEL_NOT_FOUND

    SUPPORTED_MODELS["__disabled__"] = ModelEntry(
        id="__disabled__", display_name="d", enabled=False,
        routes=[Route(provider="deepseek", type="chat", model="deepseek-chat")],
    )
    try:
        get_routes("__disabled__")
        raise AssertionError("禁用模型应抛错")
    except LLMGatewayError as err:
        assert err.code == ErrorCode.MODEL_NOT_FOUND
    finally:
        SUPPORTED_MODELS.pop("__disabled__", None)


def test_router_adapter_selection():
    """按路由协议类型构建对应适配器"""
    assert isinstance(get_adapter("deepseek-chat"), ChatAdapter)
    assert isinstance(get_adapter("deepseek-v4-pro"), ResponsesAdapter)
    assert isinstance(get_adapter("deepseek-flash"), AnthropicAdapter)
    assert get_adapter("deepseek-v4-pro").endpoint_path == "/responses"


def test_router_api_capability_declaration():
    """入口能力声明：chat / responses 过滤（/v1/responses 只路由到声明 responses 的路由）"""
    # chat 协议路由仅服务 chat 入口
    assert SUPPORTED_MODELS["deepseek-chat"].routes[0].gateway_api == "chat"
    assert SUPPORTED_MODELS["deepseek-chat"].supports_api("chat")
    assert not SUPPORTED_MODELS["deepseek-chat"].supports_api("responses")

    # responses 协议路由服务 responses 入口
    assert SUPPORTED_MODELS["deepseek-v4-pro"].supports_api("responses")
    assert not SUPPORTED_MODELS["deepseek-v4-pro"].supports_api("chat")

    # anthropic 协议出口归一为 chat 入口
    assert SUPPORTED_MODELS["deepseek-flash"].routes[0].gateway_api == "chat"
    assert SUPPORTED_MODELS["deepseek-flash"].supports_api("chat")

    # 缺省推导：未显式声明 api 时按 type 推导（body 缺省）
    derived = Route(provider="x", type="responses", model="m")
    assert derived.gateway_api == "responses" and derived.supports_api("responses")
    assert Route(provider="x", type="anthropic", model="m").gateway_api == "chat"

    # 显式声明 both 时两个入口皆可用
    assert Route(provider="x", type="chat", model="m", api="both").supports_api("responses")


def test_router_invalid_api_declaration_rejected():
    """路由 api 声明非法值 -> INVALID_REQUEST（配置快速失败）"""
    try:
        _build_route("__bad__", {"provider": "deepseek", "type": "chat", "api": "bogus"})
        raise AssertionError("非法 api 声明应报错")
    except LLMGatewayError as err:
        assert err.code == ErrorCode.INVALID_REQUEST


# ==================== C. 韧性 ====================

def test_circuit_breaker_state_machine():
    """熔断：达阈值打开 -> 冷却后半开放行 -> 成功重置；enabled=False 永不熔断"""
    cb = CircuitBreaker(tuning.circuit_breaker)
    for _ in range(tuning.circuit_breaker.failure_threshold - 1):
        assert cb.allow("x:y")
        cb.record_failure("x:y")
    assert cb.allow("x:y")
    cb.record_failure("x:y")
    assert not cb.allow("x:y"), "达阈值后应熔断"
    cb.record_success("x:y")
    assert cb.allow("x:y"), "成功后应重置"

    class _Disabled:
        enabled = False
        failure_threshold = 5
        cooldown_seconds = 30

    cb2 = CircuitBreaker(_Disabled())
    for _ in range(10):
        cb2.record_failure("k")
    assert cb2.allow("k"), "enabled=False 时不应熔断"


def test_gateway_circuit_open_fast_fail():
    """全路由熔断 -> CIRCUIT_OPEN / 503，且不发起上游请求"""
    with _patch(gateway.circuit_breaker, "allow", lambda key: False):
        try:
            asyncio.run(gateway.chat_completion("deepseek-chat", [{"role": "user", "content": "hi"}]))
            raise AssertionError("应抛 CIRCUIT_OPEN")
        except LLMGatewayError as err:
            assert err.code == ErrorCode.CIRCUIT_OPEN and err.http_status == 503


def test_gateway_rate_limit_fast_fail():
    """按模型限流超限 -> RATE_LIMITED / 429，且不发起上游请求"""
    with _patch(gateway, "check_model_rate_limit", lambda model, limit=60: False):
        try:
            asyncio.run(gateway.chat_completion("deepseek-chat", [{"role": "user", "content": "hi"}]))
            raise AssertionError("应抛 RATE_LIMITED")
        except LLMGatewayError as err:
            assert err.code == ErrorCode.RATE_LIMITED and err.http_status == 429


def test_rate_limit_sliding_window():
    """滑动窗口限流：超限拒绝、reset 后恢复"""
    ip = "__verify_ip__"
    reset_rate_limit(ip)
    assert check_rate_limit(ip, limit=2) and check_rate_limit(ip, limit=2)
    assert not check_rate_limit(ip, limit=2), "第 3 次应被拒绝"
    reset_rate_limit(ip)
    assert check_rate_limit(ip, limit=2)
    reset_rate_limit(ip)


def test_backoff_ceiling_and_status_mapping():
    """退避有基础值且被 max_delay 封顶；上游状态码映射可重试标记正确"""
    assert 0 < _backoff(0) <= tuning.retry.max_delay_seconds
    assert _backoff(20) <= tuning.retry.max_delay_seconds

    err = _upstream_status_error(429, "body")
    assert err.code == ErrorCode.RATE_LIMITED and err.retryable
    err = _upstream_status_error(503, "body")
    assert err.retryable and err.http_status == 502
    err = _upstream_status_error(400, "body")
    assert not err.retryable


# ==================== D. 结构化输出 ====================

def test_schema_from_request_normalization():
    """输入归一化：None / text / json_object / json_schema（两种形状）"""
    assert schema_from_request("chat", None) is None
    assert schema_from_request("chat", {"response_format": None}) is None
    assert schema_from_request("chat", {"response_format": {"type": "text"}}) is None
    assert schema_from_request("chat", {"response_format": {"type": "json_object"}}) == {"type": "json_object"}

    norm = schema_from_request("chat", {"response_format": {
        "type": "json_schema", "name": "calc", "schema": _JSON_SCHEMA}})
    assert norm == {"type": "json_schema", "name": "calc", "schema": _JSON_SCHEMA}

    openai_shape = schema_from_request("chat", {"response_format": {
        "type": "json_schema", "json_schema": {"name": "calc", "schema": _JSON_SCHEMA}}})
    assert openai_shape == {"type": "json_schema", "name": "calc", "schema": _JSON_SCHEMA}

    for bad in ({"type": "json_schema"}, {"type": "xml"}, "not-a-dict"):
        try:
            schema_from_request("chat", {"response_format": bad})
            raise AssertionError(f"应拒绝: {bad}")
        except LLMGatewayError as err:
            assert err.code == ErrorCode.INVALID_REQUEST


def test_content_from_response_three_protocols():
    """输出提取：chat / responses / anthropic"""
    chat = {"choices": [{"message": {"content": "hello"}}]}
    assert content_from_response("chat", chat) == "hello"

    responses = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "a"}, {"type": "reasoning", "text": "ignored"},
        {"type": "output_text", "text": "b"}]}]}
    assert content_from_response("responses", responses) == "ab"

    anthropic = {"content": [{"type": "text", "text": "x"}, {"type": "tool_use", "text": "ignored"}]}
    assert content_from_response("anthropic", anthropic) == "x"


def test_validate_response_and_fences():
    """校验：合法 JSON / 代码围栏剥离 / 非 JSON 报错 / schema 不符报错"""
    assert strip_fences("```json\n{}\n```") == "{}"
    assert strip_fences('```\n{"a":1}\n```') == '{"a":1}'

    chat = {"choices": [{"message": {"content": '{"answer": 8}'}}]}
    assert validate_response("chat", chat, None) == '{"answer": 8}'
    assert validate_response("chat", {"choices": [{"message": {"content": "```json\n{\"answer\": 8}\n```"}}]}, 
                             {"type": "json_schema", "schema": _JSON_SCHEMA}) == '{"answer": 8}'

    try:
        validate_response("chat", {"choices": [{"message": {"content": "not json"}}]}, None)
        raise AssertionError("非 JSON 应报错")
    except StructuredOutputError:
        pass

    bad_schema_payload = {"choices": [{"message": {"content": '{"answer": "eight"}'}}]}
    try:
        validate_response("chat", bad_schema_payload, {"type": "json_schema", "schema": _JSON_SCHEMA})
        raise AssertionError("schema 不符应报错")
    except StructuredOutputError:
        pass


def test_repair_instructions():
    """修复指令包含错误信息与目标 schema"""
    err = StructuredOutputError("不符合 schema")
    text = repair_instruction(err, {"type": "json_schema", "schema": _JSON_SCHEMA})
    assert "JSON" in text and "不符合 schema" in text
    assert "answer" in json_schema_instruction(_JSON_SCHEMA)


# ==================== E. 适配器 ====================

def test_adapter_payloads_and_headers():
    """请求体/鉴权差异：chat 注入约束、responses 映射 text.format、anthropic 用 x-api-key"""
    req = UnifiedRequest(
        model="deepseek-chat", messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        response_format={"type": "json_schema", "name": "calc", "schema": _JSON_SCHEMA},
        temperature=0.2, max_tokens=100,
    )

    chat = ChatAdapter("deepseek-chat", _ADAPTER_CFG)
    payload = chat.build_payload(req)
    assert payload["messages"][0]["role"] == "system" and "answer" in payload["messages"][0]["content"]
    assert payload["response_format"] == {"type": "json_object"}
    assert chat.headers()["Authorization"].startswith("Bearer ")

    resp = ResponsesAdapter("deepseek-v4-pro", _ADAPTER_CFG)
    payload = resp.build_payload(req)
    assert payload["instructions"] == "S"
    assert payload["input"] == [{"role": "user", "content": "U"}]
    assert payload["max_output_tokens"] == 100
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "calc"

    anthropic = AnthropicAdapter("deepseek-flash", _ADAPTER_CFG)
    payload = anthropic.build_payload(req)
    assert all(m["role"] != "system" for m in payload["messages"])
    assert "S" in payload["system"] and "answer" in payload["system"]
    assert "max_tokens" in payload
    assert anthropic.headers()["x-api-key"] == "test-key"


def test_adapter_response_normalization():
    """响应归一化：三协议文本与用量提取一致"""
    chat = ChatAdapter("deepseek-chat", _ADAPTER_CFG).parse_response({
        "id": "c1", "model": "deepseek-chat",
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })
    assert chat.text == "hi" and chat.finish_reason == "stop"
    assert (chat.usage.prompt_tokens, chat.usage.completion_tokens, chat.usage.total_tokens) == (3, 2, 5)

    resp = ResponsesAdapter("deepseek-v4-pro", _ADAPTER_CFG).parse_response({
        "id": "r1", "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "yo"}]}],
        "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
    })
    assert resp.text == "yo" and resp.usage.prompt_tokens == 4 and resp.usage.completion_tokens == 1

    ant = AnthropicAdapter("deepseek-flash", _ADAPTER_CFG).parse_response({
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2}, "stop_reason": "end_turn",
    })
    assert ant.text == "ok" and ant.finish_reason == "end_turn"
    assert ant.usage.total_tokens == 7


def test_adapter_stream_event_parsing():
    """流式事件解析：三协议增量与终止事件"""
    chat = ChatAdapter("deepseek-chat", _ADAPTER_CFG)
    assert chat.parse_stream_event("[DONE]").done
    assert chat.parse_stream_event('{"choices":[{"delta":{"content":"a"}}]}').text == "a"

    resp = ResponsesAdapter("deepseek-v4-pro", _ADAPTER_CFG)
    assert resp.parse_stream_event('{"type":"response.output_text.delta","delta":"b"}').text == "b"
    done = resp.parse_stream_event('{"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}')
    assert done.done and done.usage.total_tokens == 2

    ant = AnthropicAdapter("deepseek-flash", _ADAPTER_CFG)
    assert ant.parse_stream_event('{"type":"content_block_delta","delta":{"type":"text_delta","text":"c"}}').text == "c"
    assert ant.parse_stream_event('{"type":"message_stop"}').done


# ==================== F. 可观测 ====================

def test_usage_merge():
    """流式分片用量合并"""
    target = Usage()
    merge_usage(target, Usage(prompt_tokens=10))
    merge_usage(target, Usage(completion_tokens=5, total_tokens=15))
    assert (target.prompt_tokens, target.completion_tokens, target.total_tokens) == (10, 5, 15)


def test_usage_repository_record_and_query():
    """UsageEvent 落库与查询（决策调用记录持久化）"""
    _ensure_tables()
    event = UsageEvent(
        request_id="verify-req-1", api_key_hash="abc123", endpoint="/api/v1/chat",
        requested_model="deepseek-chat", provider="deepseek", upstream_model="deepseek-chat",
        status="success", status_code=200, input_tokens=1000, output_tokens=1000, cached_tokens=500,
        cost_usd=calculate_cost("deepseek-chat", 1000, 1000, 500), latency_ms=12.3,
        metadata={"cost_currency": "CNY"},
    )
    usage_repository_record = asyncio.run(usage_repository.record(event))
    assert usage_repository_record is None

    rows = asyncio.run(usage_repository.recent(limit=20))
    hit = next((r for r in rows if r["request_id"] == "verify-req-1"), None)
    assert hit, "落库的调用事件应可被查询到"
    assert hit["requested_model"] == "deepseek-chat" and hit["status"] == "success"
    assert hit["cached_tokens"] == 500 and abs(hit["cost_usd"] - 0.0026) < 1e-9
    assert (hit["metadata"] or {}).get("cost_currency") == "CNY"


# ==================== G. 提示词模板 ====================

def test_prompts_crud_render_and_versions():
    """模板版本自增、按版本引用、变量渲染、缺失变量保留占位、未找到报错（异步服务接口）"""

    async def _run():
        name = f"__verify_prompt_{int(time.time() * 1000)}__"
        v1 = await prompts.create_version(name, "你是{role}，用{lang}回答。", variables=["role", "lang"], description="v1")
        v2 = await prompts.create_version(name, "你是{role}。", variables=["role"], description="v2")
        assert (v1["version"], v2["version"]) == (1, 2)

        assert (await prompts.get_template(name))["version"] == 2                 # 默认最新
        assert (await prompts.get_template(name, 1))["version"] == 1             # 指定版本
        assert await prompts.render(name, {"role": "助手", "lang": "中文"}, 1) == "你是助手，用中文回答。"
        assert await prompts.render(name, {"role": "助手"}, 2) == "你是助手。"
        # 缺失变量保留占位符，不抛错
        assert await prompts.render(name, {}, 2) == "你是{role}。"
        assert any(p["name"] == name for p in await prompts.list_templates())

        try:
            await prompts.get_template("__no_such_prompt__")
            raise AssertionError("不存在的模板应报错")
        except LLMGatewayError as err:
            assert err.code == ErrorCode.PROMPT_NOT_FOUND

    _ensure_tables()
    asyncio.run(_run())


def test_prompts_concurrent_create_version_unique():
    """并发创建同一 name 的多个版本：版本号唯一且连续（唯一约束 + 冲突重试兜底）"""

    async def _run():
        name = f"__verify_prompt_race_{int(time.time() * 1000)}__"
        results = await asyncio.gather(
            *(prompts.create_version(name, f"模板 v{i}") for i in range(5))
        )
        versions = sorted(r["version"] for r in results)
        assert versions == [1, 2, 3, 4, 5], f"并发创建应得到唯一连续版本号，实际: {versions}"
        assert (await prompts.get_template(name))["version"] == 5

    _ensure_tables()
    asyncio.run(_run())


# ==================== H. 安全过滤 ====================

def test_security_sensitive_words():
    """敏感词检查与替换"""
    found, words = check_sensitive_words("这是一个暴力内容")
    assert found and words
    assert "***" in filter_sensitive_words("这是一个暴力内容")
    assert not check_sensitive_words("完全正常的一句话")[0]


# ==================== H. 错误体与调用方鉴权 ====================

def test_openai_style_error_body():
    """错误体为 OpenAI 风格（type/param），同时保留旧字段 code/message/detail"""
    err = LLMGatewayError(ErrorCode.MODEL_NOT_FOUND, "不支持的模型: x", param="model")
    body = err.to_dict()["error"]
    assert body["code"] == "model_not_found"          # 旧字段保留
    assert body["type"] == "invalid_request_error"    # OpenAI 语义分类
    assert body["param"] == "model"
    assert body["message"] and "detail" in body

    # 401 鉴权错误
    auth = LLMGatewayError(ErrorCode.UNAUTHORIZED, "缺少 Key")
    assert auth.http_status == 401
    assert auth.to_dict()["error"]["type"] == "authentication_error"

    # 限流 / 上游错误分类
    assert LLMGatewayError(ErrorCode.RATE_LIMITED, "x").to_dict()["error"]["type"] == "rate_limit_error"
    assert LLMGatewayError(ErrorCode.UPSTREAM_ERROR, "x").to_dict()["error"]["type"] == "api_error"


def _fake_request(headers: dict | None = None):
    """构造最小 Starlette Request（供鉴权依赖单测直接调用）"""
    from starlette.requests import Request as StarletteRequest

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "POST", "path": "/v1/chat/completions", "scheme": "http",
        "headers": raw, "query_string": b"", "client": ("127.0.0.1", 1234),
    }
    return StarletteRequest(scope)


def test_auth_dependency_optional_bearer():
    """未配置 api_keys -> 放行；配置后 -> Bearer/X-API-Key 校验（缺失或错误 401）"""
    import app.core.auth as auth_mod

    with _patch(auth_mod, "get_api_keys", lambda: []):
        asyncio.run(require_api_key(_fake_request()))  # 未配置：无 Key 也放行

    with _patch(auth_mod, "get_api_keys", lambda: ["secret-key"]):
        asyncio.run(require_api_key(_fake_request({"authorization": "Bearer secret-key"})))  # 放行
        asyncio.run(require_api_key(_fake_request({"x-api-key": "secret-key"})))             # 放行
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "secret-key"}):
            try:
                asyncio.run(require_api_key(_fake_request(headers)))
                raise AssertionError(f"应拒绝: {headers}")
            except LLMGatewayError as err:
                assert err.code == ErrorCode.UNAUTHORIZED and err.http_status == 401


# ==================== H. HTTP 端点（同步生成 E2E） ====================

def test_live_sync_generation_endpoints():
    """E2E：同步生成（4 模型）+ 结构化 + 故障转移 + 错误码 + 模板引用 + 流式 + 观测 + 健康检查"""
    if not _live_ready():
        raise SkipTest("GATEWAY_LIVE=0 或未配置 DEEPSEEK_API_KEY")
    asyncio.run(_live_checks())


async def _live_checks() -> None:
    """所有 E2E 断言在同一个事件循环内执行（避免复用跨 loop 的 httpx 客户端）"""
    _ensure_tables()
    base_messages = [{"role": "user", "content": "用一句话回答：1+1=?"}]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                 timeout=httpx.Timeout(180.0)) as c:
        # --- 健康检查 ---
        r = await c.get("/health/live")
        assert r.status_code == 200 and r.json()["status"] == "alive"
        r = await c.get("/health/ready")
        assert r.status_code == 200
        r = await c.get("/health/info")
        assert r.status_code == 200 and r.json().get("service")
        r = await c.get("/health/")
        assert r.status_code == 200 and r.json().get("uptime_seconds") is not None

        # --- 模型列表 ---
        r = await c.get("/api/v1/models")
        assert r.status_code == 200
        ids = {m["id"] for m in r.json()["models"]}
        for mid in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-flash", "smart"):
            assert mid in ids, f"/models 应包含 {mid}"
        entry = next(m for m in r.json()["models"] if m["id"] == "deepseek-v4-pro")
        assert entry["type"] == "responses" and entry["provider"] == "deepseek"

        # --- 运维别名：/healthz、/readyz、/admin/usage ---
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["status"] == "alive"
        r = await c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ready"
        r = await c.get("/admin/usage", params={"limit": 5})
        assert r.status_code == 200 and isinstance(r.json()["events"], list)

        # --- OpenAI 兼容层：/v1/models（OpenAI 列表结构） ---
        r = await c.get("/v1/models")
        assert r.status_code == 200 and r.json()["object"] == "list"
        v1_ids = {m["id"] for m in r.json()["data"]}
        assert {"deepseek-chat", "deepseek-v4-pro", "deepseek-flash"} <= v1_ids

        # --- OpenAI 兼容层：/v1/chat/completions（非流式） ---
        r = await c.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": base_messages})
        assert r.status_code == 200, r.text[:200]
        assert r.json()["object"] == "chat.completion"
        assert r.json()["choices"][0]["message"]["content"].strip()

        # --- OpenAI 兼容层：/v1/chat/completions（流式 SSE） ---
        v1_collected: list[str] = []
        async with c.stream("POST", "/v1/chat/completions", json={
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "用一句话说明什么是 SSE"}],
            "stream": True,
        }) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    payload = json.loads(line[5:].strip())
                    v1_collected.append(payload["choices"][0]["delta"].get("content") or "")
        assert "".join(v1_collected).strip(), "/v1 流式应产生正文增量"

        # --- OpenAI 兼容层：/v1/responses（仅声明 responses 能力的路由；协议透明代理） ---
        r = await c.post("/v1/responses", json={"model": "deepseek-v4-pro", "input": "用一句话回答：1+1=?"})
        assert r.status_code == 200, r.text[:200]
        assert r.json().get("output"), f"/v1/responses 应返回 Responses 结构: {r.text[:200]}"
        # chat-only 模型不得伪装成 Responses API -> 明确报错
        r = await c.post("/v1/responses", json={"model": "deepseek-chat", "input": "hi"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_found"
        # 不支持 stream
        r = await c.post("/v1/responses", json={"model": "deepseek-v4-pro", "input": "hi", "stream": True})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"

        # --- 同步生成：四个模型（核心） ---
        for mid in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-flash"):
            r = await c.post("/api/v1/chat", json={"model": mid, "messages": base_messages})
            assert r.status_code == 200, f"{mid} -> {r.status_code} {r.text[:200]}"
            body = r.json()
            assert body["object"] == "chat.completion", mid
            content = body["choices"][0]["message"]["content"]
            assert content and content.strip(), f"{mid} 返回内容为空"
            assert body["usage"]["total_tokens"] > 0, f"{mid} 用量缺失"

        # --- 结构化输出：chat（注入约束 + json_object + 本地 schema 校验） ---
        fmt = {"type": "json_schema", "name": "calc", "schema": _JSON_SCHEMA}
        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "计算 3+5，只返回符合 schema 的 JSON"}],
            "response_format": fmt,
        })
        assert r.status_code == 200, r.text[:200]
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        assert isinstance(data.get("answer"), int), data

        # --- 结构化输出：responses（原生 json_schema -> text.format） ---
        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "计算 7*6，只返回符合 schema 的 JSON"}],
            "response_format": fmt,
        })
        assert r.status_code == 200, r.text[:200]
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        assert isinstance(data.get("answer"), int), data

        # --- 结构化输出：json_object ---
        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "返回一个 JSON 对象，字段 name 为任意值"}],
            "response_format": {"type": "json_object"},
        })
        assert r.status_code == 200 and isinstance(json.loads(r.json()["choices"][0]["message"]["content"]), dict)

        # --- 故障转移（smart 首路由不可达 -> 自动切备用） ---
        r = await c.post("/api/v1/chat", json={"model": "smart", "messages": base_messages})
        assert r.status_code == 200, f"smart 故障转移失败: {r.text[:200]}"
        assert r.json()["choices"][0]["message"]["content"].strip()

        # --- 错误码：未知模型 / 空消息 / 敏感词 ---
        r = await c.post("/api/v1/chat", json={"model": "__no_such__", "messages": base_messages})
        assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_found"
        r = await c.post("/api/v1/chat", json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-chat", "messages": [{"role": "user", "content": "包含暴力内容"}]})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"

        # --- 上游失败也落库：临时注册仅含不可达路由的模型 ---
        SUPPORTED_MODELS["__unreachable__"] = ModelEntry(
            id="__unreachable__", display_name="unreachable", strategy="priority",
            routes=[Route(provider="dead", type="chat", model="dead-model",
                          base_url="http://127.0.0.1:9", api_key="x", timeout_seconds=5)],
        )
        try:
            r = await c.post("/api/v1/chat", json={"model": "__unreachable__", "messages": base_messages})
            assert r.status_code in (502, 503, 504), f"{r.status_code} {r.text[:200]}"
            assert r.json()["error"]["code"] in ("network_error", "upstream_error", "upstream_timeout")
        finally:
            SUPPORTED_MODELS.pop("__unreachable__", None)

        # --- 提示词模板：版本 / 渲染 / 引用 / 未找到 ---
        pname = f"verify-{int(time.time() * 1000)}"
        r = await c.post("/api/v1/prompts", json={"name": pname, "template": "你是{role}，用{lang}回答。",
                                                 "variables": ["role", "lang"]})
        assert r.status_code == 200 and r.json()["version"] == 1
        r = await c.post("/api/v1/prompts", json={"name": pname, "template": "你是{role}。"})
        assert r.json()["version"] == 2
        r = await c.get(f"/api/v1/prompts/{pname}")
        assert r.json()["version"] == 2
        r = await c.get(f"/api/v1/prompts/{pname}/1")
        assert r.json()["version"] == 1
        r = await c.post(f"/api/v1/prompts/{pname}/render",
                         json={"variables": {"role": "助手", "lang": "中文"}, "version": 1})
        assert "助手" in r.json()["rendered"]
        r = await c.get("/api/v1/prompts")
        assert any(p["name"] == pname for p in r.json()["prompts"])

        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-chat",
            "prompt": {"name": pname, "version": 1, "variables": {"role": "助手", "lang": "中文"}},
            "messages": [{"role": "user", "content": "你好"}],
        })
        assert r.status_code == 200, r.text[:200]

        r = await c.post("/api/v1/chat", json={
            "model": "deepseek-chat", "prompt": {"name": "__no_such_prompt__"},
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 404 and r.json()["error"]["code"] == "prompt_not_found"

        # --- 流式输出（SSE，anthropic 协议） ---
        collected: list[str] = []
        async with c.stream("POST", "/api/v1/chat/stream", json={
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "用一句话说明什么是 SSE"}],
        }) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    payload = json.loads(line[5:].strip())
                    collected.append(payload["choices"][0]["delta"].get("content") or "")
        assert "".join(collected).strip(), "流式应产生正文增量"

        # --- 观测：用量查询覆盖成功/失败/流式/降级/模板版本 ---
        r = await c.get("/api/v1/usage/recent", params={"limit": 100})
        events = r.json()["events"]
        assert events, "usage_events 应有记录"
        assert any(e["status"] == "success" and not e["stream"] for e in events), "应有非流式成功事件"
        assert any(e["stream"] and e.get("first_token_ms") for e in events), "应有流式事件且含首 Token 延迟"
        assert any(e["fallbacks"] >= 1 for e in events), "smart 故障转移应记录 fallbacks>=1"
        assert any(e.get("prompt_version") == 1 for e in events), "模板引用应记录 prompt_version"
        assert any(e["status"] == "error" for e in events), "上游失败应记录 error 事件"
        assert all(e.get("metadata") for e in events), "事件应含 metadata"


# ==================== 独立运行器 ====================

def main() -> int:
    if "--offline" in sys.argv:
        os.environ["GATEWAY_LIVE"] = "0"
        global LIVE_ENABLED
        LIVE_ENABLED = False

    tests = [(name, fn) for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = skipped = 0
    failures: list[tuple[str, Exception]] = []

    print(f"\n运行验证脚本：共 {len(tests)} 项"
          f"（实时上游={'开启' if _live_ready() else '关闭'}）\n" + "-" * 64)
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except SkipTest as exc:
            print(f"  SKIP  {name}  ({exc})")
            skipped += 1
        except Exception as exc:  # noqa: BLE001 逐项隔离，聚合报告
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
            failures.append((name, exc))

    print("-" * 64)
    print(f"结果：PASS {passed} / SKIP {skipped} / FAIL {len(failures)}")
    if failures:
        print("失败用例：" + "、".join(n for n, _ in failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
