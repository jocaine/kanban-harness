"""OTel GenAI telemetry — auto-instrument Anthropic SDK + heartbeat integration.

Two concerns unified:
1. Observability: traces LLM calls (model, tokens, latency) via OTel semantic conventions
2. Heartbeat: fires session heartbeat on span events so stall detection stays informed

Activation: call init_telemetry() in main.py lifespan. If OTEL_EXPORTER_OTLP_ENDPOINT
is not set, uses in-memory stats only (no external dependency required).
"""

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable

logger = logging.getLogger("kh.telemetry")

_initialized = False
_stats = None
_heartbeat_processor = None


@dataclass
class TelemetryStats:
    """In-memory LLM call statistics — always available, no exporter needed."""

    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0
    calls_by_model: dict = field(default_factory=dict)
    errors: int = 0
    _lock: Lock = field(default_factory=Lock)

    def record_call(
        self, model: str, input_tokens: int, output_tokens: int,
        latency_ms: float, error: bool = False,
    ):
        with self._lock:
            self.total_calls += 1
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_latency_ms += latency_ms
            if error:
                self.errors += 1
            entry = self.calls_by_model.setdefault(
                model, {"calls": 0, "input_tokens": 0, "output_tokens": 0},
            )
            entry["calls"] += 1
            entry["input_tokens"] += input_tokens
            entry["output_tokens"] += output_tokens

    def snapshot(self) -> dict:
        with self._lock:
            avg = (
                round(self.total_latency_ms / self.total_calls, 1)
                if self.total_calls else 0
            )
            return {
                "total_calls": self.total_calls,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_latency_ms": round(self.total_latency_ms, 1),
                "avg_latency_ms": avg,
                "errors": self.errors,
                "by_model": dict(self.calls_by_model),
            }


def get_stats() -> TelemetryStats | None:
    return _stats


def init_telemetry() -> bool:
    """Initialize OTel GenAI instrumentation. Safe to call multiple times.

    Returns True if OTel tracing is active (exporter configured).
    Returns False if only in-memory stats are available (no exporter).
    """
    global _initialized, _stats

    if _initialized:
        return _stats is not None

    _stats = TelemetryStats()
    _initialized = True

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    has_otel_deps = _try_init_otel(otel_endpoint)

    if has_otel_deps:
        logger.info(
            "OTel GenAI 遥测已初始化 (endpoint=%s)",
            otel_endpoint or "none/in-memory",
        )
    else:
        logger.info("OTel 依赖未安装 - 仅使用内存统计")

    return has_otel_deps


def _try_init_otel(endpoint: str | None) -> bool:
    """Attempt to set up OTel tracing. Returns False if deps missing."""
    global _heartbeat_processor

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        return False

    resource = Resource.create({
        "service.name": "kanban-harness",
        "service.version": "0.6.0",
    })
    provider = TracerProvider(resource=resource)

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            exporter = OTLPSpanExporter(
                endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OTLP 导出器已配置: %s", endpoint)
        except ImportError:
            logger.warning("OTLP 导出器包缺失, 追踪数据无法导出")

    _heartbeat_processor = _HeartbeatSpanProcessor()
    provider.add_span_processor(_heartbeat_processor)
    trace.set_tracer_provider(provider)

    _try_instrument_anthropic()
    return True


def _try_instrument_anthropic():
    """Auto-instrument Anthropic SDK if instrumentor is available."""
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
        logger.info("AnthropicInstrumentor 已激活")
    except ImportError:
        logger.debug("AnthropicInstrumentor 不可用")


class _HeartbeatSpanProcessor:
    """Custom SpanProcessor that fires heartbeat callbacks on span events.

    Agents register their heartbeat callback via set_heartbeat_callback().
    When OTel records span start/end (e.g., LLM call), the callback fires,
    keeping stall detection informed that the agent is alive.
    """

    def __init__(self):
        self._callbacks: dict[int, Callable] = {}
        self._lock = Lock()

    def on_start(self, span, parent_context=None):
        import threading
        tid = threading.current_thread().ident
        with self._lock:
            cb = self._callbacks.get(tid)
        if cb:
            cb()

    def on_end(self, span):
        import threading
        tid = threading.current_thread().ident
        with self._lock:
            cb = self._callbacks.get(tid)
        if cb:
            cb()
        self._record_from_span(span)

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=None):
        pass

    def _record_from_span(self, span):
        """Extract token counts from completed LLM spans into in-memory stats."""
        if not _stats:
            return
        attrs = span.attributes or {}
        if "gen_ai.request.model" not in attrs:
            return
        model = attrs.get("gen_ai.request.model", "unknown")
        input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
        output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
        latency_ms = 0
        if span.end_time and span.start_time:
            latency_ms = (span.end_time - span.start_time) / 1_000_000
        error = span.status.is_ok is False if hasattr(span, "status") else False
        _stats.record_call(model, input_tokens, output_tokens, latency_ms, error)


def set_heartbeat_callback(callback: Callable[[], None] | None):
    """Register heartbeat callback for the current thread.

    Call with None to unregister when the agent session ends.
    Used by agent executors so OTel span events keep stall detection alive.
    """
    import threading
    tid = threading.current_thread().ident
    if _heartbeat_processor is None:
        return
    with _heartbeat_processor._lock:
        if callback:
            _heartbeat_processor._callbacks[tid] = callback
        else:
            _heartbeat_processor._callbacks.pop(tid, None)


@contextmanager
def trace_llm_call(model: str, role: str = "", requirement_id: int = 0):
    """Manual span context for subprocess-based LLM calls (claude CLI).

    Records timing in stats. If OTel is available, also creates a proper span.
    """
    span = None
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("kh.agents")
        span = tracer.start_span(
            f"gen_ai.{role or 'agent'}",
            attributes={
                "gen_ai.request.model": model,
                "gen_ai.system": "anthropic",
                "kh.agent.role": role,
                "kh.requirement_id": requirement_id,
            },
        )
    except ImportError:
        pass

    start = time.monotonic()
    error = False
    try:
        yield span
    except Exception:
        error = True
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        if _stats:
            _stats.record_call(model, 0, 0, elapsed_ms, error)
        if span:
            span.end()
