"""主机原图及预览由不同取回动作生成独立副本，保留正式源。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command


@pytest.mark.parametrize("kind,purpose,directory,extension", [
    (1, 5, "capture-originals", "3fr"),
    (1, 5, "capture-originals", "jpg"),
    (3, 6, "capture-previews", "jpg"),
])
@pytest.mark.parametrize("cleaned", [False, True])
def test_two_obtains_make_independent_copies_of_promoted_photo_source(
    read_targets, kind, purpose, directory, extension, cleaned,
):
    owned = read_targets
    connection = owned.connection
    connection.execute("UPDATE actions SET type=1 WHERE id=11")
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id, purpose,"
        " relative_path, retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (801, 11, NULL, ?, ?, 3, 1, 4096, ?, NULL, 1, 1, 1)",
        (purpose, f"{directory}/801.{extension}", "a" * 64),
    )
    source_name = "capture.3fr" if extension == "3fr" else "capture.jpg"
    if kind == 3:
        source_name = "capture.3fr.thumbnail.jpg"
    media_type = "image/x-hasselblad-3fr" if extension == "3fr" else "image/jpeg"
    connection.execute("UPDATE outputs SET kind=?, device_file_id=NULL, intermediate_file_id=801, original_name=?,media_type=? WHERE id=701", (kind, source_name, media_type))
    if kind == 3:
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id, purpose,"
            " relative_path, retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
            " created_event_id, last_event_id, change_count)"
            " VALUES (800, 11, NULL, 5, 'capture-originals/800.3fr', 3, 1, 8192, ?, NULL, 1, 1, 1)",
            ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO outputs (id, source_action_id, kind, device_file_id, intermediate_file_id,"
            " original_name, media_type, availability, error_json, media_json, cleanup_status,"
            " cleanup_error_json, created_event_id, last_event_id, change_count)"
            " SELECT 703, source_action_id, 1, NULL, 800, original_name, media_type, availability,"
            " error_json, media_json, cleanup_status, cleanup_error_json, created_event_id, last_event_id, change_count"
            " FROM outputs WHERE id=701")
        connection.execute("UPDATE outputs SET original_name='capture.3fr',media_type='image/x-hasselblad-3fr' WHERE id=703")
        connection.execute("INSERT INTO output_origins (id, output_id, original_output_id) VALUES (1, 701, 703)")
        connection.execute("UPDATE obtain_items SET basis=3, original_output_id=703, preview_output_id=701 WHERE id=101")
    connection.execute("UPDATE action_dependencies SET depends_on_action_id=11 WHERE id=32")
    connection.execute("UPDATE obtain_items SET output_id=701, basis=?, original_output_id=?, preview_output_id=? WHERE id=102", (3 if kind == 3 else 1, 703 if kind == 3 else None, 701 if kind == 3 else None))
    if cleaned:
        connection.execute("UPDATE intermediate_files SET cleanup_state=4 WHERE id=801")
        connection.execute("UPDATE outputs SET availability=3, cleanup_status=4 WHERE id=701")
    connection.commit()
    command = replace(_command(), source_device_file_id=None, source_intermediate_file_id=801,
                      config=None, target_extension=extension, delivery_extension=extension)
    repository = OutputsRepository()
    first = repository.grant_file(command, new_operation_key(), owned)
    second = repository.grant_file(replace(command, action_id=32, item_id=102), new_operation_key(), owned)
    if cleaned:
        assert first.kind is DbOutcomeKind.ROLLED_BACK
        assert second.kind is DbOutcomeKind.ROLLED_BACK
        assert connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
        return
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert second.kind is DbOutcomeKind.COMPLETED, second.error
    assert first.value.delivery_id != second.value.delivery_id
    assert first.value.target_file_id != second.value.target_file_id
    assert connection.execute("SELECT source_intermediate_file_id, target_file_id, slot_device_id FROM file_copies ORDER BY id").fetchall() == [(801, 802, None), (801, 803, None)]
    assert connection.execute("SELECT relative_path, retention_state FROM intermediate_files WHERE id=801").fetchone() == (f"{directory}/801.{extension}", 3)
