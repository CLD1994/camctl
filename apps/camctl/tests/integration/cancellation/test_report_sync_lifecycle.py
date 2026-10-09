"""真实同步与取消消费者的协作：生效事务、目标收场及 ACK 后恢复。"""

from __future__ import annotations

import sqlite3
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode, ProcessInput
from camctl.bootstrap.application import query_work_facts
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, report_flow
from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelApplyMode,
    CancellationEffect,
    FixedCancelSet,
    FixedTarget,
    FixCancelTargets,
    SelectionBasis,
    StartCancelAction,
)
from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
from camctl.session.service import StateDbFailure, _drive_flows
from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key
from camctl.contracts.enums import load_registry
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, business_columns, event_type_name, load_event_registry
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.validators import ValidatedEvent
from camctl.host_files.handoff import HandoffDirectories
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import commit_operation, row_facts
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    _ApplyCancelTargetCommand,
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.repositories.outputs import OutputsRepository, register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.models import SyncMode
from camctl.reporting.policy import (
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
    start_sync,
    record_local_report,
    finish_canceled_sync_action,
)
from camctl.reporting.publication import DbPublicationSession, DeliveryOutcome, deliver_report
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration

from ..acceptance.test_acceptance import Catalog
from ..persistence.test_runtime import _create_valid_database


_NOW = 1_750_000_000_000_000


@pytest.fixture
def environment(tmp_path):
    register_acceptance_guards()
    register_report_guards()
    register_sync_guard()
    register_cancellation_guards()
    register_capture_guards()
    register_outputs_guards()
    path = tmp_path / "state.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        yield owned, tmp_path
    finally:
        owned.connection.close()


def _process(owned, body):
    outcome = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("plan.json", body), Catalog(), CommandMode.RUN, _NOW),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value


def _register(owned, request_id, action):
    result = _process(owned, {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "同步生命周期",
        "actions": [action],
    })
    assert result.plan_id is not None
    return owned.connection.execute(
        "SELECT id FROM actions WHERE plan_id = ?", (result.plan_id,),
    ).fetchone()[0]


def _start(owned, request_id="1"):
    action_id = _register(owned, request_id, {
        "name": "补齐状态", "type": "report_status", "params": {"scope": "full"},
    })
    outcome = start_sync(
        new_operation_key(), owned, action_id=action_id,
        mode=SyncMode.FULL, occurred_at=_NOW,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return action_id


def _fix_cancel(owned, target_id, request_id="3"):
    origin_id = _register(owned, request_id, {
        "name": "取消补齐", "type": "cancel_task",
        "params": {"target": {"action_instance_id": str(target_id)}},
    })
    repository = CancellationRepository()
    started = repository.start_cancel_action(
        StartCancelAction(origin_id, _NOW), new_operation_key(), owned,
    )
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(
        FixCancelTargets(origin_id, FixedCancelSet((FixedTarget(
            target_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED,
        ),)), _NOW), new_operation_key(), owned,
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    return origin_id, fixed.value.item_ids[0], repository


def _settlement(owned, repository):
    return TargetSettlement(
        owned=owned, outputs=OutputsRepository(), cancellations=repository,
        withdrawal_positions=lambda _delivery_id: "ready", occurred_at=lambda: _NOW,
    )


async def _publish(owned, home, report=None):
    if report is None:
        frozen = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=_NOW)
        assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
        report = frozen.value.report
    generated = generate_report_file(GenerationSpec(
        db_path=home / "state.db", report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=home / "generated.json",
    ))
    ready = home / "ready"
    ready.mkdir(exist_ok=True)
    published = await deliver_report(
        report.report_id, generated.path.read_bytes(),
        HandoffDirectories(home / "staging", ready), DbPublicationSession(owned),
    )
    assert published.outcome is DeliveryOutcome.PUBLISHED, published.error
    return report


def _action(owned, action_id):
    return owned.connection.execute(
        "SELECT status, execution_started, cancel_requested FROM actions WHERE id = ?",
        (action_id,),
    ).fetchone()


def _sync(owned, action_id):
    return owned.connection.execute(
        "SELECT status, local_report_id, ack_report_id, ended_event_id FROM state_syncs"
        " WHERE action_id = ?", (action_id,),
    ).fetchone()


def test_apply_cancel_ends_running_sync_in_same_transaction(environment):
    owned, _home = environment
    target_id = _start(owned)
    other_id = _start(owned, "2")
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    other_sync = _sync(owned, other_id)
    key = new_operation_key()

    outcome = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), key, owned,
    )

    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert _action(owned, target_id) == (2, 1, 1)
    sync = _sync(owned, target_id)
    assert sync[0] == 3
    transaction_id = owned.connection.execute(
        "SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),),
    ).fetchone()[0]
    assert owned.connection.execute(
        "SELECT transaction_id, event_type, change_seq FROM history_events WHERE id = ?",
        (sync[3],),
    ).fetchone() == (transaction_id, 29, None)
    assert _sync(owned, other_id) == other_sync


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_running_sync_cancel_settles_target_before_member_success(environment, published):
    owned, home = environment
    target_id = _start(owned)
    if published:
        await _publish(owned, home)
    origin_id, item_id, repository = _fix_cancel(owned, target_id)

    progress = await apply_cancel(
        ApplyCancel(origin_id, (item_id,)), CancellationRuntime(
            owned=owned, repository=repository, settlement=_settlement(owned, repository),
            occurred_at=lambda: _NOW,
        ),
    )

    assert _action(owned, target_id) == (6, 1, 1)
    assert (progress.items[0].status, progress.items[0].outcome) == (3, 1)
    assert _sync(owned, target_id)[0] == 3
    assert owned.connection.execute(
        "SELECT status FROM plans WHERE id = (SELECT plan_id FROM actions WHERE id = ?)",
        (target_id,),
    ).fetchone() == (3,)


@pytest.mark.asyncio
async def test_acknowledged_running_sync_can_cancel_without_rewriting_ack(environment):
    owned, home = environment
    target_id = _start(owned)
    report = await _publish(owned, home)
    _process(owned, {"request_id": "1", "last_report_id": str(report.report_id)})
    acknowledged = _sync(owned, target_id)
    assert acknowledged[:3] == (2, None, report.report_id)
    origin_id, item_id, repository = _fix_cancel(owned, target_id)

    await apply_cancel(
        ApplyCancel(origin_id, (item_id,)), CancellationRuntime(
            owned=owned, repository=repository, settlement=_settlement(owned, repository),
            occurred_at=lambda: _NOW,
        ),
    )

    assert _action(owned, target_id) == (6, 1, 1)
    assert _sync(owned, target_id) == acknowledged


class _FailedGeneration:
    """隔离后续报告进程失败；本地结果恢复仍由真实消费者保存。"""

    async def generate(self, job):
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE, job_id=job.job_id,
            failure=ResultFailureMessage(
                job_id=job.job_id, instance_id=job.instance_id,
                error_kind=ErrorKind.REPORT, error_code="controlled_report_failure",
                error_message="本轮后续报告生成失败",
            ),
        )


def _maintenance(home):
    cfg = load_config({}, ConfigDefaults())
    flow = report_flow(
        state_db=home / "state.db", staging=home / "staging", ready=home / "ready",
        processing=home / "processing", history=cfg.history, database=cfg.database,
        supervisor=_FailedGeneration(), start_actions=False,
    )
    context = SimpleNamespace(
        open_connection=lambda: open_existing(
            home / "state.db", DbOpenMode.EXISTING_RW, DbConfig(),
        ), clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )
    return flow, context


@pytest.mark.asyncio
async def test_acknowledged_sync_recovers_local_action_success(environment):
    owned, home = environment
    target_id = _start(owned)
    report = await _publish(owned, home)
    _process(owned, {"request_id": "1", "last_report_id": str(report.report_id)})
    acknowledged = _sync(owned, target_id)
    flow, context = _maintenance(home)

    await flow(context)

    assert _action(owned, target_id) == (3, 1, 0)
    assert _sync(owned, target_id) == (2, report.report_id, acknowledged[2], acknowledged[3])


@pytest.mark.asyncio
async def test_work_facts_keeps_acknowledged_local_result_pending(environment):
    owned, home = environment
    _start(owned)
    report = await _publish(owned, home)
    _process(owned, {"request_id": "1", "last_report_id": str(report.report_id)})

    facts = query_work_facts(owned.connection)

    assert facts.pending_report_changes is True


class _FaultConnection:
    """仅对指定真实事务语句注入一次错误，其余读写由 SQLite 执行。"""

    def __init__(self, connection, prefix, *, after_commit=False):
        self.connection = connection
        self.prefix = prefix
        self.after_commit = after_commit
        self.failed = False

    def execute(self, sql, parameters=()):
        if sql.startswith(self.prefix) and not self.failed:
            self.failed = True
            if self.after_commit:
                self.connection.execute(sql, parameters)
            raise sqlite3.OperationalError("受控事务失败")
        return self.connection.execute(sql, parameters)

    def close(self):
        self.connection.close()


def _fault_owned(owned, prefix, *, after_commit=False):
    return OwnedConnection(
        connection=_FaultConnection(owned.connection, prefix, after_commit=after_commit),
        metadata=owned.metadata,
    )


@pytest.mark.parametrize("prefix", ["UPDATE actions", "UPDATE state_syncs"])
def test_sync_cancellation_failure_rolls_back_all_facts(environment, prefix):
    owned, _home = environment
    target_id = _start(owned)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    sync = _sync(owned, target_id)
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()

    outcome = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW),
        new_operation_key(), _fault_owned(owned, prefix),
    )

    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert _action(owned, target_id) == (2, 1, 0)
    assert _sync(owned, target_id) == sync
    assert owned.connection.execute(
        "SELECT status, cancellation_effect FROM cancel_items WHERE id = ?", (item_id,),
    ).fetchone() == (1, 1)
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == count


@pytest.mark.parametrize("after_commit", [False, True])
def test_unknown_sync_cancellation_verifies_original_complete_transaction(environment, after_commit):
    owned, home = environment
    target_id = _start(owned)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    command = ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW)
    key = new_operation_key()
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]

    unknown = repository.apply_cancel_target(
        command, key, _fault_owned(owned, "COMMIT", after_commit=after_commit),
    )
    assert unknown.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    recovered = open_existing(home / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert _action(recovered, target_id) == (2, 1, int(after_commit))
        assert recovered.connection.execute(
            "SELECT status, outcome FROM cancel_items WHERE id = ?", (item_id,),
        ).fetchone() == (2 if after_commit else 1, None)

        verified = repository.apply_cancel_target(command, key, recovered)

        assert verified.kind is DbOutcomeKind.COMPLETED, verified.error
        assert _action(recovered, target_id) == (2, 1, 1)
        assert _sync(recovered, target_id)[0] == 3
        assert recovered.connection.execute(
            "SELECT status, outcome FROM cancel_items WHERE id = ?", (item_id,),
        ).fetchone() == (2, None)
        assert recovered.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == count + 2
        assert recovered.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key = ?", (str(key),),
        ).fetchone() == (1,)
    finally:
        recovered.connection.close()


def test_local_cancel_finish_rolls_back_action_and_plan_then_reenters(environment):
    owned, _home = environment
    target_id = _start(owned)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    sync = _sync(owned, target_id)
    key = new_operation_key()

    failed = finish_canceled_sync_action(
        key, _fault_owned(owned, "UPDATE plans"), action_id=target_id, occurred_at=_NOW,
    )

    assert failed.kind is DbOutcomeKind.ROLLED_BACK
    assert _action(owned, target_id) == (2, 1, 1)
    assert owned.connection.execute(
        "SELECT status FROM plans WHERE id = (SELECT plan_id FROM actions WHERE id = ?)",
        (target_id,),
    ).fetchone() == (2,)
    saved = finish_canceled_sync_action(key, owned, action_id=target_id, occurred_at=_NOW)
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
    repeated = finish_canceled_sync_action(key, owned, action_id=target_id, occurred_at=_NOW)
    assert saved.kind is repeated.kind is DbOutcomeKind.COMPLETED
    assert _action(owned, target_id) == (6, 1, 1)
    assert _sync(owned, target_id) == sync
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == count


async def _applied_mode(owned, home, mode):
    if mode is CancelApplyMode.PRE_START:
        target_id = _register(owned, "1", {
            "name": "补齐状态", "type": "report_status", "params": {"scope": "full"},
        })
    else:
        target_id = _start(owned)
    request_id = "3"
    if mode is CancelApplyMode.ALREADY:
        _origin_id, first_item, repository = _fix_cancel(owned, target_id)
        first = repository.apply_cancel_target(
            ApplyCancelTarget(first_item, CancelApplyMode.WITH_STOP, _NOW),
            new_operation_key(), owned,
        )
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        request_id = "4"
    if mode is CancelApplyMode.TERMINAL:
        report = await _publish(owned, home)
        local = record_local_report(
            new_operation_key(), owned, action_id=target_id,
            local_report_id=report.report_id, occurred_at=_NOW,
        )
        assert local.kind is DbOutcomeKind.COMPLETED, local.error
    _origin_id, item_id, repository = _fix_cancel(owned, target_id, request_id)
    key = new_operation_key()
    command = ApplyCancelTarget(item_id, mode, _NOW)
    applied = repository.apply_cancel_target(
        command, key, owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    return target_id, repository, command, key


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(CancelApplyMode))
async def test_report_cancel_original_key_rejects_changed_apply_mode(environment, mode):
    owned, home = environment
    target_id, repository, command, key = await _applied_mode(owned, home, mode)
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
    sync = _sync(owned, target_id)
    changed_mode = (CancelApplyMode.ALREADY if mode is CancelApplyMode.WITH_STOP
                    else CancelApplyMode.WITH_STOP)

    changed = repository.apply_cancel_target(
        ApplyCancelTarget(command.item_id, changed_mode, _NOW), key, owned,
    )

    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    assert _sync(owned, target_id) == sync
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == count


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(CancelApplyMode))
async def test_report_cancel_original_mode_reuses_after_later_facts(environment, mode):
    owned, home = environment
    target_id, repository, command, key = await _applied_mode(owned, home, mode)
    if mode in (CancelApplyMode.WITH_STOP, CancelApplyMode.ALREADY):
        finished = finish_canceled_sync_action(
            new_operation_key(), owned, action_id=target_id, occurred_at=_NOW,
        )
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    elif mode is CancelApplyMode.TERMINAL:
        local_report_id = _sync(owned, target_id)[1]
        _process(owned, {"request_id": "1", "last_report_id": str(local_report_id)})
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
    action = _action(owned, target_id)
    sync = _sync(owned, target_id)

    repeated = repository.apply_cancel_target(command, key, owned)

    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert _action(owned, target_id) == action
    assert _sync(owned, target_id) == sync
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == count


@pytest.mark.asyncio
async def test_target_cancel_recovers_after_origin_stops_waiting(environment):
    owned, home = environment
    target_id = _start(owned)
    origin_id, item_id, repository = _fix_cancel(owned, target_id)
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    next_origin, next_item, _repository = _fix_cancel(owned, origin_id, "4")
    await apply_cancel(
        ApplyCancel(next_origin, (next_item,)), CancellationRuntime(
            owned=owned, repository=repository, settlement=_settlement(owned, repository),
            occurred_at=lambda: _NOW,
        ),
    )
    origin = _action(owned, origin_id)
    member = owned.connection.execute(
        "SELECT status, cancellation_effect, outcome FROM cancel_items WHERE id = ?", (item_id,),
    ).fetchone()
    assert origin == (6, 1, 1)
    assert member == (5, 2, None)
    assert _action(owned, target_id) == (2, 1, 1)
    assert query_work_facts(owned.connection).required_settlements > 0
    flow, context = _maintenance(home)

    await flow(context)

    assert _action(owned, target_id) == (6, 1, 1)
    assert _action(owned, origin_id) == origin
    assert owned.connection.execute(
        "SELECT status, cancellation_effect, outcome FROM cancel_items WHERE id = ?", (item_id,),
    ).fetchone() == member


@pytest.mark.asyncio
async def test_one_sync_cancel_preserves_shared_frozen_report_and_other_success(environment):
    owned, home = environment
    target_id, other_id = _start(owned), _start(owned, "2")
    frozen = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=_NOW)
    assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
    report = frozen.value.report
    other_sync = _sync(owned, other_id)
    origin_id, item_id, repository = _fix_cancel(owned, target_id)
    await apply_cancel(
        ApplyCancel(origin_id, (item_id,)), CancellationRuntime(
            owned=owned, repository=repository, settlement=_settlement(owned, repository),
            occurred_at=lambda: _NOW,
        ),
    )

    await _publish(owned, home, report)
    completed = record_local_report(
        new_operation_key(), owned, action_id=other_id,
        local_report_id=report.report_id, occurred_at=_NOW,
    )
    late = record_local_report(
        new_operation_key(), owned, action_id=target_id,
        local_report_id=report.report_id, occurred_at=_NOW,
    )

    assert completed.kind is DbOutcomeKind.COMPLETED, completed.error
    assert late.kind is DbOutcomeKind.ROLLED_BACK
    assert _action(owned, target_id) == (6, 1, 1)
    assert _sync(owned, target_id)[:3] == (3, None, None)
    assert _action(owned, other_id) == (3, 1, 0)
    assert _sync(owned, other_id) == (other_sync[0], report.report_id, other_sync[2], other_sync[3])
    assert owned.connection.execute(
        "SELECT status, from_wm, to_wm, frozen_event_id FROM reports WHERE id = ?",
        (report.report_id,),
    ).fetchone() == (4, report.from_wm, report.to_wm, report.boundary.last_event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_local_result_save_failure_stops_maintenance(environment, after_commit):
    owned, home = environment
    target_id = _start(owned)
    report = await _publish(owned, home)
    flow, context = _maintenance(home)
    actual_open = context.open_connection
    fault_prefix = "COMMIT" if after_commit else "UPDATE actions"
    context.open_connection = lambda: _fault_owned(actual_open(), fault_prefix, after_commit=after_commit)

    with pytest.raises(StateDbFailure):
        await flow(context)

    assert _action(owned, target_id) == ((3, 1, 0) if after_commit else (2, 1, 0))
    assert _sync(owned, target_id)[1] == (report.report_id if after_commit else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_actions", [False, True])
@pytest.mark.parametrize("prefix", ["SELECT id FROM actions WHERE type = 7", "SELECT s.action_id"])
async def test_report_state_query_failure_is_fatal_before_following_flow(environment, start_actions, prefix):
    owned, home = environment
    target_id = _start(owned)
    await _publish(owned, home)
    cfg = load_config({}, ConfigDefaults())
    flow = report_flow(
        state_db=home / "state.db", staging=home / "staging", ready=home / "ready",
        processing=home / "processing", history=cfg.history, database=cfg.database,
        supervisor=_FailedGeneration(), start_actions=start_actions,
    )
    context = SimpleNamespace(
        open_connection=lambda: _fault_owned(open_existing(
            home / "state.db", DbOpenMode.EXISTING_RW, DbConfig(),
        ), prefix), clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )

    with pytest.raises(StateDbFailure):
        await flow(context)

    followed = []

    async def device_flow(_context):
        followed.append(True)

    context.flows = {"report": flow, "device": device_flow}
    context.failure_log = None
    context.copy_request_factory = None
    fatal = await _drive_flows(context)
    assert fatal is not None
    assert followed == []
    assert _action(owned, target_id) == (2, 1, 0)
    assert _sync(owned, target_id)[1] is None


@pytest.mark.asyncio
async def test_cancel_finish_store_failure_stops_session_without_member_result(environment):
    owned, home = environment
    target_id = _start(owned)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    context = SimpleNamespace(
        open_connection=lambda: _fault_owned(open_existing(
            home / "state.db", DbOpenMode.EXISTING_RW, DbConfig(),
        ), "UPDATE plans"), clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )
    followed = []

    async def device_flow(_context):
        followed.append(True)

    context.flows = {
        "cancel": cancel_flow(ready=home / "ready", processing=home / "processing"),
        "device": device_flow,
    }

    fatal = await _drive_flows(context)

    assert fatal is not None
    assert followed == []
    assert _action(owned, target_id) == (2, 1, 1)
    assert owned.connection.execute(
        "SELECT status, outcome FROM cancel_items WHERE id = ?", (item_id,),
    ).fetchone() == (2, None)


@pytest.mark.parametrize("after_commit", [False, True])
def test_unknown_local_cancel_finish_recovers_original_key(environment, after_commit):
    owned, home = environment
    target_id = _start(owned)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    sync = _sync(owned, target_id)
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
    key = new_operation_key()

    unknown = finish_canceled_sync_action(
        key, _fault_owned(owned, "COMMIT", after_commit=after_commit),
        action_id=target_id, occurred_at=_NOW,
    )

    assert unknown.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    recovered = open_existing(home / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert _action(recovered, target_id) == (6 if after_commit else 2, 1, 1)
        assert recovered.connection.execute(
            "SELECT status, outcome FROM cancel_items WHERE id = ?", (item_id,),
        ).fetchone() == (2, None)
        verified = finish_canceled_sync_action(
            key, recovered, action_id=target_id, occurred_at=_NOW,
        )
        assert verified.kind is DbOutcomeKind.COMPLETED, verified.error
        assert _action(recovered, target_id) == (6, 1, 1)
        assert _sync(recovered, target_id) == sync
        assert recovered.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == count + 2
        assert recovered.connection.execute(
            "SELECT status FROM plans WHERE id = (SELECT plan_id FROM actions WHERE id = ?)",
            (target_id,),
        ).fetchone() == (3,)
    finally:
        recovered.connection.close()


@pytest.mark.asyncio
async def test_sync_cancel_history_replays_target_sync_member_and_plan(environment):
    owned, home = environment
    target_id = _start(owned)
    origin_id, item_id, repository = _fix_cancel(owned, target_id)
    history = HistoryRepository(home / "state.db")
    before = history.current_boundary()
    sync_id = owned.connection.execute(
        "SELECT id FROM state_syncs WHERE action_id = ?", (target_id,),
    ).fetchone()[0]
    plan_id = owned.connection.execute(
        "SELECT plan_id FROM actions WHERE id = ?", (target_id,),
    ).fetchone()[0]
    before_metadata = {
        (table, identity): row_facts(owned.connection, table, identity)
        for table, identity in (("actions", target_id), ("plans", plan_id),
                                ("state_syncs", sync_id), ("actions", origin_id))
    }
    objects = load_registry()["history_objects"]
    before_counts = {
        (name, identity): owned.connection.execute(
            "SELECT MAX(change_count) FROM entity_event_links WHERE entity_type = ? AND entity_id = ?",
            (objects[name]["id"], identity),
        ).fetchone()[0]
        for name, identity in (("action", target_id), ("plan", plan_id),
                               ("state_sync", sync_id), ("action", origin_id))
    }
    await apply_cancel(
        ApplyCancel(origin_id, (item_id,)), CancellationRuntime(
            owned=owned, repository=repository, settlement=_settlement(owned, repository),
            occurred_at=lambda: _NOW,
        ),
    )
    after = history.current_boundary()
    owners = {table: name for table, name in (
        ("actions", "action"), ("plans", "plan"), ("state_syncs", "state_sync"),
    )}
    events = []
    for stored in owned.connection.execute(
        "SELECT id, transaction_id, event_type, event_version, occurred_at,"
        " clock_status, change_seq, body_json FROM history_events ORDER BY id",
    ).fetchall():
        envelope = decode_event_row(stored)
        row_owners = {}
        for change in envelope.rows:
            name = owners.get(change.table)
            if name is not None:
                row_owners[(change.table, change.row_id)] = (objects[name]["id"], change.row_id)
            elif change.table == "cancel_items":
                item = row_facts(owned.connection, change.table, change.row_id)
                row_owners[(change.table, change.row_id)] = (objects["action"]["id"], item["action_id"])
        references = tuple(owned.connection.execute(
            "SELECT entity_type, entity_id FROM entity_event_links WHERE event_id = ?",
            (envelope.event_id,),
        ).fetchall())
        events.append(ValidatedEvent(
            envelope, event_type_name(envelope.event_type),
            branch_of(envelope.event_type, json.loads(stored[7])["reason"])[0],
            references, row_owners,
        ))
    expected_rows = {
        ("action", target_id): [("actions", target_id)],
        ("state_sync", sync_id): [("state_syncs", sync_id)],
        ("action", origin_id): [("actions", origin_id), ("cancel_items", item_id)],
        ("plan", plan_id): [("plans", plan_id)],
    }
    count_changes = {("actions", target_id): 2, ("state_syncs", sync_id): 1,
                     ("actions", origin_id): 2, ("plans", plan_id): 1}
    for (name, identity), rows in expected_rows.items():
        initial = restore(RestoreSeed(EntityImage(
            objects[name]["id"], identity, False, {}, 0, 0,
        ), INITIAL_BOUNDARY), events, after)
        expected = {}
        for table, row_id in rows:
            facts = row_facts(owned.connection, table, row_id)
            expected[(table, row_id)] = {column: facts[column] for column in business_columns(table)}
        assert initial.rows == expected
        restored = history.restore_entity(name, identity, after, event_batch_size=1)
        assert {key: {column: facts[column] for column in business_columns(key[0])}
                for key, facts in restored.items()} == expected
        table = objects[name]["table"]
        primary = restored[(table, identity)]
        original = before_metadata[(table, identity)]
        declared_metadata = load_event_registry()["tables"][table]["derived"]
        assert primary["id"] == identity
        if "created_event_id" in declared_metadata:
            assert primary["created_event_id"] == original["created_event_id"]
        if "change_count" in declared_metadata:
            assert primary["change_count"] == original["change_count"] + count_changes[(table, identity)]
        assert initial.change_count == before_counts[(name, identity)] + count_changes[(table, identity)]
    original_action = history.restore_entity("action", target_id, before, event_batch_size=1)
    assert (original_action[("actions", target_id)]["status"],
            original_action[("actions", target_id)]["cancel_requested"]) == (2, 0)
    assert original_action[("actions", target_id)]["last_event_id"] == before_metadata[("actions", target_id)]["last_event_id"]
    assert original_action[("actions", target_id)]["change_count"] == before_metadata[("actions", target_id)]["change_count"]
    original_sync = history.restore_entity("state_sync", sync_id, before, event_batch_size=1)
    assert (original_sync[("state_syncs", sync_id)]["status"],
            original_sync[("state_syncs", sync_id)]["ended_event_id"]) == (1, None)
    original_member = history.restore_entity("action", origin_id, before, event_batch_size=1)
    assert (original_member[("cancel_items", item_id)]["status"],
            original_member[("cancel_items", item_id)]["cancellation_effect"]) == (1, 1)


def test_running_unstarted_capture_original_key_keeps_exact_mode(environment):
    owned, _home = environment
    target_id = _register(owned, "1", {
        "name": "拍照", "type": "camera_take_photo", "device_id": "cam-1",
        "scheduled_at": "2025-06-15 15:06:40", "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 1000},
    })
    started = SchedulingRepository().start_action(
        StartActionRequest(target_id, _NOW, _NOW), new_operation_key(), owned,
    )
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    assert _action(owned, target_id) == (2, 1, 0)
    _origin_id, item_id, repository = _fix_cancel(owned, target_id)
    key = new_operation_key()
    command = ApplyCancelTarget(item_id, CancelApplyMode.PRE_START, _NOW)
    first = repository.apply_cancel_target(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert _action(owned, target_id) == (2, 1, 1)
    count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()

    changed = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), key, owned,
    )

    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    legal = repository.apply_cancel_target(command, key, owned)
    assert legal.kind is DbOutcomeKind.COMPLETED, legal.error
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == count


@pytest.mark.parametrize("invalid", ["missing", "wrong_item", "wrong_mode", "wrong_time", "extra_field"])
def test_cancel_guard_rejects_invalid_original_input_evidence(environment, invalid):
    owned, _home = environment
    target_id = _start(owned)
    _origin_id, item_id, _repository = _fix_cancel(owned, target_id)
    key = new_operation_key()
    request = {"item_id": item_id, "mode": "with_stop", "occurred_at": _NOW}
    if invalid == "wrong_item":
        request["item_id"] += 100
    elif invalid == "wrong_mode":
        request["mode"] = "already"
    elif invalid == "wrong_time":
        request["occurred_at"] += 1
    elif invalid == "extra_field":
        request["unexpected"] = True
    evidence = {} if invalid == "missing" else {"cancel_request": request}
    actual = _ApplyCancelTargetCommand(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW), key)

    class EvidenceFault:
        """在正式事务校验边界替换申请依据；实际状态变化由生产命令形成。"""

        def plan(self, scope):
            plan = actual.plan(scope)
            return replace(plan, events=(replace(plan.events[0], evidence=evidence), *plan.events[1:]))

    receipt = commit_operation(EvidenceFault(), key, owned)

    assert receipt.kind == "rolled_back"
    assert _action(owned, target_id) == (2, 1, 0)
    assert _sync(owned, target_id)[0] == 1
