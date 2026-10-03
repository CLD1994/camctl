"""正式登记从当前状态库核对来源，整组拒绝不留下成功终态。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository

from .test_recording_finish import _environment, _finish, _seed_action, _NOW


def _other_action(connection):
    connection.execute(
        "INSERT INTO plans (id,request_id,name,created_at,status,created_event_id,last_event_id,change_count)"
        " VALUES (2,4243,'other',?,1,1,1,1)", (_NOW,))
    _seed_action(connection, 2, 2)


@pytest.mark.parametrize("case", [
    "other_source", "unknown_source", "other_device", "other_driver", "non_capture",
    "wrong_catalog_action",
])
def test_registration_rejects_conflicting_source_facts(tmp_path, case):
    owned = _environment(tmp_path)
    connection = owned.connection
    try:
        command = _finish()
        if case in {"other_source", "other_device", "other_driver"}:
            _other_action(connection)
        if case == "other_source":
            connection.execute("UPDATE device_files SET source_action_id=2 WHERE id=11")
        elif case == "unknown_source":
            connection.execute("UPDATE device_files SET source_action_id=NULL,"
                               " ownership_evidence_json=NULL WHERE id=11")
        elif case in {"other_device", "other_driver"}:
            connection.execute("UPDATE device_files SET observer_action_id=2 WHERE id=11")
            column = "device_id" if case == "other_device" else "driver_id"
            connection.execute(f"UPDATE actions SET {column}='other' WHERE id=2")
        elif case == "non_capture":
            connection.execute("UPDATE actions SET type=4, effective_params_json=NULL, driver_id=NULL,"
                               " device_id=NULL, max_delay_ms=NULL WHERE id=1")
        else:
            command = replace(command, catalog_facts=OutputCatalogFacts(2, True))
        connection.commit()
        before = tuple(connection.iterdump())

        outcome = CaptureRepository().finish_capture(command, new_operation_key(), owned)

        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(connection.iterdump()) == before
    finally:
        connection.close()


def test_registration_accepts_other_observer_with_same_saved_binding(tmp_path):
    owned = _environment(tmp_path)
    connection = owned.connection
    try:
        _other_action(connection)
        connection.execute("UPDATE device_files SET observer_action_id=2 WHERE id=11")
        connection.commit()

        outcome = CaptureRepository().finish_capture(_finish(), new_operation_key(), owned)

        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.plan_status == 3
        assert connection.execute("SELECT status FROM plans WHERE id=2").fetchone() == (1,)
        assert connection.execute("SELECT source_action_id,device_file_id FROM outputs").fetchall() == [(1, 11)]
    finally:
        connection.close()
