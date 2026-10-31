"""活动表和打开入口的结构核对不读取真实包资源。"""

from enum import IntEnum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sqlite3
import sys
from types import ModuleType
from unittest.mock import create_autospec, patch

import pytest

from camctl import resources
from camctl.contracts import enums

with patch.object(enums, "resource_bytes", side_effect=AssertionError("禁止读取包资源")):
    from camctl.history import events
    from camctl.persistence import output_scope_structure as structure


_AUTHORITY = """CREATE TABLE device_activities (
    id INTEGER PRIMARY KEY,
    expected_identity_json TEXT,
    resource_key TEXT,
    CHECK ((expected_identity_json IS NULL) = (resource_key IS NULL))
) STRICT;"""


@pytest.mark.parametrize("change", [None, "identity", "constraint", "missing"])
def test_activity_structure_requires_fixed_identity_columns_and_constraint(monkeypatch, change):
    monkeypatch.setattr(structure, "resource_bytes", lambda name: _AUTHORITY.encode())
    actual = _AUTHORITY.rstrip(";")
    if change == "identity":
        actual = actual.replace("expected_identity_json TEXT,", "")
    elif change == "constraint":
        actual = actual.replace(" = (resource_key IS NULL)", " OR (resource_key IS NULL)")
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = None if change == "missing" else (actual,)
    connection.execute.return_value = cursor
    if change:
        with pytest.raises(ValueError):
            structure.verify_device_activity_structure(connection)
    else:
        structure.verify_device_activity_structure(connection)
    cursor.close.assert_called_once()


@pytest.fixture
def runtime(monkeypatch):
    versions = IntEnum("Version", {"V2": 2, "V4": 4, "V5": 5, "V6": 6})
    action_types = IntEnum("ActionType", {"CAMERA_TAKE_PHOTO": 1})
    definitions = {"database_metadata.format_version": versions, "actions.type": action_types}
    monkeypatch.setattr(enums, "enum_for", lambda column: definitions[column])
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("禁止读取包资源"))
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("禁止读取包资源"))
    source = find_spec("camctl.persistence.runtime")
    spec = spec_from_file_location("camctl.persistence._isolated_activity_runtime", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("format_version", [1, 6])
def test_open_structure_rejects_old_activity_definition(monkeypatch, runtime, format_version):
    monkeypatch.setattr(runtime, "expected_table_names", lambda: set())
    monkeypatch.setattr(events, "load_event_registry", lambda: {"tables": {}})
    monkeypatch.setattr(events, "business_tables_for_format", lambda version: ())
    monkeypatch.setattr(runtime, "_verify_format_constraints", lambda connection, version: None)
    monkeypatch.setattr(runtime, "_verify_read_end_structure", lambda connection: None)
    retry_structure = ModuleType("camctl.persistence.retry_wait_structure")
    retry_structure.verify_retry_wait_structure = lambda connection: None
    monkeypatch.setitem(sys.modules, retry_structure.__name__, retry_structure)

    def reject_old_structure(connection):
        raise ValueError("device_activities 结构不一致")

    monkeypatch.setattr(structure, "verify_device_activity_structure", reject_old_structure)
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.__iter__.return_value = iter(())
    with pytest.raises(runtime.DatabaseInvalidError, match="device_activities"):
        runtime._verify_structure(connection, format_version)
