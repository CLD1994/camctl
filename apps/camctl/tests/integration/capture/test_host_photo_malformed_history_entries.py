"""主机准备申请的坏历史值须在各持久化读取入口拒绝。"""

import json

import pytest

from camctl.contracts.history_values import INITIAL_BOUNDARY, ReadOrder, ReadScope
from camctl.contracts.values import new_operation_key, ConsistencyError
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_host_photo_storage import _NOW, _photo_environment


@pytest.mark.parametrize("reader", ["global_page", "reverse_file", "host_receipt"])
def test_malformed_host_photo_request_is_rejected_by_history_readers(tmp_path, reader):
    from camctl.persistence.repositories.host_capture import (
        HostCaptureRepository,
        PrepareHostPhoto,
    )

    owned = _photo_environment(tmp_path)
    repository = HostCaptureRepository()
    key = new_operation_key()
    try:
        saved = repository.prepare_host_photo(1, _NOW, key, owned)
        assert saved.kind.value == "completed", saved.error
        files = saved.value
        event_id, body_json = owned.connection.execute(
            "SELECT id, body_json FROM history_events"
            " WHERE event_type=26 AND json_extract(body_json,'$.reason')=1"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        body = json.loads(body_json)
        body["evidence"]["host_photo_request"] = 0
        owned.connection.execute(
            "UPDATE history_events SET body_json=? WHERE id=?",
            (json.dumps(body), event_id),
        )
        owned.connection.commit()
        boundary = HistoryRepository(tmp_path / "state.db").current_boundary()
    finally:
        owned.connection.close()

    history = HistoryRepository(tmp_path / "state.db")
    if reader == "global_page":
        scope = ReadScope(
            order=ReadOrder.ASCENDING,
            previous_position=None,
            lower_position=event_id,
            upper_position=event_id,
            batch_limit=1,
            cursor_position=lambda cursor: cursor,
        )
        with pytest.raises(ConsistencyError):
            history.read_events(scope, boundary)
    elif reader == "reverse_file":
        with pytest.raises(ConsistencyError):
            history.restore_entity(
                "intermediate_file", files.original.file_id, INITIAL_BOUNDARY,
                event_batch_size=1,
            )
    else:
        verification = open_existing(
            tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        try:
            result = repository.verify_operation(
                PrepareHostPhoto(1, _NOW), key, verification)
            assert result.kind.value == "unknown"
        finally:
            verification.connection.close()
