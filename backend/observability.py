"""可观测性：request_id 全链路 + 结构化 JSON 日志（P0-E）。

平移自 deepresearch 的方法论（该能力在另一个项目已验证），落到本项目：
  - 每个请求一个 request_id（可外部传入 X-Request-ID，或自动生成），存 ContextVar，
    贯穿所有日志 → 一条请求从头到尾可追踪。
  - 日志统一输出为单行 JSON，自动带 request_id/ts/level/logger，便于机器采集。
"""
import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """取当前请求的 request_id（无则 '-'）。"""
    return _request_id_ctx.get()


def set_request_id(rid: str | None = None) -> str:
    """设置当前请求的 request_id，返回实际值。"""
    rid = (rid or uuid.uuid4().hex[:12]).strip()
    _request_id_ctx.set(rid)
    return rid


class JsonFormatter(logging.Formatter):
    """把日志转成单行 JSON，自动带 request_id 等上下文。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 允许调用方通过 record.extra_fields 附加结构化字段
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger 为 JSON 输出（幂等：只装一次 handler）。"""
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
