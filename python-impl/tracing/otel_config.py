"""
全链路追踪 — OpenTelemetry集成
为每个Agent调用创建Span，记录延迟、Token消耗、路由决策等关键指标。
支持导出到Jaeger/Zipkin/LangSmith等后端。
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


_tracer = None


def init_tracer(
    service_name: str = "smart-cs-multi-agent",
    otlp_endpoint: str | None = None,
) -> None:
    """
    初始化OpenTelemetry追踪器。

    Args:
        service_name: 服务名称
        otlp_endpoint: OTLP收集器地址，None则输出到控制台
    """
    global _tracer

    if not _HAS_OTEL:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except ImportError:
            exporter = ConsoleSpanExporter()
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)


def get_tracer():
    """获取全局Tracer实例"""
    global _tracer
    if _tracer is None:
        if _HAS_OTEL:
            _tracer = trace.get_tracer("smart-cs-multi-agent")
        else:
            return None
    return _tracer


def trace_agent_call(agent_name: str) -> Callable:
    """
    Agent调用追踪装饰器。

    为每个Agent方法创建一个Span，记录：
    - agent.name: Agent名称
    - agent.duration_ms: 调用耗时
    - agent.input_size: 输入大小
    - agent.success: 是否成功

    用法：
        @trace_agent_call("knowledge_rag")
        async def process(self, state):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            tracer = get_tracer()

            if tracer is None:
                return await func(*args, **kwargs)

            span_name = f"agent.{agent_name}.{func.__name__}"

            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("agent.name", agent_name)
                span.set_attribute("agent.method", func.__name__)

                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.time() - start_time) * 1000

                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", True)

                    if isinstance(result, dict):
                        span.set_attribute("agent.result_keys", str(list(result.keys())))

                    return result

                except Exception as e:
                    duration_ms = (time.time() - start_time) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", False)
                    span.set_attribute("agent.error", str(e))
                    span.record_exception(e)
                    raise

        return wrapper
    return decorator


class AgentMetrics:
    """Agent调用指标收集器"""

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}
        self._request_latencies_ms: list[float] = []
        self._request_count = 0
        self._request_errors = 0
        self._compliance_total = 0
        self._compliance_passed = 0
        self._token_estimates: list[int] = []
        self._routing_total = 0
        self._routing_correct = 0

    def record_call(self, agent_name: str, duration_ms: float, success: bool):
        self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
        self._total_duration[agent_name] = self._total_duration.get(agent_name, 0.0) + duration_ms
        if not success:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def record_request(
        self,
        latency_ms: float,
        success: bool,
        token_estimate: int = 0,
        compliance_passed: bool | None = None,
        routing_correct: bool | None = None,
    ):
        self._request_count += 1
        self._request_latencies_ms.append(latency_ms)
        if len(self._request_latencies_ms) > 5000:
            self._request_latencies_ms = self._request_latencies_ms[-5000:]

        if not success:
            self._request_errors += 1

        if token_estimate > 0:
            self._token_estimates.append(token_estimate)
            if len(self._token_estimates) > 5000:
                self._token_estimates = self._token_estimates[-5000:]

        if compliance_passed is not None:
            self._compliance_total += 1
            if compliance_passed:
                self._compliance_passed += 1

        if routing_correct is not None:
            self._routing_total += 1
            if routing_correct:
                self._routing_correct += 1

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(values)
        k = int((len(sorted_values) - 1) * p)
        return sorted_values[k]

    def get_summary(self) -> dict[str, Any]:
        summary = {}
        for agent_name in self._call_counts:
            calls = self._call_counts[agent_name]
            total_ms = self._total_duration[agent_name]
            errors = self._error_counts.get(agent_name, 0)
            summary[agent_name] = {
                "total_calls": calls,
                "avg_duration_ms": total_ms / calls if calls > 0 else 0,
                "error_rate": errors / calls if calls > 0 else 0,
            }
        request_error_rate = self._request_errors / self._request_count if self._request_count > 0 else 0.0
        compliance_pass_rate = self._compliance_passed / self._compliance_total if self._compliance_total > 0 else 0.0
        routing_accuracy = self._routing_correct / self._routing_total if self._routing_total > 0 else 0.0
        avg_tokens = sum(self._token_estimates) / len(self._token_estimates) if self._token_estimates else 0.0

        return {
            "agent_metrics": summary,
            "system_metrics": {
                "total_requests": self._request_count,
                "request_error_rate": request_error_rate,
                "p95_latency_ms": self._percentile(self._request_latencies_ms, 0.95),
                "p99_latency_ms": self._percentile(self._request_latencies_ms, 0.99),
                "avg_tokens_per_request_estimate": avg_tokens,
                "compliance_pass_rate": compliance_pass_rate,
                "routing_accuracy_estimate": routing_accuracy,
            },
        }
