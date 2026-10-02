"""读取建档使用实际分配身份形成可恢复定位的固定名称。"""

from contextlib import closing
from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.host_files.paths import PathRuleError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command


def _seed_prior_names(connection):
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id, purpose,"
        " relative_path, retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (601, 12, NULL, 3, 'processing-temp/601.tmp', 1, 1, NULL, NULL, NULL, 1, 1, 1)"
    )
    connection.execute(
        "INSERT INTO deliveries (id, action_id, output_id, file_name, display_name, status,"
        " publication_intent_event_id, published_event_id, withdrawal_state, withdrawal_error_json,"
        " error_json, created_event_id, last_event_id, change_count)"
        " VALUES (901, 32, 702, '901.mp4', '另一交付', 1, NULL, NULL, 1, NULL, NULL, 1, 1, 1)"
    )
    connection.commit()


@pytest.mark.parametrize("internal", [False, True])
def test_read_creation_uses_allocated_file_and_delivery_id(read_targets, internal):
    owned = read_targets
    _seed_prior_names(owned.connection)
    if internal:
        owned.connection.execute("UPDATE actions SET scheduled_at=scheduled_at+60000000 WHERE type=4")
        owned.connection.commit()
    command = _command(internal=internal)
    if not internal:
        command = replace(command, delivery_display_name="../../用户可读名称.mp4")
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.target_file_id == 602
    with closing(owned.connection.execute(
        "SELECT relative_path FROM intermediate_files WHERE id=602"
    )) as cursor:
        assert cursor.fetchone() == (("recording-inputs/602.part" if internal else "deliveries/602.part"),)
    if not internal:
        assert result.value.delivery_id == 902
        with closing(owned.connection.execute("SELECT file_name, display_name FROM deliveries WHERE id=902")) as cursor:
            assert cursor.fetchone() == ("902.mp4", "../../用户可读名称.mp4")


@pytest.mark.parametrize("field", ["target_extension", "delivery_extension"])
def test_name_too_long_after_id_allocation_rolls_back(read_targets, field):
    owned = read_targets
    _seed_prior_names(owned.connection)
    command = replace(_command(), **{field: "x" * 252})
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, PathRuleError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("extension,expected", [(None, "deliveries/602"), ("x" * 251, "deliveries/602." + "x" * 251)])
def test_target_name_accepts_absent_extension_and_exact_length_limit(read_targets, extension, expected):
    owned = read_targets
    _seed_prior_names(owned.connection)
    result = OutputsRepository().grant_file(
        replace(_command(), target_extension=extension), new_operation_key(), owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    with closing(owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE id=602")) as cursor:
        assert cursor.fetchone() == (expected,)
