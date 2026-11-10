"""obtain CHECK 的真实归属、配置分区与累计尝试；不调用 QUERY 端口。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, BeginDisposition,
    OperationKind,
)
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue, SettlementBasis
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.persistence.repositories.file_batches import FileBatchRepository

from .test_attempts import _NOW, _outcome, _validated
from ..outputs.test_sources import _seed_environment, _seed_plan, _seed_action, _seed_output
from ..support.output_planning import EventSequence, ScopedEventSequence, capture_operation


def _environment(tmp_path, *, host=True):
    path, owned = _seed_environment(tmp_path)
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 1, 1, action_type=1, status=3 if not host else 2)
    _seed_action(connection, 2, 1, action_type=4, source_resolution_state=1)
    if not host:
        connection.execute("UPDATE actions SET source_resolution_state=2,resolved_source_plan_id=1 WHERE id=2")
    connection.execute("UPDATE actions SET input_fields_json=?,execution_spec_json=? WHERE id=2",
        ('{"params":{"source":{"action_instance_id":"1"},"purpose":"manual","output_ids":["1","2"]}}',
         '{"selection_mode":3}'))
    _seed_output(connection, 1, 1, 2 if host else 1, availability=5)
    connection.execute("INSERT INTO action_dependencies VALUES (1,2,1)")
    connection.execute("INSERT INTO obtain_source_selections VALUES (1,1,?,NULL,NULL)", (2 if not host else 1,))
    connection.execute(
        "INSERT INTO obtain_items (id,selection_id,requested_output_id,output_id,basis,"
        "status,source_dependency) VALUES (1,1,1,NULL,5,1,0)")
    connection.commit()
    if not host:
        batch = FileBatchRepository().acquire(BatchRequest(2, "cam-1", _NOW), new_operation_key(), owned)
        assert batch.kind is DbOutcomeKind.COMPLETED and batch.value is BatchOutcome.GRANTED, batch
    return path, owned


def _intent(*, host=True, **changes):
    intent = AttemptIntent(
        operation="query", action_id=2, kind=OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(obtain_item_id=1), query_purpose=None,
        config=AttemptConfig(3, None if host else Decimal("3"), None if host else Decimal("0")),
        occurred_at=_NOW)
    return replace(intent, **changes)


def _finish(owned, ticket, *, retry_wait=False, not_dispatched=False):
    result = _validated(ticket, _outcome(
        status=AttemptStatus.FAILED, error=ErrorValue(code="transport_timeout", stage="transport"),
        effect=EffectState.NO_EFFECT if not_dispatched else EffectState.UNKNOWN,
        basis=SettlementBasis.NOT_DISPATCHED if not_dispatched else SettlementBasis.OBSERVED,
        evidence_type="dispatch_prevented" if not_dispatched else "query_returned"))
    return OperationRepository().finish_attempt(
        AttemptFinish(ticket, result, _NOW + 1, retry_wait=retry_wait), new_operation_key(), owned)


@pytest.mark.parametrize("not_dispatched", [False, True])
def test_host_followup_uses_actual_unresolved_item_and_complete_result(tmp_path, not_dispatched):
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        ticket = first.value.ticket
        assert ticket.responsibility_key == "obtain-exists/1" and ticket.target_id == "1"
        assert _finish(owned, ticket, not_dispatched=not_dispatched).kind is DbOutcomeKind.COMPLETED
        run = owned.connection.execute("SELECT status,retry_wait_required,timeout_s_json,"
            "retry_interval_s_json,obtain_item_id,cleanup_item_id FROM operation_runs").fetchone()
        assert tuple(run) == (2, 0, None, None, 1, None)
        second = repository.begin_attempt(_intent(occurred_at=_NOW + 2), new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        assert second.value.ticket.run_id == ticket.run_id
        assert second.value.ticket.attempt_id == 2
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs").fetchone()[0] == 2
    finally:
        owned.connection.close()


def _unchanged(owned, operation):
    before = tuple(owned.connection.iterdump())
    value = operation()
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction
    return value


@pytest.mark.parametrize("field,value", [
    ("occurred_at", _NOW + 1),
    ("config", AttemptConfig(4)),
    ("config", AttemptConfig(3, Decimal("1"), None)),
    ("config", AttemptConfig(3, None, Decimal("0"))),
    ("target", AttemptTarget(obtain_item_id=2)),
])
def test_original_key_rejects_changed_complete_input(tmp_path, field, value):
    _, owned = _environment(tmp_path)
    try:
        owned.connection.execute("INSERT INTO obtain_items (id,selection_id,requested_output_id,"
            "output_id,basis,status,source_dependency) VALUES (2,1,2,NULL,5,1,0)")
        _seed_output(owned.connection, 2, 1, 2, availability=5)
        owned.connection.commit()
        repository, key = OperationRepository(), new_operation_key()
        first = repository.begin_attempt(_intent(), key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        refused = _unchanged(owned, lambda: repository.begin_attempt(
            replace(_intent(), **{field:value}), key, owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK, refused.error
    finally:
        owned.connection.close()


def test_running_original_ticket_cannot_be_hidden_by_new_key(tmp_path):
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED
        second = _unchanged(owned, lambda: repository.begin_attempt(
            _intent(occurred_at=_NOW + 1), new_operation_key(), owned))
        assert second.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


@pytest.mark.parametrize("host", [True, False])
def test_actual_carrier_rejects_other_configuration_partition(tmp_path, host):
    legal_path = tmp_path / "legal"
    legal_path.mkdir()
    _, legal = _environment(legal_path, host=host)
    try:
        accepted = OperationRepository().begin_attempt(_intent(host=host), new_operation_key(), legal)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        assert accepted.value.disposition is BeginDisposition.GRANTED
    finally:
        legal.connection.close()
    _, owned = _environment(tmp_path, host=host)
    try:
        refused = _unchanged(owned, lambda: OperationRepository().begin_attempt(
            _intent(host=not host), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(refused.error, ConsistencyError)
        assert "配置必须符合真实主机或设备载体分区" in str(refused.error)
    finally:
        owned.connection.close()


def test_device_zero_interval_still_requires_persisted_retry_wait(tmp_path):
    _, owned = _environment(tmp_path, host=False)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(host=False), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        refused = _unchanged(owned, lambda: repository.begin_attempt(
            _intent(host=False, occurred_at=_NOW + 2), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK
        # 另一个真实世界在 Finish 同组建立 RETRY_WAIT；间隔 0 仍须此责任。
    finally:
        owned.connection.close()


def test_device_zero_interval_with_retry_wait_uses_same_run(tmp_path):
    _, owned = _environment(tmp_path, host=False)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(host=False), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert _finish(owned, first.value.ticket, retry_wait=True).kind is DbOutcomeKind.COMPLETED
        assert owned.connection.execute("SELECT retry_wait_required,retry_interval_s_json FROM operation_runs").fetchone() == (1, '0')
        second = repository.begin_attempt(_intent(host=False, occurred_at=_NOW + 2), new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        assert (second.value.ticket.run_id, second.value.ticket.attempt_id) == (first.value.ticket.run_id, 2)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("terminal", [False, True])
def test_budget_lowering_and_terminal_run_preserve_original_history(tmp_path, terminal):
    from camctl.operations.attempts import StaleRunFinish, RunOutcome
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        if terminal:
            closed = repository.finish_stale_runs(StaleRunFinish(
                (first.value.ticket.responsibility_key,), RunOutcome.CANCELED, _NOW + 2),
                new_operation_key(), owned)
            assert closed.kind is DbOutcomeKind.COMPLETED, closed.error
        refused = _unchanged(owned, lambda: repository.begin_attempt(
            _intent(config=AttemptConfig(4 if terminal else 1), occurred_at=_NOW + 3), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.COMPLETED, refused.error
        assert refused.value.disposition is BeginDisposition.REJECTED
        assert refused.value.reason == ("run_ended" if terminal else "budget_exhausted")
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mutation", ["foreign_action", "foreign_source", "missing_output", "wrong_carrier_owner"])
def test_actual_fixed_chain_rejects_forgery_without_intent(tmp_path, mutation):
    _, owned = _environment(tmp_path)
    try:
        if mutation == "foreign_action":
            _seed_action(owned.connection, 3, 1, action_type=4)
            owned.connection.execute("UPDATE action_dependencies SET action_id=3")
        elif mutation == "foreign_source":
            _seed_action(owned.connection, 3, 1, action_type=1)
            owned.connection.execute("UPDATE action_dependencies SET depends_on_action_id=3")
        elif mutation == "missing_output":
            owned.connection.execute("UPDATE obtain_items SET requested_output_id=999")
        else:
            _seed_action(owned.connection, 3, 1, action_type=1)
            owned.connection.execute("UPDATE intermediate_files SET owner_action_id=3")
        owned.connection.commit()
        refused = _unchanged(owned, lambda: OperationRepository().begin_attempt(_intent(), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK, refused.error
    finally:
        owned.connection.close()


def test_same_numeric_cleanup_and_obtain_owners_are_independent(tmp_path):
    _, owned = _environment(tmp_path)
    try:
        owned.connection.execute("INSERT INTO cleanup_items (id,action_id,requested_output_id,status,restriction_state) VALUES (1,2,1,1,1)")
        owned.connection.commit()
        repository = OperationRepository()
        cleanup = replace(_intent(), target=AttemptTarget(cleanup_item_id=1))
        first = repository.begin_attempt(cleanup, new_operation_key(), owned)
        second = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert first.kind is second.kind is DbOutcomeKind.COMPLETED
        assert first.value.ticket.run_id != second.value.ticket.run_id
        assert (first.value.ticket.responsibility_key, second.value.ticket.responsibility_key) == ('exists/1', 'obtain-exists/1')
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        third = repository.begin_attempt(replace(cleanup, occurred_at=_NOW+2), new_operation_key(), owned)
        assert third.kind is DbOutcomeKind.COMPLETED
        assert third.value.ticket.attempt_id == 2
        # 相同数值的 ticket target 也不能互换责任键和 run。
        ticket = second.value.ticket
        bad = replace(ticket, responsibility_key=first.value.ticket.responsibility_key)
        from camctl.persistence.repositories.transactions import TransactionError
        refused = _unchanged(owned, lambda: repository.finish_attempt(
            _finish_value(bad), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(refused.error, TransactionError)
    finally:
        owned.connection.close()


def _finish_value(ticket):
    result = _validated(ticket, _outcome(status=AttemptStatus.FAILED,
        error=ErrorValue(code="transport_timeout", stage="transport"), effect=EffectState.UNKNOWN,
        basis=SettlementBasis.OBSERVED, evidence_type="query_returned"))
    return AttemptFinish(ticket, result, _NOW + 1)


def test_original_commit_unknown_fresh_error_then_original_key_recovers_complete_history(tmp_path):
    import sqlite3
    from .test_result_reuse import _FaultConnection
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    from camctl.persistence.repositories.history import HistoryRepository
    path, owned = _environment(tmp_path)
    try:
        repository, begin_key, finish_key = OperationRepository(), new_operation_key(), new_operation_key()
        first = repository.begin_attempt(_intent(), begin_key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        finish = _finish_value(first.value.ticket)
        class LostReceipt(_FaultConnection):
            def execute(self, sql, parameters=()):
                result = self._connection.execute(sql, parameters)
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("lost receipt after actual COMMIT")
                return result
        unknown = repository.finish_attempt(finish, finish_key,
            replace(owned, connection=LostReceipt(owned.connection, 'unused')))
        assert unknown.kind is DbOutcomeKind.UNKNOWN
        history = HistoryRepository(path)
        boundary = history.current_boundary()
        frozen = history.restore_entity('action', 2, boundary)
        before = tuple(owned.connection.iterdump())
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        failed = repository.finish_attempt(finish, finish_key, replace(owned,
            connection=_FaultConnection(owned.connection, 'SELECT')))
        assert failed.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(failed.error, sqlite3.OperationalError)
        assert tuple(owned.connection.iterdump()) == before
        repeated = _unchanged(owned, lambda: OperationRepository().begin_attempt(_intent(), begin_key, owned))
        assert repeated.kind is DbOutcomeKind.COMPLETED and repeated.value.ticket == first.value.ticket
        saved = _unchanged(owned, lambda: OperationRepository().finish_attempt(finish, finish_key, owned))
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        second = repository.begin_attempt(_intent(occurred_at=_NOW+2), new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        assert HistoryRepository(path).restore_entity('action', 2, boundary) == frozen
        assert frozen[('operation_runs', first.value.ticket.run_id)]['obtain_item_id'] == 1
        assert frozen[('operation_attempts', 1)]['result_json']['observations'] == []
        # 同发行物的新 Python 进程独立打开 SQL/事件资源并恢复同一完整 H。
        import subprocess, sys, json
        source = '''import json,sys
from pathlib import Path
from camctl.contracts.history_values import HistoryBoundary
from camctl.persistence.repositories.history import HistoryRepository
rows=HistoryRepository(Path(sys.argv[1])).restore_entity('action',2,HistoryBoundary(int(sys.argv[2]),int(sys.argv[3])))
print(json.dumps(sorted((t,i,v) for (t,i),v in rows.items()),sort_keys=True))
'''
        actual = subprocess.check_output([sys.executable, '-c', source, str(path), str(boundary.txn_id), str(boundary.last_event_id)], text=True)
        assert json.loads(actual) == json.loads(json.dumps(sorted((t,i,v) for (t,i),v in frozen.items()), sort_keys=True))
    finally:
        owned.connection.close()


def _mutate_identity_proposal(plan, mutation):
    """改变正式仓储实际提交的事件或测试提供的当前事实。"""
    from copy import deepcopy
    event, = plan.events
    attempt, run = event.rows
    state = deepcopy(plan.state_rows)
    changed = dict(run.after.values)
    if mutation == 'key_cleanup':
        changed['responsibility_key'] = 'exists/1'
    elif mutation == 'owner_cleanup':
        changed.update(cleanup_item_id=1, obtain_item_id=None)
    elif mutation == 'both_owners':
        changed['cleanup_item_id'] = 1
    elif mutation == 'no_owner':
        changed['obtain_item_id'] = None
    elif mutation == 'wrong_kind':
        changed.update(kind=4, responsibility_key='delete/1')
    elif mutation == 'missing_fk':
        changed.update(obtain_item_id=999, responsibility_key='obtain-exists/999')
    elif mutation == 'foreign_action':
        changed['action_id'] = 1
    elif mutation == 'run_config':
        changed['timeout_s_json'] = Decimal('1')
    elif mutation == 'attempt_config':
        attempt = replace(attempt, after=replace(attempt.after,
            values={**attempt.after.values, 'timeout_s_json':Decimal('1')}))
    elif mutation in ('missing_carrier', 'double_carrier'):
        state['outputs'][1]['intermediate_file_id'] = None if mutation == 'missing_carrier' else 901
        if mutation == 'double_carrier':
            state['outputs'][1]['device_file_id'] = 901
    elif mutation == 'valid':
        pass
    else:
        raise AssertionError(mutation)
    run = replace(run, after=replace(run.after, values=changed))
    event = replace(event, rows=(attempt, run))
    return replace(plan, events=(event,), state_rows=state)

@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
@pytest.mark.parametrize('mutation', ['key_cleanup','owner_cleanup','both_owners','no_owner',
    'wrong_kind','missing_fk','foreign_action','run_config','attempt_config','missing_carrier','double_carrier'])
def test_registered_guards_reject_forged_identity_in_complete_transaction(tmp_path, bounded, mutation):
    import sqlite3
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        owned.connection.execute("INSERT INTO cleanup_items (id,action_id,requested_output_id,status,restriction_state) VALUES (1,2,1,1,1)")
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(), key)
        with sqlite3.connect(':memory:') as baseline:
            owned.connection.backup(baseline)
            accepted = commit_operation(_proposal_command(plan, bounded), key, owned)
            assert accepted.kind == 'completed', accepted.error
            baseline.backup(owned.connection)
        receipt = commit_operation(_proposal_command(_mutate_identity_proposal(plan, mutation), bounded), key, owned)
        assert receipt.kind == 'rolled_back', (mutation, receipt.error)
        assert isinstance(receipt.error, EventValidationError), receipt.error
        expected = {'key_cleanup': '流程责任键与固定身份不符',
            'owner_cleanup': '流程责任键与固定身份不符',
            'both_owners': '普通流程的固定身份字段无效或缺失',
            'no_owner': '普通流程的固定身份字段无效或缺失',
            'wrong_kind': '普通流程的固定身份字段无效或缺失',
            'missing_fk': '关联记录不存在: obtain_items#999',
            'foreign_action': '历史归属',
            'run_config': '配置必须符合真实主机或设备载体分区',
            'attempt_config': '意图与实际流程采用配置不同',
            'missing_carrier': '产物必须恰有一种实际文件载体',
            'double_carrier': '产物必须恰有一种实际文件载体'}
        assert expected[mutation] in str(receipt.error)
        assert tuple(owned.connection.iterdump()) == before
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
def test_registered_guards_accept_legal_owner_in_complete_transaction(tmp_path, bounded):
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        key = new_operation_key()
        receipt = commit_operation(_proposal_command(_capture_plan(owned, _intent(), key), bounded), key, owned)
        assert receipt.kind == 'completed', receipt.error
    finally:
        owned.connection.close()


@pytest.mark.parametrize('mutation', ['both_owners','no_owner','wrong_kind','missing_fk',
    'duplicate_owner','key_cleanup','key_obtain_on_cleanup','other_target'])
def test_current_sql_owner_partition_fk_key_and_uniqueness_are_atomic(tmp_path, mutation):
    import sqlite3
    _, owned = _environment(tmp_path)
    try:
        connection = owned.connection
        connection.execute("INSERT INTO cleanup_items (id,action_id,requested_output_id,status,restriction_state) VALUES (1,2,1,1,1)")
        connection.commit()
        before = tuple(connection.iterdump())
        run = dict(action_id=2, kind=5, responsibility_key='obtain-exists/1', obtain_item_id=1,
            cleanup_item_id=None, status=2, attempts_used=0, max_attempts_used=3, retry_wait_required=0)
        if mutation == 'both_owners':
            run['cleanup_item_id'] = 1
        elif mutation == 'no_owner':
            run['obtain_item_id'] = None
        elif mutation == 'wrong_kind':
            run['kind'] = 4
        elif mutation == 'missing_fk':
            run.update(obtain_item_id=999, responsibility_key='obtain-exists/999')
        elif mutation == 'key_cleanup':
            run['responsibility_key'] = 'exists/1'
        elif mutation == 'key_obtain_on_cleanup':
            run.update(obtain_item_id=None, cleanup_item_id=1)
        elif mutation == 'other_target':
            run['copy_id'] = 1
        columns = ','.join(run)
        sql = f'INSERT INTO operation_runs ({columns}) VALUES ({",".join("?" for _ in run)})'
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, tuple(run.values()))
            if mutation == 'duplicate_owner':
                connection.execute(sql, tuple(run.values()))
            connection.commit()
        connection.rollback()
        assert tuple(connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('result', [None, {}], ids=['missing-result', 'incomplete-result'])
def test_host_followup_rejects_unreadable_previous_result_without_new_facts(tmp_path, result):
    """失败行允许缺正文；直接 API 仍须可靠恢复完整结果才能签发下一票。"""
    import json
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        owned.connection.execute('UPDATE operation_attempts SET result_json=?',
            (None if result is None else json.dumps(result),))
        owned.connection.commit()
        refused = _unchanged(owned, lambda: repository.begin_attempt(
            _intent(occurred_at=_NOW + 2), new_operation_key(), owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


def _mutate_followup_proposal(plan, mutation):
    from copy import deepcopy
    from camctl.history.events import RowChange, RowImage
    event, = plan.events
    state = deepcopy(plan.state_rows)
    attempt, run = event.rows
    if mutation in ('missing_result', 'missing_result_event', 'incomplete_result'):
        previous, = state['operation_attempts'].values()
        previous['result_event_id' if mutation == 'missing_result_event' else 'result_json'] = (
            {} if mutation == 'incomplete_result' else None)
    elif mutation in ('owner', 'action', 'key'):
        column, value = {'owner': ('obtain_item_id', 2), 'action': ('action_id', 1),
                         'key': ('responsibility_key', 'obtain-exists/2')}[mutation]
        original = state['operation_runs'][run.row_id][column]
        run = replace(run, before=replace(run.before, values={**run.before.values, column: original}),
                      after=replace(run.after, values={**run.after.values, column: value}))
        event = replace(event, rows=(attempt, run))
    elif mutation == 'saved_attempt_config':
        previous, = state['operation_attempts'].values()
        old_attempt = RowChange('operation_attempts', previous['id'],
            RowImage(True, {'timeout_s_json': None}), RowImage(True, {'timeout_s_json': Decimal('1')}))
        event = replace(event, rows=(*event.rows, old_attempt))
    elif mutation != 'valid':
        raise AssertionError(mutation)
    plan = replace(plan, events=(event,), state_rows=state)
    return plan

@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
@pytest.mark.parametrize('mutation', ['missing_result', 'missing_result_event', 'incomplete_result',
    'owner', 'action', 'key', 'saved_attempt_config', 'valid'])
def test_followup_registered_guards_preserve_previous_receipt_and_identity(tmp_path, bounded, mutation):
    import sqlite3
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.persistence.repositories.transactions import commit_operation
    path, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        history = HistoryRepository(path)
        boundary = history.current_boundary()
        frozen = history.restore_entity('action', 2, boundary)
        before = tuple(owned.connection.iterdump())
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(occurred_at=_NOW + 2), key)
        if mutation != 'valid':
            with sqlite3.connect(':memory:') as baseline:
                owned.connection.backup(baseline)
                accepted = commit_operation(_proposal_command(plan, bounded), key, owned)
                assert accepted.kind == 'completed', accepted.error
                baseline.backup(owned.connection)
        receipt = commit_operation(_proposal_command(_mutate_followup_proposal(plan, mutation), bounded), key, owned)
        if mutation == 'valid':
            assert receipt.kind == 'completed', receipt.error
            assert owned.connection.execute('SELECT attempts_used FROM operation_runs').fetchone() == (2,)
        else:
            assert receipt.kind == 'rolled_back', (mutation, receipt.error)
            assert isinstance(receipt.error, EventValidationError), receipt.error
            expected = {'missing_result': '缺少上一票的完整可靠结果',
                'missing_result_event': '缺少上一票的完整可靠结果',
                'incomplete_result': '上一票的实际结果不可解释',
                'owner': '更新列', 'action': '更新列', 'key': '更新列',
                'saved_attempt_config': '不匹配 ATTEMPT_STARTED.NEW 声明的任何行规格'}
            assert expected[mutation] in str(receipt.error)
            assert tuple(owned.connection.iterdump()) == before
        assert not owned.connection.in_transaction
        assert HistoryRepository(path).restore_entity('action', 2, boundary) == frozen
    finally:
        owned.connection.close()


@pytest.mark.parametrize('host', [True, False])
def test_new_configuration_updates_only_new_attempt_and_old_key_keeps_adopted_input(tmp_path, host):
    from camctl.persistence.repositories.history import HistoryRepository
    path, owned = _environment(tmp_path, host=host)
    try:
        repository, key = OperationRepository(), new_operation_key()
        original_intent = _intent(host=host)
        first = repository.begin_attempt(original_intent, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert _finish(owned, first.value.ticket, retry_wait=not host).kind is DbOutcomeKind.COMPLETED
        original = owned.connection.execute('SELECT * FROM operation_attempts').fetchone()
        history = HistoryRepository(path)
        boundary = history.current_boundary()
        frozen = history.restore_entity('action', 2, boundary)
        new_config = AttemptConfig(4, None if host else Decimal('7'), None if host else Decimal('2'))
        second = repository.begin_attempt(_intent(host=host, config=new_config, occurred_at=_NOW + 2),
            new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        assert second.value.ticket.run_id == first.value.ticket.run_id
        assert owned.connection.execute('SELECT * FROM operation_attempts WHERE attempt_no=1').fetchone() == original
        assert owned.connection.execute('SELECT max_attempts_used,timeout_s_json,retry_interval_s_json '
            'FROM operation_attempts WHERE attempt_no=2').fetchone() == (4, None if host else '7', None if host else '2')
        repeated = _unchanged(owned, lambda: repository.begin_attempt(original_intent, key, owned))
        assert repeated.kind is DbOutcomeKind.COMPLETED and repeated.value == first.value
        refused = _unchanged(owned, lambda: repository.begin_attempt(
            replace(original_intent, config=new_config), key, owned))
        assert refused.kind is DbOutcomeKind.ROLLED_BACK
        assert HistoryRepository(path).restore_entity('action', 2, boundary) == frozen
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
def test_actual_running_previous_ticket_cannot_borrow_context_completion(tmp_path, bounded):
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        original_key = new_operation_key()
        first = OperationRepository().begin_attempt(_intent(), original_key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        from contextlib import closing
        import sqlite3
        from camctl.persistence.runtime import OwnedConnection
        from camctl.history.validators import EventValidationError
        key = new_operation_key()
        with closing(sqlite3.connect(':memory:')) as connection:
            owned.connection.backup(connection)
            clone = OwnedConnection(connection, owned.metadata)
            finished = _finish(clone, first.value.ticket)
            assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
            plan = _capture_plan(clone, _intent(occurred_at=_NOW + 2), key)
            accepted = commit_operation(_proposal_command(plan, bounded), key, clone)
            assert accepted.kind == 'completed', accepted.error
        next_id, = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()
        event, = plan.events
        rows = tuple(replace(row, after=replace(row.after,
            values={**row.after.values, 'intent_event_id': next_id}))
            if row.table == 'operation_attempts' else row for row in event.rows)
        plan = replace(plan, events=(replace(event, rows=rows),))
        receipt = _unchanged(owned, lambda: commit_operation(_proposal_command(plan, bounded), key, owned))
        assert receipt.kind == 'rolled_back', receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert '缺少上一票的完整可靠结果' in str(receipt.error)
        assert owned.connection.execute('SELECT attempts_used FROM operation_runs').fetchone() == (1,)
    finally:
        owned.connection.close()


def _capture_plan(owned, intent, key):
    return capture_operation(owned, lambda target: OperationRepository().begin_attempt(intent, key, target))


def _proposal_command(saved, bounded, *, caller_obtain_check_reads=None):
    if not bounded:
        assert caller_obtain_check_reads is None
        return EventSequence(saved.events, saved)
    from copy import deepcopy
    return ScopedEventSequence(saved.events, saved, lambda _event: None,
        state_transform=lambda _state, _event: deepcopy(saved.state_rows),
        caller_obtain_check_reads=caller_obtain_check_reads)


def _assert_legal_proposal(owned, plan, bounded, key):
    import sqlite3
    from camctl.persistence.repositories.transactions import commit_operation
    with sqlite3.connect(':memory:') as baseline:
        owned.connection.backup(baseline)
        accepted = commit_operation(_proposal_command(plan, bounded), key, owned)
        assert accepted.kind == 'completed', accepted.error
        baseline.backup(owned.connection)


@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
@pytest.mark.parametrize('damage', ['intent_reference', 'missing_event', 'other_attempt',
    'body_mismatch', 'incomplete_range', 'read_error'])
def test_followup_guard_reads_actual_previous_result_event(tmp_path, bounded, damage):
    import json, sqlite3
    from ..operations.test_result_reuse import _FaultConnection
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        first = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(occurred_at=_NOW + 2), key)
        _assert_legal_proposal(owned, plan, bounded, key)
        connection = owned.connection
        event_id = connection.execute('SELECT result_event_id FROM operation_attempts').fetchone()[0]
        if damage == 'intent_reference':
            connection.execute('UPDATE operation_attempts SET result_event_id=intent_event_id')
        elif damage == 'missing_event':
            connection.execute('UPDATE operation_attempts SET result_event_id=999999')
        elif damage in ('other_attempt', 'body_mismatch'):
            raw = parse_exact_json(connection.execute('SELECT body_json FROM history_events WHERE id=?',
                (event_id,)).fetchone()[0])
            row = next(row for row in raw['rows'] if row['table'] == 'operation_attempts')
            if damage == 'other_attempt': row['id'] = 999
            else: row['after']['values']['result_json']['observations'] = [{'type':'unexpected','version':1,'data':{}}]
            connection.execute('UPDATE history_events SET body_json=? WHERE id=?', (json.dumps(raw), event_id))
        elif damage == 'incomplete_range':
            connection.execute('UPDATE history_transactions SET last_event_id=last_event_id+1'
                               ' WHERE id=(SELECT transaction_id FROM history_events WHERE id=?)', (event_id,))
        connection.commit()
        class ReadError(_FaultConnection):
            error = sqlite3.OperationalError('actual result read failed')

            def execute(self, sql, parameters=()):
                if sql.startswith('SELECT id,transaction_id,event_type,event_version,'):
                    raise self.error
                return self._connection.execute(sql, parameters)
        target = replace(owned, connection=ReadError(connection, 'unused')) if damage == 'read_error' else owned
        receipt = _unchanged(owned, lambda: commit_operation(_proposal_command(plan, bounded), key, target))
        assert receipt.kind == 'rolled_back', receipt.error
        from camctl.history.validators import EventValidationError
        if damage == 'read_error':
            assert receipt.error is target.connection.error
        elif damage == 'incomplete_range':
            assert isinstance(receipt.error, ConsistencyError)
            assert '成员与首尾范围不一致' in str(receipt.error)
        else:
            assert isinstance(receipt.error, EventValidationError), receipt.error
            expected = {'intent_reference': '与实际已提交的完整结果事件不同',
                'missing_event': '前票的实际结果事件缺失',
                'other_attempt': '与实际已提交的完整结果事件不同',
                'body_mismatch': '实际结果正文与固定行不同'}
            assert expected[damage] in str(receipt.error)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
def test_actual_device_carrier_cannot_borrow_host_context(tmp_path, bounded):
    from copy import deepcopy
    import sqlite3
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.transactions import commit_operation, row_facts
    _, owned = _environment(tmp_path, host=False)
    try:
        # 合法 host 文件仅供虚构上下文引用；实际 requested output 仍由 device 承载。
        _seed_output(owned.connection, 2, 1, 2, availability=5)
        owned.connection.commit()
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(host=False), key)
        with sqlite3.connect(':memory:') as baseline:
            owned.connection.backup(baseline)
            legal = commit_operation(_proposal_command(plan, bounded), key, owned)
            assert legal.kind == 'completed', legal.error
            baseline.backup(owned.connection)
        event, = plan.events
        state = deepcopy(plan.state_rows)
        host = row_facts(owned.connection, 'outputs', 2)['intermediate_file_id']
        state['outputs'][1].update(device_file_id=None, intermediate_file_id=host)
        state.setdefault('intermediate_files', {})[host] = row_facts(owned.connection, 'intermediate_files', host)
        event = replace(event, rows=tuple(replace(row, after=replace(row.after,
            values={**row.after.values, 'timeout_s_json':None, 'retry_interval_s_json':None}))
            for row in event.rows))
        plan = replace(plan, events=(event,), state_rows=state)
        receipt = _unchanged(owned, lambda: commit_operation(_proposal_command(plan, bounded), key, owned))
        assert receipt.kind == 'rolled_back', receipt.error
        assert isinstance(receipt.error, EventValidationError)
        assert '配置必须符合真实主机或设备载体分区' in str(receipt.error)
    finally:
        owned.connection.close()


def test_bounded_obtain_check_rejects_caller_supplied_read_port(tmp_path):
    import sqlite3
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(), key)
        with sqlite3.connect(':memory:') as baseline:
            owned.connection.backup(baseline)
            accepted = commit_operation(_proposal_command(plan, True), key, owned)
            assert accepted.kind == 'completed', accepted.error
            baseline.backup(owned.connection)
        receipt = _unchanged(owned, lambda: commit_operation(
            _proposal_command(plan, True, caller_obtain_check_reads=object()), key, owned))
        assert receipt.kind == 'rolled_back', receipt.error
        assert isinstance(receipt.error, ConsistencyError), receipt.error
        assert '读取端口必须由原写事务内核创建' in str(receipt.error)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded', [False, True], ids=['finite', 'actual-bounded'])
@pytest.mark.parametrize('existing', [False, True], ids=['first', 'followup'])
@pytest.mark.parametrize('shape', ['missing', 'mixed_before', 'mixed_after', 'duplicate'])
def test_every_obtain_intent_run_requires_exactly_one_own_new_attempt(tmp_path, bounded, existing, shape):
    from copy import deepcopy
    from camctl.persistence.repositories.transactions import commit_operation
    _, owned = _environment(tmp_path)
    try:
        repository = OperationRepository()
        if existing:
            first = repository.begin_attempt(_intent(), new_operation_key(), owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
            assert _finish(owned, first.value.ticket).kind is DbOutcomeKind.COMPLETED
        cleanup_intent = replace(_intent(), target=AttemptTarget(cleanup_item_id=1))
        if shape.startswith('mixed'):
            owned.connection.execute('INSERT INTO cleanup_items '
                '(id,action_id,requested_output_id,status,restriction_state) VALUES (1,2,1,1,1)')
            owned.connection.commit()
            cleanup = repository.begin_attempt(cleanup_intent, new_operation_key(), owned)
            assert cleanup.kind is DbOutcomeKind.COMPLETED, cleanup.error
            assert _finish(owned, cleanup.value.ticket).kind is DbOutcomeKind.COMPLETED
        key = new_operation_key()
        plan = _capture_plan(owned, _intent(occurred_at=_NOW + 2), key)
        _assert_legal_proposal(owned, plan, bounded, key)
        event, = plan.events
        attempt, run = event.rows
        if shape == 'duplicate':
            duplicate_id = attempt.row_id + 1
            event = replace(event, rows=(*event.rows, replace(attempt, row_id=duplicate_id)))
            plan = replace(plan, owners={**plan.owners,
                ('operation_attempts', duplicate_id): plan.owners['operation_attempts', attempt.row_id]})
        elif shape == 'missing':
            event = replace(event, rows=(run,))
        else:
            other = _capture_plan(owned, replace(cleanup_intent, occurred_at=_NOW + 2), key)
            other_event, = other.events
            assert event.event_id == other_event.event_id
            state = deepcopy(other.state_rows)
            for table, rows in plan.state_rows.items():
                state.setdefault(table, {}).update(deepcopy(rows))
            rows = (run, *other_event.rows) if shape == 'mixed_before' else (*other_event.rows, run)
            event = replace(event, rows=rows)
            plan = replace(plan, owners={**other.owners, **plan.owners}, state_rows=state)
        plan = replace(plan, events=(event,))
        receipt = _unchanged(owned, lambda: commit_operation(_proposal_command(plan, bounded), key, owned))
        assert receipt.kind == 'rolled_back', receipt.error
        from camctl.history.validators import EventValidationError
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert '一次取回 CHECK 意图只能增加紧邻的一票' in str(receipt.error)
    finally:
        owned.connection.close()
