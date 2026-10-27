"""只用连接替身验证其他逻辑设备的活动身份读取。"""

import asyncio
import sqlite3
import sys
from decimal import Decimal
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

import pytest

from camctl.contracts import enums

definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
definitions.DRIVER_ID = "hasselblad_cfv100c"
files_api = ModuleType("camctl.devices.drivers.hasselblad.files")
files_api.CameraFileIdentity = SimpleNamespace(from_source=lambda key, locator: SimpleNamespace(
    serial=locator["serial"], product_id=locator["product_id"],
    binding=SimpleNamespace(device_id=locator["device_id"], driver_id=definitions.DRIVER_ID)))
with (patch.object(enums, "resource_bytes", side_effect=AssertionError("单元测试不能读取包资源")),
      patch.dict(sys.modules, {definitions.__name__: definitions, files_api.__name__: files_api})):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding, binding_failure_details
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    identity_module = sys.modules[CameraIdentity.__module__]
    from camctl.persistence.repositories import physical_alias
    action_physical_identity = physical_alias.action_physical_identity
    physical_alias_conflict = physical_alias.physical_alias_conflict
    source_physical_alias_conflict = physical_alias.source_physical_alias_conflict
    verify_source_identity_mismatch = physical_alias.verify_source_identity_mismatch
    from camctl.outputs.physical_binding import source_physical_binding


@pytest.fixture(autouse=True)
def no_resources(monkeypatch):
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    monkeypatch.setitem(sys.modules, identity_module.__name__, identity_module)
    monkeypatch.setitem(sys.modules, physical_alias.__name__, physical_alias)
    monkeypatch.setitem(sys.modules, files_api.__name__, files_api)


def connection_for(rows, read_rows=(), *, activity_position=0, read_position=2):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursors = [create_autospec(sqlite3.Cursor, instance=True, spec_set=True) for _ in range(4)]
    for cursor in cursors:
        cursor.fetchone.return_value = None
    cursors[activity_position].fetchone.return_value = rows[0] if rows else None
    cursors[read_position].fetchone.return_value = read_rows[0] if read_rows else None
    connection.execute.side_effect = cursors
    return connection, cursors[0]


@pytest.mark.parametrize("rows,expected", [
    ([], False),
    ([('{"serial":"SERIAL","product_id":2}', '["hasselblad_cfv100c","SERIAL"]')], True),
])
def test_held_alias_uses_fixed_serial_and_checks_all_candidates(rows, expected):
    connection, cursor = connection_for(rows)
    assert physical_alias_conflict(connection, "new", CameraIdentity("SERIAL", 1)) is expected
    sql, parameters = connection.execute.call_args_list[0].args
    assert "a.device_id<>?" in sql and "d.occupancy_state=1" in sql and "d.resource_key=?" in sql
    assert parameters == ('["hasselblad_cfv100c","SERIAL"]', "hasselblad_cfv100c", "new")
    cursor.close.assert_called_once()


def test_released_activity_with_running_attempt_remains_a_conflict():
    row = ('{"serial":"SERIAL","product_id":1}', '["hasselblad_cfv100c","SERIAL"]')
    connection, _ = connection_for([row], activity_position=1)
    assert physical_alias_conflict(connection, "new", CameraIdentity("SERIAL", 1))
    sql = connection.execute.call_args_list[1].args[0]
    assert "t.status=1" in sql and "r.activity_id" in sql


def test_terminal_read_with_unfinished_attempt_remains_a_conflict():
    identity = CameraIdentity("SERIAL", 1)
    row = ('{"serial":"SERIAL","product_id":1}', identity.resource_key, "source-key",
           {"serial": "SERIAL", "product_id": 1, "device_id": "old"},
           "old", definitions.DRIVER_ID, "old", definitions.DRIVER_ID)
    connection, _ = connection_for([], [row], read_position=3)
    assert physical_alias_conflict(connection, "new", identity)
    assert "t.status=1" in connection.execute.call_args_list[3].args[0]


@pytest.mark.parametrize("product,expected", [(1, True), (2, False)])
def test_unfinished_device_read_checks_source_and_activity_identity(product, expected):
    identity = CameraIdentity("SERIAL", 1)
    row = ('{"serial":"SERIAL","product_id":1}', identity.resource_key, "source-key",
           {"serial": "SERIAL", "product_id": product, "device_id": "old"},
           "old", definitions.DRIVER_ID, "old", definitions.DRIVER_ID)
    connection, _ = connection_for([], [row])
    with patch.dict(sys.modules, {files_api.__name__: files_api}):
        if expected:
            assert physical_alias_conflict(connection, "new", identity)
        else:
            with pytest.raises(ConsistencyError):
                physical_alias_conflict(connection, "new", identity)
    sql = connection.execute.call_args_list[2].args[0]
    assert "c.committed_bytes<c.source_size" in sql and "r.status IN (1,2)" in sql


def test_source_conflict_checks_the_original_activity(monkeypatch):
    fixed = CameraIdentity("SERIAL", 1)
    monkeypatch.setattr(physical_alias, "action_physical_identity", lambda connection, action_id: fixed)
    monkeypatch.setattr(physical_alias, "physical_alias_conflict",
                        lambda connection, device_id, identity: device_id == "old" and identity == fixed)
    source = {"source_action_id": 4, "identity_key": "key",
              "locator_json": {"serial": "SERIAL", "product_id": 1, "device_id": "old"}}
    action = {"device_id": "old", "driver_id": definitions.DRIVER_ID}
    assert source_physical_alias_conflict(object(), source, action, action)
    with pytest.raises(ConsistencyError):
        source_physical_alias_conflict(object(), source, action, {**action, "device_id": "other"})


def test_original_activity_missing_identity_is_not_filled_from_configuration():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = (None, None)
    with pytest.raises(ConsistencyError):
        action_physical_identity(connection, 1)


def test_binding_conflict_uses_registered_reason_and_original_binding():
    binding = DeviceBinding("new", definitions.DRIVER_ID)
    result = BindingResult(BindingStatus.PHYSICAL_IDENTITY_CONFLICT, binding, None)
    assert binding_failure_details(result) == {
        "device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
        "reason": "physical_identity_conflict"}


@pytest.mark.parametrize("current,expected", [
    ({"serial": "OTHER", "product_id": 1}, True),
    ({"serial": "SERIAL", "product_id": 2}, True),
    ({"serial": "SERIAL", "product_id": 1}, False),
])
def test_device_source_mismatch_checks_exact_fixed_identity(monkeypatch, current, expected):
    fixed = CameraIdentity("SERIAL", 1)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: fixed)
    assert verify_source_identity_mismatch(object(), {}, {}, {}, current) is expected


def test_binding_mismatch_keeps_current_identity_out_of_public_error():
    binding = DeviceBinding("new", definitions.DRIVER_ID)
    result = BindingResult(BindingStatus.PHYSICAL_IDENTITY_MISMATCH, binding, None,
                           {"serial": "OTHER", "product_id": 1})
    assert binding_failure_details(result) == {
        "device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
        "reason": "physical_identity_mismatch"}


@pytest.mark.parametrize("eligible,attempts,allowed", [
    (True, [], True),
    (False, [], False),
    (True, [{"status": 1, "result_json": None}], False),
    (True, [{"status": 2, "result_json": None}], False),
    (True, [{"status": 2, "result_json": {}}], True),
    (False, [{"status": 2, "result_json": {}}], False),
])
def test_mismatch_finish_preserves_cancellation_and_unsettled_read(eligible, attempts, allowed):
    assert physical_alias.read_mismatch_can_finish(owner_eligible=eligible, attempts=attempts) is allowed


@pytest.mark.parametrize("current,conflict,expected", [
    (("OTHER", 1), True, BindingStatus.PHYSICAL_IDENTITY_MISMATCH),
    (("SERIAL", 2), True, BindingStatus.PHYSICAL_IDENTITY_MISMATCH),
    (("SERIAL", 1), True, BindingStatus.PHYSICAL_IDENTITY_CONFLICT),
    (("SERIAL", 1), False, None),
])
def test_read_binding_classifies_before_any_driver_call(monkeypatch, current, conflict, expected):
    configuration = ModuleType("camctl.devices.drivers.hasselblad.configuration")
    configuration.read_camera_config = lambda declaration: SimpleNamespace(serial=current[0], product_id=current[1])
    transaction = ModuleType("camctl.persistence.transaction")
    source = {"observer_action_id": 2, "source_action_id": 3}
    transaction.row_facts = lambda connection, table, identity: source if table == "device_files" else {"id": identity}
    monkeypatch.setitem(sys.modules, configuration.__name__, configuration)
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: CameraIdentity("SERIAL", 1))
    monkeypatch.setattr(physical_alias, "physical_alias_conflict", lambda *args: conflict)
    driver_call = Mock(side_effect=AssertionError("读取前不能调用设备"))
    driver = SimpleNamespace(devices={"camera": {}}, control=driver_call, open_read=driver_call)
    class Database:
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=object()))
    runtime = SimpleNamespace(database=Database(), devices={"camera": SimpleNamespace(driver=driver)})
    result = asyncio.run(source_physical_binding(runtime, 1, "camera", definitions.DRIVER_ID))
    assert (None if result is None else result.status) is expected
    driver_call.assert_not_called()


@pytest.mark.parametrize("raw,key", [
    (None, None),
    ('{"serial":"SERIAL","product_id":1}', '["hasselblad_cfv100c","OTHER"]'),
    ('{"serial":"SERIAL"}', '["hasselblad_cfv100c","SERIAL"]'),
])
def test_invalid_fixed_fact_is_consistency_error(raw, key):
    connection, cursor = connection_for([(raw, key)])
    with pytest.raises(ConsistencyError):
        physical_alias_conflict(connection, "new", CameraIdentity("SERIAL", 1))
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
        monkeypatch.setattr(physical_alias, "physical_alias_conflict", lambda *args: has_conflict)
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
