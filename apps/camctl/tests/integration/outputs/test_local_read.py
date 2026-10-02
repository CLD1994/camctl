"""正式主机产物建立独立取回副本，不占相机机会或套用设备配置。"""

from contextlib import closing
from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command
from .test_qualification import _seed_output


@pytest.fixture
def local_read(read_targets):
    owned = read_targets
    connection = owned.connection
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id, purpose,"
        " relative_path, retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (801, 11, NULL, 4, 'derived/801.mp4', 3, 1, 4096, ?, NULL, 1, 1, 1)",
        ("a" * 64,),
    )
    connection.execute("UPDATE outputs SET kind=2, device_file_id=NULL, intermediate_file_id=801 WHERE id=701")
    _seed_output(connection, 703, 11, 501)
    connection.execute("INSERT INTO output_origins (output_id, original_output_id) VALUES (701, 703)")
    connection.execute("UPDATE obtain_items SET basis=2, original_output_id=703 WHERE id=101")
    connection.commit()
    command = replace(_command(), source_device_file_id=None,
                      source_intermediate_file_id=801, config=None)
    return owned, command


def test_local_read_creates_independent_copy_with_local_config(local_read):
    owned, command = local_read
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.target_file_id == 802
    with closing(owned.connection.execute(
        "SELECT source_device_file_id, source_intermediate_file_id, source_size,"
        " source_sha256, slot_device_id FROM file_copies WHERE id=?", (result.value.copy_id,),
    )) as cursor:
        assert cursor.fetchone() == (None, 801, 4096, "a" * 64, None)
    with closing(owned.connection.execute(
        "SELECT max_attempts_used, timeout_s_json, retry_interval_s_json, attempts_used"
        " FROM operation_runs WHERE id=?", (result.value.run_id,),
    )) as cursor:
        assert cursor.fetchone() == (1, None, None, 0)
    with closing(owned.connection.execute("SELECT relative_path, retention_state FROM intermediate_files WHERE id=801")) as cursor:
        assert cursor.fetchone() == ("derived/801.mp4", 3)


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("fault", ["owner", "purpose", "retention", "identity"])
def test_local_read_rejects_invalid_fixed_source_before_status(local_read, canceled, fault):
    owned, command = local_read
    if fault == "owner":
        owned.connection.execute("UPDATE intermediate_files SET owner_action_id=12 WHERE id=801")
    elif fault == "purpose":
        owned.connection.execute("UPDATE intermediate_files SET purpose=3 WHERE id=801")
    elif fault == "retention":
        owned.connection.execute("UPDATE intermediate_files SET retention_state=1 WHERE id=801")
    else:
        command = replace(command, source_intermediate_file_id=999)
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("path", [
    "deliveries/801.mp4", "derived/802.mp4", "derived/801.bad-ext",
    "/derived/801.mp4", "derived/more/801.mp4", "derived//801.mp4",
    "derived/801." + "x" * 252,
])
def test_local_read_rejects_unrecoverable_saved_source_path(local_read, path, canceled):
    owned, command = local_read
    owned.connection.execute("UPDATE intermediate_files SET relative_path=? WHERE id=801", (path,))
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("path", ["derived/801", "derived/801." + "x" * 251])
def test_local_source_accepts_absent_extension_and_exact_name_length(local_read, path):
    owned, command = local_read
    owned.connection.execute("UPDATE intermediate_files SET relative_path=? WHERE id=801", (path,))
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error


def test_two_obtain_actions_create_independent_copies_of_same_local_output(local_read):
    owned, first_command = local_read
    owned.connection.execute("UPDATE action_dependencies SET depends_on_action_id=11 WHERE id=32")
    owned.connection.execute("UPDATE obtain_items SET output_id=701, basis=2, original_output_id=703 WHERE id=102")
    owned.connection.commit()
    second_command = replace(first_command, action_id=32, item_id=102)
    repository = OutputsRepository()
    first = repository.grant_file(first_command, new_operation_key(), owned)
    second = repository.grant_file(second_command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert second.kind is DbOutcomeKind.COMPLETED, second.error
    assert first.value.delivery_id != second.value.delivery_id
    assert (first.value.target_file_id, second.value.target_file_id) == (802, 803)
    with closing(owned.connection.execute(
        "SELECT source_intermediate_file_id, target_file_id, slot_device_id FROM file_copies ORDER BY id"
    )) as cursor:
        assert cursor.fetchall() == [(801, 802, None), (801, 803, None)]
