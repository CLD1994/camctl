"""按权威 SQL 核对读取重试所需的有界查询索引。"""

from contextlib import closing
from functools import lru_cache
import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import ResourceError, resource_bytes


_INDEX_RESOURCES = {
    "history_events_row": "sql/history.sql",
    "operation_attempts_run_status": "sql/operations.sql",
    "operation_attempts_without_result": "sql/operations.sql",
}

_CAPTURE_SELECTION_INDEX = "actions_pending_capture_selection"


@lru_cache(maxsize=None)
def _expected(name):
    try:
        resource = "sql/core.sql" if name == _CAPTURE_SELECTION_INDEX else _INDEX_RESOURCES[name]
        authority = resource_bytes(resource).decode("utf-8")
    except (ResourceError, UnicodeError) as error:
        raise ValueError(f"读取重试索引的权威 SQL 不可读取: {name}") from error
    matches = re.findall(r"CREATE\s+INDEX\s+" + re.escape(name) + r"\s+ON\s+[^;]+;", authority, re.I)
    if len(matches) != 1:
        raise ValueError(f"读取重试索引的权威 SQL 缺失或重复: {name}")
    return _tokens(matches[0].rstrip(";"))


def verify_retry_wait_index(connection, name):
    """核对索引的实际表、键和条件，拒绝缺失或不一致的定义。"""
    expected = _expected(name)
    with closing(connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    )) as cursor:
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str) or _tokens(row[0]) != expected:
        raise ValueError(f"读取重试索引缺失或定义不符: {name}")


def verify_retry_wait_structure(connection):
    for name in _INDEX_RESOURCES:
        verify_retry_wait_index(connection, name)
