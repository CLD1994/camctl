"""原准备责任查询只使用投影字段登记中的真实列。"""

import re

import pytest

from camctl.capture.preparation_discovery import preparation_responsibility_where
from camctl.history.events import business_columns


_TABLE_COLUMNS = {table: business_columns(table) | {"id"} for table in (
    "actions", "device_activities", "operation_runs", "operation_attempts")}


def _check_columns(sql):
    aliases = {"a": "actions", "d": "device_activities"}
    aliases.update((alias, table) for table, alias in re.findall(r"(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql))
    for alias, column in re.findall(r"\b([a-z]+)\.([a-z_]+)\b", sql):
        assert column in _TABLE_COLUMNS[aliases[alias]], f"{aliases[alias]}.{column}"


@pytest.mark.parametrize("include_pending", [False, True])
def test_responsibility_query_uses_registered_projection_columns(include_pending):
    _check_columns(preparation_responsibility_where(include_pending=include_pending))


def test_column_check_rejects_an_activity_column_without_projection_definition():
    sql = preparation_responsibility_where().replace("d.started_at", "d.created_event_id")
    with pytest.raises(AssertionError, match="device_activities.created_event_id"):
        _check_columns(sql)
