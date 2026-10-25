"""原准备责任查询只使用投影字段登记中的真实列。"""

import re

import pytest

from camctl.capture import preparation_discovery
from camctl.capture.preparation_discovery import (
    photo_preparation_start_where, preparation_responsibility_where,
)
from camctl.contracts.enums import enum_for
from camctl.history.events import business_columns


_TABLE_COLUMNS = {table: business_columns(table) | {"id"} for table in (
    "actions", "device_activities", "operation_runs", "operation_attempts")}


def _check_columns(sql):
    aliases = {"a": "actions", "d": "device_activities"}
    aliases.update((alias, table) for table, alias in re.findall(r"(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql))
    for alias, column in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", sql):
        assert column in _TABLE_COLUMNS[aliases[alias]], f"{aliases[alias]}.{column}"


@pytest.mark.parametrize("include_pending", [False, True])
def test_responsibility_query_uses_registered_projection_columns(include_pending):
    _check_columns(preparation_responsibility_where(include_pending=include_pending))


def test_column_check_rejects_an_activity_column_without_projection_definition():
    sql = preparation_responsibility_where().replace("d.started_at", "d.created_event_id")
    with pytest.raises(AssertionError, match="device_activities.created_event_id"):
        _check_columns(sql)


def test_responsibility_query_derives_capture_types_from_shared_contract(mocker):
    mocker.patch.object(preparation_discovery, "CAPTURE_ACTION_TYPES", (77,))
    assert preparation_responsibility_where().startswith("a.type IN (77)")


def test_photo_preparation_excludes_every_original_start_without_filtering_its_outcome():
    clause = photo_preparation_start_where()
    photo = int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
    start = int(enum_for("operation_runs.kind").START)
    assert clause.startswith(f"(a.type<>{photo} OR NOT EXISTS (")
    assert f"photo_start.action_id=a.id AND photo_start.kind={start}" in clause
    assert "status" not in clause and "effect" not in clause
    assert "operation_attempts" not in clause
    assert clause in preparation_responsibility_where(include_pending=True)
