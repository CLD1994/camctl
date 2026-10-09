"""默认取消在保存新成功之前必须核实本会话已取得的媒体申请。"""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.capture.media_flow import DriverReadSessions, run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.media_retry_fixtures import _Catalog, media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_media_flow import ReadDriverDouble
from ..capture.test_media_result_retry import _ActualTools
from .test_binding_transactions import _CommitFailure
from .test_media_saved_result_consumers import (
    _cancel_and_end_original, _default_factories,
    _observe_reopened_flow_connection,
)


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("confirmation", ["saved", "rejected"])
@pytest.mark.parametrize("commit_after", [False, True], ids=["uncommitted", "committed"])
async def test_default_cancel_confirms_original_media_before_new_cancel_success(pipeline, monkeypatch, confirmation, commit_after):
    owned, roots, source_id = pipeline
    deps, factories, context = await _default_factories(pipeline, monkeypatch)
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools("check")
    flow = factories[0](owned, "cam-1").media
    assert flow is not None
    flow.sessions = DriverReadSessions(owned, driver, ticket=None)
    flow.tools = tools
    original_save = CaptureRepository.save_check_result
    inputs, order = [], []
    failed = False

    def save(repository, command, key, current):
        nonlocal failed
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        inputs.append((command, key))
        if not failed:
            failed = True
            return original_save(repository, command, key,
                replace(current, connection=_CommitFailure(current.connection, commit_after)))
        order.append("media_confirmation")
        if confirmation == "rejected":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK,
                error=ConsistencyError("original media history unavailable"))
        return original_save(repository, command, key, current)

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    reopened = None
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, 1, 1, source_id)
        assert len(inputs) == 1 and tools.probes == 1 and tools.repairs == 0
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        original_facts = saved_transaction_events(reopened.connection, inputs[0][1])
        assert (original_facts is not None) is commit_after
        flow_connections = _observe_reopened_flow_connection(context, reopened)
        if commit_after:
            _cancel_and_end_original(reopened)
        instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("new-cancel.json", {
            "request_id": "3", "created_at": instant, "name": "取消已结束媒体动作",
            "actions": [{"name": "新取消", "type": "cancel_task",
                "params": {"target": {"action_instance_id": "1"}}}]}),
            _Catalog(), CommandMode.RUN, _NOW + 30), new_operation_key(), reopened)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        newest = reopened.connection.execute("SELECT MAX(id) FROM actions WHERE type=6").fetchone()[0]
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (1,)
        for method in ("start_cancel_action", "apply_cancel_target", "finish_cancel_action"):
            original = getattr(CancellationRepository, method)

            def observed(repository, command, key, current, *, _original=original, _method=method):
                order.append(_method)
                return _original(repository, command, key, current)

            monkeypatch.setattr(CancellationRepository, method, observed)
        if confirmation == "rejected":
            with pytest.raises(StateDbFailure):
                await context.flows["cancel"](context)
            assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (1,)
            assert order == ["media_confirmation"]
        else:
            await context.flows["cancel"](context)
            assert order[0] == "media_confirmation"
            assert "start_cancel_action" in order and "finish_cancel_action" in order
            assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (3,)
        assert len(inputs) == 2 and inputs[1] == inputs[0]
        assert len(flow_connections) == 1
        assert tools.probes == 1 and tools.repairs == 0 and len(driver.opens) == 1
        original_target = reopened.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=1").fetchone()
        if commit_after or confirmation == "saved":
            assert original_target == (6, 1)
        else:
            assert original_target == (2, 0)
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)
