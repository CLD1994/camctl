from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from camctl.contracts.enums import enum_for
from camctl.outputs.read_attempts import run_owned_read
from camctl.outputs.segment_copy_reuse import waiter_was_pending_at_boundary
from camctl.outputs.segment_copy_reuse import copy_is_reusable


def test_only_running_obtain_with_pending_selection_is_bound_at_publication():
    action_status = enum_for("actions.status")
    selection_status = enum_for("obtain_source_selections.status")
    assert waiter_was_pending_at_boundary(
        action_status=int(action_status.RUNNING),
        selection_status=int(selection_status.PENDING))
    assert not waiter_was_pending_at_boundary(
        action_status=int(action_status.SUCCEEDED),
        selection_status=int(selection_status.PENDING))
    assert not waiter_was_pending_at_boundary(
        action_status=int(action_status.FAILED),
        selection_status=int(selection_status.PENDING))
    assert not waiter_was_pending_at_boundary(
        action_status=int(action_status.RUNNING),
        selection_status=int(selection_status.FIXED))


@dataclass(frozen=True)
class CopyFacts:
    inspection_state: int
    verification_state: int
    committed_bytes: int
    source_size: int
    retention_state: int
    cleanup_state: int


def test_only_completed_verified_full_required_copy_is_reusable():
    inspections = enum_for("segment_inspections.state")
    verifications = enum_for("file_copies.verification_state")
    retention = enum_for("intermediate_files.retention_state")
    cleanup = enum_for("intermediate_files.cleanup_state")
    complete = CopyFacts(
        int(inspections.COMPLETED), int(verifications.MATCHED), 128, 128,
        int(retention.REQUIRED), int(cleanup.NOT_NEEDED))

    assert copy_is_reusable(complete)
    assert copy_is_reusable(replace(
        complete,
        verification_state=int(verifications.SOURCE_CHECKSUM_UNAVAILABLE)))
    assert not copy_is_reusable(None)
    assert not copy_is_reusable(replace(
        complete, inspection_state=int(inspections.PENDING)))
    assert not copy_is_reusable(replace(complete, verification_state=int(verifications.MISMATCHED)))
    assert not copy_is_reusable(replace(complete, committed_bytes=127))
    assert not copy_is_reusable(replace(complete, retention_state=int(retention.RELEASABLE)))
    assert not copy_is_reusable(replace(complete, cleanup_state=int(cleanup.COMPLETED)))


@pytest.mark.asyncio
async def test_local_read_pins_both_check_source_and_delivery_target():
    class Cursor:
        def fetchone(self):
            return 41, 52

    class Connection:
        def execute(self, query, parameters):
            assert "source_intermediate_file_id" in query
            assert parameters == (7,)
            return Cursor()

    class Executor:
        task = None

        async def run_owned_async_file_task(self, task):
            self.task = task
            return await task.body(None)

    executor = Executor()
    runtime = SimpleNamespace(
        file_executor=executor,
        owned=SimpleNamespace(connection=Connection()))

    async def body():
        return "read finished"

    assert await run_owned_read(runtime, 7, body) == "read finished"
    assert executor.task.file_ids == (52, 41)
