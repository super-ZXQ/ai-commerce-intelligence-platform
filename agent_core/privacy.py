"""反馈只保存统计元数据；限制文件总量，不落问题、结果或凭据。"""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

_feedback_lock = Lock()


def append_feedback(path: str, payload: dict) -> None:
    # 单个Streamlit进程内串行化，最多约5MiB；备份依次覆盖，避免无限增长。
    with _feedback_lock:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=4, encoding="utf-8")
        try:
            handler.emit(logging.LogRecord("feedback", logging.INFO, "", 0, json.dumps(payload, ensure_ascii=False), (), None))
        finally:
            handler.close()
