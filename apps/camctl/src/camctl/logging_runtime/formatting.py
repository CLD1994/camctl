"""普通日志与错误诊断共用的 JSON 外层字段。"""

from datetime import datetime, timezone
import json
from os import getpid

from camctl.logging_runtime.models import LogLevel


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_metadata(level: LogLevel, *, logged_at_utc=None, pid=None) -> dict:
    return {"level": level.name,
            "logged_at_utc": utc_now() if logged_at_utc is None else logged_at_utc,
            "pid": getpid() if pid is None else pid}


def encode_record(level: LogLevel, payload: dict, *, logged_at_utc=None, pid=None) -> str:
    document = {**payload, **log_metadata(level, logged_at_utc=logged_at_utc, pid=pid)}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
