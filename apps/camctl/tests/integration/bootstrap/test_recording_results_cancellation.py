"""原录像终态事务可靠未提交后，已生效取消结束其普通登记资格。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.flows import _resolve_and_fix
from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, StartCancelAction
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import _Catalog, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure, _WaitingProbe, _attempts
from .test_read_default_consumers import _default_world

pytestmark = pytest.mark.asyncio


def _apply_cancellation(owned, terminal):
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cancel.json", {
        "request_id": "2", "created_at": instant, "name": "取消原录像",
        "actions": [{"name": "取消", "type": "cancel_task",
            "params": {"target": {"action_instance_id": "1"}}}]}),
        _Catalog(), CommandMode.RUN, _NOW + 10), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    if terminal is None:
        # 取消计划已受理，尚未对目标保存取消标记。
        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (2, 0)
        return
    origin, raw = owned.connection.execute("SELECT id,input_fields_json FROM actions WHERE type=6").fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, _NOW + 10), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    items = _resolve_and_fix(owned, repository, origin, raw, lambda: _NOW + 10)
    assert len(items) == 1
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        items[0], CancelApplyMode.WITH_STOP, _NOW + 10), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    if terminal:
        ended = CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(1, _NOW + 10), new_operation_key(), owned)
        assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (
        6 if terminal else 2, 1)


class _BeforeCandidates(Exception):
    """本次测试只验证前置责任核实，后续取消收场由拥有者推进。"""


class _CandidateBoundary:
    def __init__(self, connection, check):
        self.connection, self.check = connection, check

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith((
            "SELECT id, device_id, scheduled_at, max_delay_ms FROM actions",
            "SELECT t.id, t.attempt_no, r.id, r.kind, r.activity_id",
            "SELECT id FROM actions WHERE type = 2 AND status = 2",
            "SELECT id, status, input_fields_json FROM actions WHERE type = 6",
        )):
            self.check()
            raise _BeforeCandidates
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("terminal", [None, False, True], ids=["cancel-accepted", "cancel-effective", "target-canceled"])
async def test_default_entry_resolves_uncommitted_recording_decision_from_cancellation_facts(
        pipeline, monkeypatch, entrance, terminal):
    owned, roots, _source_id = pipeline
    deps, factories, context, reader, driver, _ends, wall, factory_calls = await _default_world(pipeline, monkeypatch)
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)
        original_attempts = _attempts(owned)
        original_save = CaptureRepository.finish_recording_results
        inputs, outcomes = [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, False)
                result = original_save(repository, request, key, replace(current, connection=fault))
                assert fault.commit_calls == 1
            else:
                result = original_save(repository, request, key, current)
            outcomes.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
        wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, runtime)
        request, key = inputs[0]
        assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        assert saved_transaction_events(reopened.connection, key) is None
        _apply_cancellation(reopened, terminal)
        assert _attempts(reopened) == original_attempts
        before = tuple(reopened.connection.iterdump())
        calls = tuple(driver.calls)

        def check():
            assert inputs == [(request, key), (request, key)]
            assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
            assert outcomes[1].value.disposition.value == ("saved" if terminal is None else "retired")
            assert not runtime.pending_recording_results
            events = saved_transaction_events(reopened.connection, key)
            if terminal is None:
                assert events is not None and {event["occurred_at"] for event in events} == {request.capture.occurred_at}
                assert reopened.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (3, 0)
                assert reopened.connection.execute("SELECT status FROM operation_runs WHERE id=?", (request.run_id,)).fetchone() == (3,)
                assert reopened.connection.execute("SELECT device_file_id FROM outputs").fetchall() == [(_source_id,)]
                assert _attempts(reopened) == original_attempts
            else:
                assert events is None
                assert tuple(reopened.connection.iterdump()) == before

        original_open = context.open_connection

        def open_connection():
            current = original_open()
            assert current.metadata == reopened.metadata and current.connection is not reopened.connection
            return replace(current, connection=_CandidateBoundary(current.connection, check))

        context.open_connection = open_connection
        selected = {"normal": context.flows["scheduling"], "residual": context.flows["residual"],
            "restricted": context.restricted_flows["winddown"], "normal-cancel": context.flows["cancel"],
            "restricted-cancel": context.restricted_flows["cancel"]}[entrance]
        with pytest.raises(_BeforeCandidates):
            await selected(context)
        check()
        assert len(factory_calls) == 1
        assert tuple(driver.calls) == calls and len(reader.requests) == 1
        assert runtime.results.calls == [1] and tools.calls == ["probe", "probe"]
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)
