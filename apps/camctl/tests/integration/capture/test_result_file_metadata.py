"""原 RESULTS 文件元数据与可靠文件状态分别保持各自权威。"""

import pytest

from camctl.capture.files import FileCompletionSave, FileObservationSave, FilePresenceSave, OwnershipSave
from camctl.capture.handlers import _begin_check_round, _finish_listing_result, _listing_round, _saved_result_listing
from camctl.capture.results import FileKind
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import RunOutcome
from camctl.persistence.models import DbOutcomeKind

from .test_result_consumer_saves import _actual, _consumer_world, _result_port
from .test_result_file_recovery import _fresh_runtime

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("latest_without_observation", [False, True])
async def test_saved_listing_preserves_v1_metadata_when_file_evidence_has_no_kind(tmp_path, latest_without_observation):
    owned, runtime, action_id, _handler = await _consumer_world(tmp_path, "record")
    actual = _actual(action_id, with_files=True, complete=True)
    original = actual.observations[0].data["entries"][0]
    original.update(original_name="original.mp4", media_type="video/mp4")
    actual.observations[0].data["entries"].append({
        "identity": "preview", "locator": {"path": "/DCIM/preview.jpg"},
        "kind": "photo", "complete": True, "size_bytes": 7,
        "original_name": "preview.jpg", "media_type": "image/jpeg", "paired_identity": "original",
    })
    driver = _result_port(runtime, actual)
    try:
        listing = await _listing_round(runtime, action_id)
        original_id = None
        for identity, locator, size, role in (
            ("original", {"path": "/DCIM/original.mp4"}, 41, 2),
            ("preview", {"path": "/DCIM/preview.jpg"}, 7, 3),
        ):
            discovered = runtime.capture.save_file_observation(FileObservationSave(
                action_id, identity, locator, listing.occurred_at), new_operation_key(), owned)
            assert discovered.kind is DbOutcomeKind.COMPLETED, discovered.error
            file_id = discovered.value.file_id
            if original_id is None:
                original_id = file_id
            owner = OwnershipSave(file_id, action_id, 1, role, {"listing": identity}, listing.occurred_at,
                **({"paired_device_file_id": original_id, "pairing_observation": {"paired_identity": "original"}}
                   if role == 3 else {}))
            for save, command in (
                (runtime.capture.save_file_presence, FilePresenceSave(file_id, 2, listing.occurred_at)),
                (runtime.capture.save_file_ownership, owner),
                (runtime.capture.save_file_completion, FileCompletionSave(file_id, 3, listing.occurred_at,
                    basis=1, observation={"size_verified": True}, size_bytes=size)),
            ):
                receipt = save(command, new_operation_key(), owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        _finish_listing_result(runtime, listing, retry_wait=latest_without_observation,
            end_run=None if latest_without_observation else RunOutcome.SUCCEEDED)
        if latest_without_observation:
            ticket = _begin_check_round(runtime, action_id).ticket
            assert ticket is not None
            runtime.finish(ticket, _actual(action_id, with_files=False), end_run=RunOutcome.SUCCEEDED)
        resumed = _fresh_runtime(owned, runtime)
        driver.list_results.side_effect = AssertionError("持久化文件输入恢复不能重新列举")

        saved = _saved_result_listing(resumed, action_id)

        entries = {entry.identity: entry for entry in saved.entries}
        assert entries["original"].kind is FileKind.VIDEO
        assert entries["original"].original_name == "original.mp4"
        assert entries["original"].media_type == "video/mp4"
        assert entries["original"].size_bytes == 41
        assert entries["preview"].kind is FileKind.PHOTO
        assert entries["preview"].original_name == "preview.jpg"
        assert entries["preview"].media_type == "image/jpeg"
        assert entries["preview"].paired_identity == "original"
        assert entries["preview"].size_bytes == 7
        assert all(entry.complete for entry in saved.entries)
        assert saved.registered_files["original"][0].kind is FileKind.VIDEO
        assert saved.registered_files["preview"][0].kind is FileKind.PHOTO
        if latest_without_observation:
            assert saved.outcome.observations == ()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()
