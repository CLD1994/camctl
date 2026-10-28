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
    values_module = sys.modules[ConsistencyError.__module__]
    from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding, binding_failure_details
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    identity_module = sys.modules[CameraIdentity.__module__]
    from camctl.persistence.repositories import physical_alias
    action_physical_identity = physical_alias.action_physical_identity
    physical_alias_conflict = physical_alias.physical_alias_conflict
    source_physical_alias_conflict = physical_alias.source_physical_alias_conflict
    verify_source_identity_mismatch = physical_alias.verify_source_identity_mismatch
    from camctl.outputs.physical_binding import source_physical_binding, saved_source_binding


@pytest.fixture(autouse=True)
def no_resources(monkeypatch):
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    monkeypatch.setitem(sys.modules, values_module.__name__, values_module)
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
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.logical_held_identity_mismatch = lambda *args: False
    monkeypatch.setitem(sys.modules, target.__name__, target)
    assert verify_source_identity_mismatch(object(), {}, {"device_id": "old"}, {}, current) is expected


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
    transaction.row_facts = lambda connection, table, identity: source if table == "device_files" else {"id": identity, "device_id": "old"}
    monkeypatch.setitem(sys.modules, configuration.__name__, configuration)
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: CameraIdentity("SERIAL", 1))
    monkeypatch.setattr(physical_alias, "physical_alias_conflict", lambda *args: conflict)
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.logical_held_identity_mismatch = lambda *args: False
    monkeypatch.setitem(sys.modules, target.__name__, target)
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
    wait_api = ModuleType("camctl.persistence.repositories.availability_wait_finish")
    wait_api.saved_binding_check_failure = lambda *args: None
    modules = {module.__name__: module for module in (definitions, preparation_api, facts_api, errors_api, wait_api)}
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


@pytest.mark.parametrize("mismatch", [True, False])
def test_preparation_failure_rechecks_old_logical_identity(monkeypatch, mismatch):
    preparation_api = ModuleType("camctl.devices.capture_preparation")
    preparation_api.CAPTURE_ACTION_TYPES = frozenset({1})
    facts_api = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_api.preparation_calls_resolved = lambda *args: True
    facts_api.load_start_facts = lambda *args: SimpleNamespace(not_started=True, activity=None)
    errors_api = ModuleType("camctl.contracts.workflow_errors")
    errors_api.validate_error_details = lambda *args: None
    targets_api = ModuleType("camctl.persistence.repositories.availability_target")
    seen = []
    targets_api.logical_held_identity_mismatch = lambda connection, device, identity: seen.append((connection, device, identity)) or mismatch
    targets_api.photo_check_identity_mismatch = lambda *args: False
    wait_api = ModuleType("camctl.persistence.repositories.availability_wait_finish")
    wait_api.saved_binding_check_failure = lambda *args: None
    modules = {module.__name__: module for module in (definitions, preparation_api, facts_api, errors_api, targets_api, wait_api)}
    with patch.dict(sys.modules, modules):
        from camctl.persistence.repositories import preparation
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connection.execute.return_value.fetchone.return_value = None
        action = {"id": 1, "driver_id": definitions.DRIVER_ID, "device_id": "new", "type": 1,
                  "status": 1, "execution_started": 0, "cancel_requested": 0,
                  "execution_spec_json": {}, "scheduled_at": 10, "max_delay_ms": 0}
        request = {"details": {"device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
                               "reason": "physical_identity_mismatch"},
                   "current_identity": {"serial": "SERIAL", "product_id": 1},
                   "prepare_lead_s": Decimal(0), "trusted_wall_now": 10}
        if mismatch:
            assert preparation.verify_binding_preparation_failure(connection, action, request) is None
        else:
            with pytest.raises(preparation.BindingPreparationIneligible):
                preparation.verify_binding_preparation_failure(connection, action, request)
        assert seen == [(connection, "new", CameraIdentity("SERIAL", 1))]


@pytest.mark.parametrize("changes,now,not_started", [
    ({"status": 4}, 10, True), ({"cancel_requested": 1}, 10, True),
    ({}, 9, True), ({}, 11, True), ({"execution_started": 1}, 10, False),
    ({}, 10, False),
])
def test_old_logical_mismatch_cannot_override_action_eligibility(monkeypatch, changes, now, not_started):
    preparation_api = ModuleType("camctl.devices.capture_preparation")
    preparation_api.CAPTURE_ACTION_TYPES = frozenset({1})
    facts_api = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_api.preparation_calls_resolved = lambda *args: True
    facts_api.load_start_facts = lambda *args: SimpleNamespace(not_started=not_started, activity=None)
    errors_api = ModuleType("camctl.contracts.workflow_errors")
    errors_api.validate_error_details = lambda *args: None
    targets_api = ModuleType("camctl.persistence.repositories.availability_target")
    check = Mock(return_value=True)
    targets_api.logical_held_identity_mismatch = check
    modules = {module.__name__: module for module in (definitions, preparation_api, facts_api, errors_api, targets_api)}
    with patch.dict(sys.modules, modules):
        from camctl.persistence.repositories import preparation
        action = {"id": 1, "driver_id": definitions.DRIVER_ID, "device_id": "new", "type": 1,
                  "status": 1, "execution_started": 0, "cancel_requested": 0,
                  "execution_spec_json": {}, "scheduled_at": 10, "max_delay_ms": 0, **changes}
        request = {"details": {"device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
                               "reason": "physical_identity_mismatch"},
                   "current_identity": {"serial": "SERIAL", "product_id": 1},
                   "prepare_lead_s": Decimal(0), "trusted_wall_now": now}
        with pytest.raises(preparation.BindingPreparationIneligible):
            preparation.verify_binding_preparation_failure(object(), action, request)
        check.assert_not_called()


@pytest.mark.parametrize("blocked", [True, False])
def test_device_source_rechecks_same_logical_held_identity(monkeypatch, blocked):
    fixed = CameraIdentity("SERIAL", 1)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: fixed)
    target = ModuleType("camctl.persistence.repositories.availability_target")
    calls = []
    target.logical_held_identity_mismatch = lambda connection, device, identity: calls.append((connection, device, identity)) or blocked
    monkeypatch.setitem(sys.modules, target.__name__, target)
    connection = object()
    observer = {"device_id": "old"}
    assert verify_source_identity_mismatch(connection, {}, observer, {}, {"serial": "SERIAL", "product_id": 1}) is blocked
    assert calls == [(connection, "old", fixed)]


def test_read_binding_rejects_old_logical_held_identity_before_any_driver_call(monkeypatch):
    configuration = ModuleType("camctl.devices.drivers.hasselblad.configuration")
    configuration.read_camera_config = lambda declaration: SimpleNamespace(serial="SERIAL", product_id=1)
    transaction = ModuleType("camctl.persistence.transaction")
    source = {"observer_action_id": 2, "source_action_id": 3}
    transaction.row_facts = lambda connection, table, identity: source if table == "device_files" else {"id": identity, "device_id": "old"}
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.logical_held_identity_mismatch = lambda *args: True
    for dependency in (configuration, transaction, target):
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: CameraIdentity("SERIAL", 1))
    monkeypatch.setattr(physical_alias, "physical_alias_conflict", lambda *args: False)
    driver_call = Mock(side_effect=AssertionError("读取前不能调用设备"))
    driver = SimpleNamespace(devices={"camera": {}}, control=driver_call, open_read=driver_call)
    class Database:
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=object()))
    runtime = SimpleNamespace(database=Database(), devices={"camera": SimpleNamespace(driver=driver)})
    result = asyncio.run(source_physical_binding(runtime, 1, "camera", definitions.DRIVER_ID))
    assert result.status is BindingStatus.PHYSICAL_IDENTITY_MISMATCH
    assert result.current_identity == {"serial": "SERIAL", "product_id": 1}
    driver_call.assert_not_called()


def test_preparation_accepts_only_verified_finished_availability_without_activity(monkeypatch):
    preparation_api = ModuleType("camctl.devices.capture_preparation")
    preparation_api.CAPTURE_ACTION_TYPES = frozenset({1})
    facts_api = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_api.preparation_calls_resolved = lambda *args: True
    facts_api.load_start_facts = lambda *args: SimpleNamespace(not_started=True, activity=None)
    errors_api = ModuleType("camctl.contracts.workflow_errors")
    errors_api.validate_error_details = lambda *args: None
    targets_api = ModuleType("camctl.persistence.repositories.availability_target")
    targets_api.logical_held_identity_mismatch = lambda *args: True
    targets_api.photo_check_identity_mismatch = lambda *args: False
    modules = {module.__name__: module for module in (definitions, preparation_api, facts_api, errors_api, targets_api)}
    with patch.dict(sys.modules, modules):
        from camctl.persistence.repositories import preparation
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connection.execute.return_value.fetchone.return_value = (12,)
        verified = Mock()
        monkeypatch.setattr(preparation, "verify_unstarted_availability_checks", verified, raising=False)
        action = {"id": 1, "driver_id": definitions.DRIVER_ID, "device_id": "new", "type": 1,
                  "status": 1, "execution_started": 0, "cancel_requested": 0,
                  "execution_spec_json": {}, "scheduled_at": 10, "max_delay_ms": 0}
        request = {"details": {"device_id": "new", "expected_driver_id": definitions.DRIVER_ID,
                               "reason": "physical_identity_mismatch"},
                   "current_identity": {"serial": "SERIAL", "product_id": 1},
                   "prepare_lead_s": Decimal(0), "trusted_wall_now": 10}
        assert preparation.verify_binding_preparation_failure(connection, action, request) is None
        verified.assert_called_once_with(connection, action, request["details"], request["current_identity"])


@pytest.mark.parametrize("case,allowed", [("failed_binding", True), ("success", True),
    ("unsettled", False), ("active", False), ("wrong_owner", False), ("wrong_kind", False),
    ("missing_target", False), ("wrong_error", False), ("released_failed", True), ("held_failed", False)])
def test_preparation_rechecks_original_check_and_settlement(monkeypatch, case, allowed):
    preparation_api = ModuleType("camctl.devices.capture_preparation")
    preparation_api.CAPTURE_ACTION_TYPES = frozenset({1})
    facts_api = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_api.preparation_calls_resolved = lambda *args: True
    identity = CameraIdentity("SERIAL", 1)
    details = {"device_id": "old", "expected_driver_id": definitions.DRIVER_ID, "reason": "physical_identity_mismatch"}
    check = {"id": 9, "action_id": 1, "trigger_copy_id": None, "device_id": "old",
             "driver_id": definitions.DRIVER_ID, "expected_identity_json": {"serial": "SERIAL", "product_id": 1},
             "resource_key": identity.resource_key}
    run = {"id": 3, "kind": 12, "action_id": 1, "availability_check_id": 9,
           "responsibility_key": "availability/9", "status": 4, "retry_wait_required": 0,
           "error_json": {"code": "device_binding_unavailable", "stage": "execution", "details": details}}
    activity = {"id": 4, "action_id": 5, "resource_key": identity.resource_key,
                "expected_identity_json": check["expected_identity_json"], "occupancy_state": 1}
    owner = {"id": 5, "device_id": "old", "driver_id": definitions.DRIVER_ID}
    if case == "success":
        run.update(status=3, error_json=None)
        activity["occupancy_state"] = 2
    if case == "active": run["status"] = 2
    if case == "wrong_kind": run["kind"] = 1
    if case == "wrong_owner": owner["device_id"] = "other"
    if case == "wrong_error": run["error_json"] = {"code": "other"}
    if case in ("released_failed", "held_failed"):
        run["error_json"] = {"code": "device_availability_unconfirmed"}
        activity["occupancy_state"] = 2 if case == "released_failed" else 1
    rows = {("operation_runs", 3): run, ("device_availability_checks", 9): check,
            ("device_activities", 4): activity, ("actions", 5): owner}
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, ident: rows.get((table, ident))
    targets = ModuleType("camctl.persistence.repositories.availability_target")
    targets.iter_fixed_targets = lambda *args: iter(() if case == "missing_target" else [((1, 1, 4),)])
    def check_targets(*args):
        if case in ("wrong_owner", "missing_target"):
            raise ConsistencyError("固定目标损坏")
        return activity["occupancy_state"] == 2
    targets.verify_binding_check_targets = check_targets
    finish = ModuleType("camctl.persistence.repositories.availability_finish")
    def settled(run_id):
        if case == "unsettled": raise ConsistencyError("原结果未交付")
    finish.AvailabilityFinishReads = lambda connection: SimpleNamespace(require_settled=settled)
    wait = ModuleType("camctl.persistence.repositories.availability_wait_finish")
    wait.saved_binding_check_failure = lambda *args: (details, {"serial": "OTHER", "product_id": 1})
    failure = ModuleType("camctl.persistence.repositories.availability_failure")
    def failure_basis(*args):
        if case == "wrong_error": raise ConsistencyError("终态错误不受支持")
        return None if case == "released_failed" else object()
    failure.failure_basis = failure_basis
    modules = {m.__name__: m for m in (preparation_api, facts_api, transaction, targets, finish, wait, failure, definitions)}
    with patch.dict(sys.modules, modules):
        from camctl.persistence.repositories import preparation
        monkeypatch.setattr(preparation, "enum_for", lambda name: {
            "operation_runs.kind": SimpleNamespace(CHECK_DEVICE_AVAILABILITY=12),
            "operation_runs.status": SimpleNamespace(SUCCEEDED=3, FAILED=4, UNCONFIRMED=6),
            "device_activities.occupancy_state": SimpleNamespace(HELD=1, RELEASED=2)}[name])
        class Cursor:
            def fetchmany(self, limit): return [(3,)]
            def close(self): pass
        connection = SimpleNamespace(execute=lambda *args: Cursor())
        action = {"id": 1, "device_id": "old", "driver_id": definitions.DRIVER_ID}
        if allowed:
            preparation.verify_unstarted_availability_checks(connection, action, details, {"serial": "OTHER", "product_id": 1})
        else:
            with pytest.raises(ConsistencyError):
                preparation.verify_unstarted_availability_checks(connection, action, details, {"serial": "OTHER", "product_id": 1})


def test_saved_device_check_failure_keeps_original_identity_after_configuration_recovers(monkeypatch):
    transaction = ModuleType("camctl.persistence.transaction")
    source = {"observer_action_id": 2, "source_action_id": 3}
    transaction.row_facts = lambda connection, table, ident: source if table == "device_files" else {"id": ident}
    finish = ModuleType("camctl.persistence.repositories.availability_wait_finish")
    details = {"device_id": "camera", "expected_driver_id": definitions.DRIVER_ID,
               "reason": "physical_identity_mismatch"}
    calls = []
    finish.saved_binding_check_failure = lambda connection, action_id, resource_key, expected_identity: (
        calls.append((action_id, resource_key)) or (details, {"serial": "old-observation", "product_id": 10}))
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    monkeypatch.setitem(sys.modules, finish.__name__, finish)
    fixed = CameraIdentity("original", 10)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: fixed)
    class Database:
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=object()))
    result = asyncio.run(saved_source_binding(SimpleNamespace(database=Database()), 7, 11, "camera", definitions.DRIVER_ID))
    assert result.status is BindingStatus.PHYSICAL_IDENTITY_MISMATCH
    assert result.current_identity == {"serial": "old-observation", "product_id": 10}
    assert calls == [(11, fixed.resource_key)]


def test_saved_source_mismatch_remains_verifiable_after_old_limit_is_released(monkeypatch):
    fixed = CameraIdentity("SERIAL", 1)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: fixed)
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.logical_held_identity_mismatch = lambda *args: False
    wait = ModuleType("camctl.persistence.repositories.availability_wait_finish")
    details = {"device_id": "old", "expected_driver_id": definitions.DRIVER_ID,
               "reason": "physical_identity_mismatch"}
    wait.saved_binding_check_failure = lambda *args, **kwargs: (details, {"serial": "SERIAL", "product_id": 1})
    monkeypatch.setitem(sys.modules, target.__name__, target)
    monkeypatch.setitem(sys.modules, wait.__name__, wait)
    observer = {"device_id": "old", "driver_id": definitions.DRIVER_ID}
    assert verify_source_identity_mismatch(object(), {}, observer, {},
        {"serial": "SERIAL", "product_id": 1}, action_id=10)
    assert not verify_source_identity_mismatch(object(), {}, observer, {},
        {"serial": "SERIAL", "product_id": 1}, action_id=None)
