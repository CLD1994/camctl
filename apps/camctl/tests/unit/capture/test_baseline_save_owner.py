"""并发恢复共享原保存阶段，并且只消费一次可靠回执。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline import PendingBaseline, resume_baseline_save
from camctl.capture.baseline_models import (
    BaselineChunkResult, BaselineChunkSave, BaselineFixSave, BaselineReadCallView,
    BaselineReadReturn, BaselineReadState, BaselineRef, BaselineState,
)
from camctl.capture.baseline_reads import PendingBaselineRead, resume_baseline_read_save
from camctl.capture.models import ActivityReleaseSave
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.file_identity import FileIdentity
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.capture import ActivityReleaseResult, CaptureRepository, ReleaseOutcome
from .test_baseline_read_models import _intent

pytestmark = pytest.mark.asyncio


def _observe_applications(monkeypatch, owner, fields):
    """保留真实对象的赋值行为，观察重复赋入同一回执的情况。"""
    applications = []
    assign = type(owner).__setattr__

    def observe(instance, name, value):
        if instance is owner and name in fields:
            applications.append((name, value))
        assign(instance, name, value)

    monkeypatch.setattr(type(owner), "__setattr__", observe)
    return applications


def _gated_database():
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    entered, release = asyncio.Event(), asyncio.Event()
    owned = object()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return callback(owned)

    database.write.side_effect = write
    return database, entered, release


async def _resume_together(resume, entered, release, cancel_waiter, before_release):
    tasks = [asyncio.create_task(resume()) for _ in range(2)]
    try:
        await entered.wait()
        if cancel_waiter is not None:
            tasks[cancel_waiter].cancel()
            await asyncio.sleep(0)
            assert not tasks[cancel_waiter].done()
        before_release()
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for index, result in enumerate(results):
        if index == cancel_waiter:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert not isinstance(result, BaseException), repr(result)
    return results


@pytest.mark.parametrize("cancel_waiter", [None, 0, 1], ids=["both-wait", "cancel-first", "cancel-second"])
async def test_baseline_chunk_concurrent_resume_applies_original_save_once(monkeypatch, mocker, cancel_waiter):
    intent = _intent()
    entries = tuple(FileIdentity(intent.binding, path) for path in ("/data/A/a.mp4", "/data/A/b.mp4"))
    cursor = DirectoryCursor(intent.binding, intent.directories, 0, "/data/A/b.mp4")
    owner = PendingBaseline(intent.activity_id, intent.binding, intent.directories,
        chunks=1, entries=3, next_cursor=cursor)
    request = BaselineChunkSave(intent.activity_id, 2, entries, 1000)
    original = owner.saves.begin(request)
    take = mocker.spy(owner.saves, "take")
    applications = _observe_applications(monkeypatch, owner, {"chunks", "entries", "cursor"})
    database, entered, release = _gated_database()
    capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
    capture.append_baseline.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BaselineChunkResult(intent.activity_id, 2, 12))
    runtime = SimpleNamespace(pending_baselines={intent.action_id: owner}, capture=capture, database=database)

    def before_release():
        assert owner.saves.pending is original
        assert (owner.chunks, owner.entries, owner.cursor) == (1, 3, None)
        assert applications == []

    results = await _resume_together(
        lambda: resume_baseline_save(intent.action_id, runtime=runtime),
        entered, release, cancel_waiter, before_release)

    assert all(result is None for index, result in enumerate(results) if index != cancel_waiter)
    assert {
        "writes": database.write.await_count,
        "takes": take.call_count,
        "applications": applications,
        "state": (owner.chunks, owner.entries, owner.cursor),
        "pending": owner.saves.pending,
    } == {
        "writes": 1,
        "takes": 1,
        "applications": [("chunks", 2), ("entries", 5), ("cursor", cursor)],
        "state": (2, 5, cursor),
        "pending": None,
    }


@pytest.mark.parametrize("kind", ["chunk", "fix", "release"])
async def test_baseline_applies_completed_save_before_notification_error(monkeypatch, mocker, kind):
    intent = _intent()
    cursor = DirectoryCursor(intent.binding, intent.directories, 0, "/data/A/b.mp4")
    owner = PendingBaseline(intent.activity_id, intent.binding, intent.directories,
        chunks=1, entries=3, next_cursor=cursor)
    if kind == "chunk":
        entries = tuple(FileIdentity(intent.binding, path)
            for path in ("/data/A/a.mp4", "/data/A/b.mp4"))
        owner.saves.begin(BaselineChunkSave(intent.activity_id, 2, entries, 1000))
        response = BaselineChunkResult(intent.activity_id, 2, 12)
        expected_applications = [("chunks", 2), ("entries", 5), ("cursor", cursor)]
    elif kind == "fix":
        owner.saves.begin(BaselineFixSave(intent.activity_id, 1, 3, 1000))
        response = BaselineRef(intent.activity_id, BaselineState.FIXED, 11, 11, 1, 3)
        expected_applications = [("completed", True), ("closed", True)]
    else:
        owner.release = (ActivityReleaseSave(intent.action_id, 1000,
            preparation_resolved=True), "3" * 32)
        response = ActivityReleaseResult(ReleaseOutcome.RELEASED)
        expected_applications = [("completed", True), ("closed", True)]
    take = mocker.spy(owner.saves, "take")
    applications = _observe_applications(monkeypatch, owner,
        {"chunks", "entries", "cursor", "completed", "closed"})
    failure = RuntimeError("已提交后的通知失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, response))
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    database.write.side_effect = failure
    runtime = SimpleNamespace(pending_baselines={intent.action_id: owner},
        capture=create_autospec(CaptureRepository, instance=True, spec_set=True), database=database)

    with pytest.raises(RuntimeError) as raised:
        await resume_baseline_save(intent.action_id, runtime=runtime)

    assert raised.value is failure
    assert {
        "writes": database.write.await_count,
        "takes": take.call_count,
        "applications": applications,
        "state": (owner.chunks, owner.entries, owner.cursor, owner.completed, owner.closed),
        "registered": runtime.pending_baselines.get(intent.action_id),
        "pending": owner.saves.pending,
    } == {
        "writes": 1,
        "takes": 0 if kind == "release" else 1,
        "applications": expected_applications,
        "state": (2, 5, cursor, False, False) if kind == "chunk" else (1, 3, None, True, True),
        "registered": owner if kind == "chunk" else None,
        "pending": None,
    }


@pytest.mark.parametrize("kind", ["intent", "return"])
@pytest.mark.parametrize("cancel_waiter", [None, 0, 1], ids=["both-wait", "cancel-first", "cancel-second"])
async def test_baseline_read_concurrent_resume_applies_original_save_once(monkeypatch, mocker, kind, cancel_waiter):
    intent = _intent()
    intent_view = BaselineReadCallView(7, 11, BaselineReadState.INTENDED, intent,
        None, None, None, None, None, HistoryBoundary(1, 11))
    owner = PendingBaselineRead(intent, intent_view=None if kind == "intent" else intent_view)
    if kind == "intent":
        request, response, applied_field = intent, intent_view, "intent_view"
        expected_result = None
    else:
        request = BaselineReadReturn(7, 1000, None, DirectoryRead(None, None, Page((), None)))
        response = BaselineReadCallView(7, 11, BaselineReadState.RETURNED, intent,
            12, 1000, None, None, None, HistoryBoundary(2, 12))
        applied_field, expected_result = "result", response
    original = owner.saves.begin(request,
        key=intent.call_key if kind == "intent" else intent.result_save_key)
    take = mocker.spy(owner.saves, "take")
    applications = _observe_applications(monkeypatch, owner, {"intent_view", "result"})
    database, entered, release = _gated_database()
    capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
    method = capture.save_baseline_read_intent if kind == "intent" else capture.save_baseline_read_return
    method.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)

    def before_release():
        assert owner.saves.pending is original
        assert owner.result is None
        assert applications == []

    results = await _resume_together(
        lambda: resume_baseline_read_save(owner, capture=capture, database=database),
        entered, release, cancel_waiter, before_release)

    assert all(result is expected_result for index, result in enumerate(results) if index != cancel_waiter)
    assert {
        "writes": database.write.await_count,
        "takes": take.call_count,
        "applications": applications,
        "intent_view": owner.intent_view,
        "result": owner.result,
        "pending": owner.saves.pending,
    } == {
        "writes": 1,
        "takes": 1,
        "applications": [(applied_field, response)],
        "intent_view": intent_view,
        "result": expected_result,
        "pending": None,
    }


@pytest.mark.parametrize("continued", [False, True], ids=["exhausted", "continued"])
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError],
    ids=["notification-error", "completed-cancellation"])
async def test_baseline_parent_consumes_completed_read_before_shared_delivery_error(
        monkeypatch, mocker, continued, failure_type):
    intent = _intent()
    cursor = (DirectoryCursor(intent.binding, intent.directories, 0, "/data/A/b.mp4")
        if continued else None)
    intent_view = BaselineReadCallView(7, 11, BaselineReadState.INTENDED, intent,
        None, None, None, None, None, HistoryBoundary(1, 11))
    read_owner = PendingBaselineRead(intent, intent_view=intent_view)
    request = BaselineReadReturn(7, 1000, None,
        DirectoryRead(None, None, Page((), cursor)))
    response = BaselineReadCallView(7, 11, BaselineReadState.RETURNED, intent,
        12, 1000, None, cursor, None, HistoryBoundary(2, 12))
    read_owner.saves.begin(request, key=intent.result_save_key)
    owner = PendingBaseline(intent.activity_id, intent.binding, intent.directories,
        read_owner=read_owner)
    take = mocker.spy(read_owner.saves, "take")
    applications = _observe_applications(monkeypatch, owner,
        {"read_no", "cursor", "exhausted", "read_owner"})
    database, entered, release = _gated_database()
    failure = failure_type("已提交后的交付失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, response))

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        raise failure

    database.write.side_effect = write
    runtime = SimpleNamespace(pending_baselines={intent.action_id: owner},
        capture=create_autospec(CaptureRepository, instance=True, spec_set=True),
        database=database)
    async def resume():
        try:
            return await resume_baseline_save(intent.action_id, runtime=runtime)
        except BaseException as error:
            return error

    tasks = [asyncio.create_task(resume()) for _ in range(2)]
    try:
        await entered.wait()
        assert applications == []
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(result is failure for result in results)
    assert read_owner.result is response
    assert read_owner.saves.pending is None
    assert {
        "writes": database.write.await_count,
        "takes": take.call_count,
        "applications": applications,
        "state": (owner.read_no, owner.cursor, owner.exhausted, owner.read_owner),
    } == {
        "writes": 1,
        "takes": 1,
        "applications": [("read_no", intent.read_no), ("cursor", cursor),
            ("exhausted", not continued), ("read_owner", None)],
        "state": (intent.read_no, cursor, not continued, None),
    }
    assert await resume_baseline_save(intent.action_id, runtime=runtime) is None
    assert database.write.await_count == 1
    assert len(applications) == 4
