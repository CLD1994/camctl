from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.contracts.enums import enum_for
from camctl.outputs.read_attempts import run_owned_read
from camctl.outputs.segment_copy_reuse import (
    check_copy_is_locally_usable,
    copy_is_reusable,
    waiter_was_pending_at_boundary,
)
from camctl.host_files.io import HashResult
from camctl.host_files.tasks import FileTaskError


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


def _usable_copy():
    inspections = enum_for("segment_inspections.state")
    verifications = enum_for("file_copies.verification_state")
    retention = enum_for("intermediate_files.retention_state")
    cleanup = enum_for("intermediate_files.cleanup_state")
    return SimpleNamespace(
        inspection_state=int(inspections.COMPLETED),
        verification_state=int(verifications.MATCHED),
        committed_bytes=128,
        source_size=128,
        retention_state=int(retention.REQUIRED),
        cleanup_state=int(cleanup.NOT_NEEDED),
        target_file_id=41,
        input_relative_path="recording-inputs/check-41.bin",
        target_sha256="digest-128",
    )


@pytest.mark.asyncio
async def test_local_check_hash_is_registered_with_source_file_lease():
    class Executor:
        task = None

        async def run_owned_async_file_task(self, task):
            self.task = task
            return HashResult("digest-128", 128, None)

    executor = Executor()
    assert await check_copy_is_locally_usable(
        _usable_copy(), Path("/staging"), executor)
    assert executor.task.file_ids == (41,)
    assert executor.task.stage == "segment_copy_hash"


@pytest.mark.asyncio
async def test_unavailable_file_lease_falls_back_from_local_copy():
    class BusyExecutor:
        async def run_owned_async_file_task(self, _task):
            raise FileTaskError("同文件已有未结束任务")

    assert not await check_copy_is_locally_usable(
        _usable_copy(), Path("/staging"), BusyExecutor())


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
