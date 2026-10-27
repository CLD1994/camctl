"""只用连接替身验证其他逻辑设备的活动身份读取。"""

import sqlite3
import sys
from decimal import Decimal
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from camctl.contracts import enums

definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
definitions.DRIVER_ID = "hasselblad_cfv100c"
with (patch.object(enums, "resource_bytes", side_effect=AssertionError("单元测试不能读取包资源")),
      patch.dict(sys.modules, {definitions.__name__: definitions})):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.repositories.physical_alias import held_activity_alias_conflict


@pytest.fixture(autouse=True)
def no_resources(monkeypatch):
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))


def connection_for(rows):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.__iter__.return_value = iter(rows)
    connection.execute.return_value = cursor
    return connection, cursor


@pytest.mark.parametrize("rows,expected", [
    ([], False),
    ([('{"serial":"OTHER","product_id":1}', '["hasselblad_cfv100c","OTHER"]')], False),
    ([('{"serial":"SERIAL","product_id":2}', '["hasselblad_cfv100c","SERIAL"]')], True),
    ([('{"serial":"OTHER","product_id":1}', '["hasselblad_cfv100c","OTHER"]'),
      ('{"serial":"SERIAL","product_id":1}', '["hasselblad_cfv100c","SERIAL"]')], True),
])
def test_held_alias_uses_fixed_serial_and_checks_all_candidates(rows, expected):
    connection, cursor = connection_for(rows)
    assert held_activity_alias_conflict(connection, "new", CameraIdentity("SERIAL", 1)) is expected
    sql, parameters = connection.execute.call_args.args
    assert "a.device_id<>?" in sql and "d.occupancy_state=1" in sql
    assert parameters == ("hasselblad_cfv100c", "new")
    cursor.close.assert_called_once()


@pytest.mark.parametrize("raw,key", [
    (None, None),
    ('{"serial":"SERIAL","product_id":1}', '["hasselblad_cfv100c","OTHER"]'),
    ('{"serial":"SERIAL"}', '["hasselblad_cfv100c","SERIAL"]'),
])
def test_invalid_fixed_fact_is_consistency_error(raw, key):
    connection, cursor = connection_for([(raw, key)])
    with pytest.raises(ConsistencyError):
        held_activity_alias_conflict(connection, "new", CameraIdentity("SERIAL", 1))
    cursor.close.assert_called_once()


@pytest.mark.parametrize("has_conflict", [True, False])
def test_preparation_failure_rechecks_conflict_from_current_facts(monkeypatch, has_conflict):
    preparation_api = ModuleType("camctl.devices.capture_preparation")
    preparation_api.CAPTURE_ACTION_TYPES = frozenset({1})
    facts_api = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_api.preparation_calls_resolved = lambda *args: True
    facts_api.load_start_facts = lambda *args: SimpleNamespace(not_started=True, activity=None)
    errors_api = ModuleType("camctl.contracts.workflow_errors")
    errors_api.validate_error_details = lambda *args: None
    modules = {module.__name__: module for module in (definitions, preparation_api, facts_api, errors_api)}
    with patch.dict(sys.modules, modules):
        from camctl.persistence.repositories import preparation
        from camctl.persistence.repositories import physical_alias
        monkeypatch.setattr(physical_alias, "held_activity_alias_conflict", lambda *args: has_conflict)
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connection.execute.return_value.fetchone.return_value = None
        action = {"id": 1, "driver_id": definitions.DRIVER_ID, "device_id": "new", "type": 1,
                  "status": 1, "execution_started": 0, "cancel_requested": 0,
                  "execution_spec_json": {}, "scheduled_at": 10, "max_delay_ms": 0}
        request = {"details": {"device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
                               "reason": "physical_identity_conflict"},
                   "current_identity": {"serial": "SERIAL", "product_id": 1},
                   "prepare_lead_s": Decimal(0), "trusted_wall_now": 10}
        if has_conflict:
            assert preparation.verify_binding_preparation_failure(connection, action, request) is None
        else:
            with pytest.raises(preparation.BindingPreparationIneligible):
                preparation.verify_binding_preparation_failure(connection, action, request)
