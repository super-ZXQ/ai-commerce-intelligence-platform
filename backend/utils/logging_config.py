"""应用日志配置：请求 ID 贯通与可选 JSON 结构化输出。

请求 ID 存在 ContextVar 中，既写响应头，也由 logging filter 注入每一条日志，
因此 Nginx、FastAPI、Agent 三层可以用同一个 request_id 关联。
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

TEXT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] - %(message)s"


def set_request_id(value: str) -> Token:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str:
    return _REQUEST_ID.get()


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入 LogRecord，未处于请求上下文时为 "-"。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象，便于 Loki/ELK/CloudWatch 直接检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_format: str = "text") -> None:
    """配置根日志；已有 handler（如 pytest）只更新格式，不重复添加。"""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter = JsonFormatter() if log_format == "json" else logging.Formatter(TEXT_FORMAT)
    for handler in root.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
    root.setLevel(logging.INFO)
