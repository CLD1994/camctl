"""报告发布取消后仍拥有当前文件阶段，结束后不继续发布。"""

import asyncio
from pathlib import Path
from unittest.mock import create_autospec
from types import SimpleNamespace

import pytest

from camctl.host_files.handoff import HandoffDirectories, PublishResult, PublishStage, WithdrawResult, WithdrawStage
from camctl.host_files.io import DirectorySyncStage
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.reporting import publication
from camctl.reporting.publication import PublicationSession, StagedReport
from camctl.bootstrap.background_flow import ReportBackgroundFlow

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("stage", ["stage", "list", "withdraw", "move"])
@pytest.mark.parametrize("actual_failure", [False, True])
async def test_cancel_waits_current_file_stage_and_preserves_actual_result(mocker, stage, actual_failure):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failed = OSError("actual file operation failed") if actual_failure else None
    staged_bytes = mocker.patch.object(publication, "_stage_payload", autospec=True, return_value=None)
    listed = mocker.patch.object(publication, "_list_stale_ready", autospec=True,
                                return_value=(("old-report",), None))
    withdrawn = mocker.patch.object(publication, "_withdraw_stale", autospec=True,
                                   return_value=WithdrawResult(WithdrawStage.WITHDRAWN, None))

    async def thread(function, *args):
        if ((stage == "stage" and function is staged_bytes)
                or (stage == "list" and function is listed)
                or (stage == "withdraw" and function is withdrawn)):
            entered.set()
            await release.wait()
            finished.set()
            if failed is not None:
                raise failed
        return function(*args)

    mocker.patch("camctl.reporting.publication.asyncio.to_thread", autospec=True, side_effect=thread)

    async def move(*args):
        if stage == "move":
            entered.set()
            await release.wait()
            finished.set()
            if failed is not None:
                raise failed
        return PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)

    moved = mocker.patch.object(publication, "_move_staged", autospec=True, side_effect=move)
    session = create_autospec(PublicationSession, instance=True, spec_set=True)
    session.prepare.return_value = session.record_intent.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    staged = StagedReport(name=publication.report_file_name(1, "b" * 64), size_bytes=42, sha256="b" * 64)
    directories = HandoffDirectories(Path("/staging"), Path("/ready"))
    operation = asyncio.create_task(
        publication.deliver_report(1, b"report", directories, session) if stage == "stage"
        else publication.deliver_staged_report(1, staged, directories, session))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        operation.cancel("first stop")
        await asyncio.sleep(0)
        operation.cancel("repeated stop")
        await asyncio.sleep(0)
        assert not operation.done()
        assert not finished.is_set()
        session.save_publication.assert_not_called()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(operation, timeout=1)
        assert caught.value.args == ("first stop",)
        assert finished.is_set()
        if failed is not None:
            assert caught.value.__cause__ is failed
            assert caught.value.report_file_result.error is failed
        elif stage == "move":
            assert any("moved" in note for note in caught.value.__notes__)
        session.save_publication.assert_not_called()
        session.settle_local.assert_not_called()
        if stage in ("stage", "list"):
            withdrawn.assert_not_called()
        if stage == "stage":
            session.prepare.assert_not_called()
        if stage != "move":
            moved.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("stage", [publication.ReportFileStage.WRITE, publication.ReportFileStage.LIST])
async def test_report_owner_keeps_actual_returned_file_error_after_stop(mocker, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    failure = "actual report file error"
    actual = failure if stage is publication.ReportFileStage.WRITE else ((), failure)

    async def file_step(function, *args):
        entered.set()
        await release.wait()
        return actual

    mocker.patch("camctl.reporting.publication.asyncio.to_thread", autospec=True, side_effect=file_step)
    session = create_autospec(PublicationSession, instance=True, spec_set=True)
    session.prepare.return_value = session.record_intent.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    directories = HandoffDirectories(Path("/staging"), Path("/ready"))
    staged = StagedReport(name=publication.report_file_name(1, "b" * 64), size_bytes=42, sha256="b" * 64)

    async def report(context):
        if stage is publication.ReportFileStage.WRITE:
            await publication.deliver_report(1, b"report", directories, session)
        else:
            await publication.deliver_staged_report(1, staged, directories, session)

    owner = ReportBackgroundFlow(report)
    await owner(SimpleNamespace())
    await asyncio.wait_for(entered.wait(), timeout=1)
    owner.stop_new_work()
    waiter = asyncio.create_task(owner.settle())
    try:
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        await asyncio.wait_for(waiter, timeout=1)
        assert owner.file_step_result.stage is stage
        assert owner.file_step_result.value == actual
        assert owner.file_step_result.error is None
        assert any(failure in note for note in owner.interrupted_error.__notes__)
        session.save_publication.assert_not_called()
        session.settle_local.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
