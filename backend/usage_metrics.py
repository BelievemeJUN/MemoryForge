"""OpenTelemetry GenAI 标准指标——成本可观测（替换"callback hook + request_id"方案）。

面试可讲：
  - 成本可观测从「应用内钩子（callback + request_id 日志）」升级为
    「行业标准语义约定」OpenTelemetry GenAI（`gen_ai.client.token.usage`）——
    这是 LLM 可观测的主流方向，天然可对接 Prometheus / Grafana / 任意 OTLP 后端。
  - 每个 LLM 调用点显式上报（不经 callback 钩子），与 per-user Redis 预算熔断**双轨**：
      功能层（预算熔断，超限拒请求）+ 可观测层（标准 GenAI 指标，可回顾可告警）。
  - 导出可切：默认 Console（本地零依赖看输出），`OTEL_METRICS_EXPORTER=otlp` 时走 OTLP。
"""
import os
from typing import Any

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)

_METER_NAME = "memoryforge.genai"
_meter: Any = None
_counter: Any = None


def _get_counter():
    """惰性初始化 MeterProvider + gen_ai.client.token.usage counter（只建一次）。

    导出策略（环境变量 OTEL_METRICS_EXPORTER）：
      - 空（默认）：InMemory 采集，不打印——评测/生产不刷屏，指标可随时接管
      - console：每 5s 打印到 stderr（本地演示看输出）
      - otlp：发往 OTLP 后端（Prometheus/Grafana 等）
    """
    global _meter, _counter
    if _counter is not None:
        return _counter
    exporter = os.getenv("OTEL_METRICS_EXPORTER", "")
    if exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    elif exporter == "console":
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=5000
        )
    else:
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()  # 静默采集，不输出（默认）
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    _meter = metrics.get_meter(_METER_NAME)
    _counter = _meter.create_counter(
        "gen_ai.client.token.usage",
        unit="token",
        description="LLM token usage per call (OpenTelemetry GenAI semantic conventions)",
    )
    return _counter


def record_llm_usage(operation: str, resp: Any) -> int:
    """上报一次 LLM 调用的 token 用量（GenAI 标准指标），并返回 total（供现有记账复用）。

    operation: intent / chat / plan / write / fix / judge / task 等节点名。
    resp: langchain LLM 响应（含 usage_metadata / response_metadata）。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    total = int(um.get("total_tokens") or 0)
    inp = int(um.get("input_tokens") or 0)
    out = int(um.get("output_tokens") or 0)
    if not total:  # 兼容 response_metadata.token_usage 通道
        rm = getattr(resp, "response_metadata", None) or {}
        tu = rm.get("token_usage") or {}
        total = int(tu.get("total_tokens") or 0)
        inp = int(tu.get("prompt_tokens") or 0)
        out = int(tu.get("completion_tokens") or 0)
    if not total:
        return 0
    rm = getattr(resp, "response_metadata", None) or {}
    model = rm.get("model_name") or "unknown"
    _get_counter().add(
        total,
        {
            "gen_ai.operation.name": operation,
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": inp,
            "gen_ai.usage.output_tokens": out,
        },
    )
    return total


def shutdown():
    """强制导出并关闭（脚本/测试结束时调用；常驻服务由进程退出自动处理）。"""
    global _counter, _meter
    if _counter is not None:
        provider = metrics.get_meter_provider()
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _counter = None
    _meter = None
