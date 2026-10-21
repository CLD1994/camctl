"""原始结果页为独立设备文件保存用途与来源；配对另行证明。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from camctl.capture import files
from camctl.capture.handlers import _begin_check_round
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.contracts.values import ConsistencyError
from camctl.history.validators import EventContext
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import BoundedCommandPlan, commit_operation, row_facts
from camctl.persistence.repositories.result_file_ownership import _FileOwnershipFromResultPageCommand
from camctl.persistence.repositories import result_pages
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards

from .test_segment_storage import SegmentedCatalog
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_page_history import _actual, _request

register_operation_guards()
register_capture_guards()


def _ok(receipt):
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return receipt.value


def _entry(name, *, complete=True, size=10, format_id=None):
    path = '/DCIM/' + name
    return {'identity': path, 'locator': {'path': path}, 'complete': complete,
            'size_bytes': size, 'kind': 'video',
            'format_id': format_id or name.rpartition('.')[2].lower(),
            'paired_identity': None}


async def _world(tmp_path, entries, *, consumer='record', independent_activity=False, catalog=None,
                 next_cursor=None, error=None):
    owned, runtime, action_id, _ = await consumer_world(
        tmp_path, consumer, catalog=catalog or SegmentedCatalog(),
        independent_activity=independent_activity)
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        request = _request(runtime, ticket, _actual(ticket, entries=entries,
            next_cursor=next_cursor, finalized=next_cursor is None and error is None, error=error))
        page = _ok(runtime.capture.save_result_page(request, new_operation_key(), owned))
        saved = runtime.capture.read_result_page(page, owned)
        return owned, runtime, action_id, saved
    except BaseException:
        owned.connection.close()
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize('consumer', ['record', 'timelapse'])
async def test_raw_lrf_owned_before_pair(tmp_path, consumer):
    # 若原页分支遗漏或把 paired=None 一律作为原片，本测试必须失败。
    entries = [_entry('opaque.LRF', complete=False, size=None),
               _entry('opaque.lrf', complete=False, size=None),
               _entry('opaque.OSV'), _entry('zero.lrf', size=0)]
    owned, runtime, source, saved = await _world(tmp_path, entries, consumer=consumer)
    try:
        ids = dict(saved.file_ids)
        for identity, expected_role in [('/DCIM/opaque.LRF', 3),
                                        ('/DCIM/opaque.OSV', 2),
                                        ('/DCIM/opaque.lrf', 3),
                                        ('/DCIM/zero.lrf', 3)]:
            command = files.OwnershipFromResultPageSave(
                saved.ref, ids[identity], runtime.wall_us())
            outcome = _ok(runtime.capture.save_ownership_from_result_page(
                command, new_operation_key(), owned))
            assert outcome.file_id == ids[identity]
            row = owned.connection.execute(
                'SELECT source_action_id,role,ownership_evidence_json,'
                'original_device_file_id,pairing_evidence_json FROM device_files WHERE id=?',
                (outcome.file_id,)).fetchone()
            assert row[:2] == (source, expected_role)
            assert json.loads(row[2]) == {
                'method': int(enum_for('device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE),
                'result_page_event_id': saved.ref.event_id}
            assert row[3:] == (None, None)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()[0] == 0
    finally:
        owned.connection.close()


def _snapshot(owned):
    return {name: owned.connection.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
            for name in ('device_files', 'outputs', 'actions', 'device_activities',
                         'operation_runs', 'operation_attempts', 'result_page_files',
                         'history_transactions', 'history_events', 'entity_event_links',
                         'report_entity_changes', 'entity_snapshot_progress')}


def _command(runtime, saved, *, file_id=None):
    return files.OwnershipFromResultPageSave(saved.ref,
        saved.file_ids[0][1] if file_id is None else file_id, runtime.wall_us())


def _bounded(plan, connection, *, context_transform=lambda context: context):
    return BoundedCommandPlan(iter(plan.events),
        lambda event, transaction: context_transform(EventContext(
            transaction, plan.owners, deepcopy(plan.state_rows),
            current_reads=SqlEventReads(connection, plan.capture_result_read_scope))),
        plan.result, lambda connection: None)


class _AlteredCommand:
    def __init__(self, request, key, *, bounded=False, alter=lambda plan: plan,
                 context_transform=lambda context: context):
        self.request, self.key = request, key
        self.bounded, self.alter, self.context_transform = bounded, alter, context_transform

    def plan(self, scope):
        plan = self.alter(_FileOwnershipFromResultPageCommand(self.request, self.key).plan(scope))
        return (_bounded(plan, scope.connection, context_transform=self.context_transform)
                if self.bounded else plan)


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
async def test_original_page_ownership_both_real_transaction_entries(tmp_path, bounded):
    owned, runtime, source, saved = await _world(tmp_path, [_entry('orphan.LRF')],
                                                independent_activity=True)
    try:
        request, key = _command(runtime, saved), new_operation_key()
        physical, = owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id)).fetchone()
        assert source != int(saved.ref.ticket.target_id)
        assert physical != saved.ref.ticket.attempt_id
        receipt = commit_operation(_AlteredCommand(request, key, bounded=bounded), key, owned)
        assert receipt.kind == 'completed', receipt.error
        assert receipt.boundary.last_event_id > saved.ref.event_id
        tx = owned.connection.execute('SELECT first_event_id,last_event_id FROM history_transactions WHERE operation_key=?',
                                      (str(key),)).fetchone()
        assert tx[0] == tx[1]
        assert runtime.capture.verify_ownership_from_result_page(request, key, owned).file_id == request.file_id
    finally:
        owned.connection.close()


def _alter_event(plan, case):
    event, = plan.events
    row, = event.rows
    after, before, evidence = dict(row.after.values), dict(row.before.values), deepcopy(event.evidence)
    raw = evidence['ownership_from_result_page_request']
    if case == 'source': after['source_action_id'] += 100
    elif case == 'role': after['role'] = 2
    elif case == 'evidence_page': after['ownership_evidence_json'] = {**after['ownership_evidence_json'], 'result_page_event_id': 1}
    elif case == 'pair':
        before['original_device_file_id'] = None; after['original_device_file_id'] = 1
    elif case == 'wrong_file': raw['file_id'] += 1
    elif case == 'wrong_page': raw['page']['event_id'] += 1
    elif case == 'wrong_run': raw['page']['ticket']['run_id'] += 1
    elif case == 'wrong_attempt': raw['page']['ticket']['attempt_id'] += 1
    elif case == 'wrong_activity':
        raw['page']['ticket']['target_id'] = '99'; raw['page']['ticket']['responsibility_key'] = 'results/99'
    elif case == 'wrong_operation': raw['page']['ticket']['operation'] = 'control'
    elif case == 'wrong_key': raw['page']['ticket']['responsibility_key'] = 'results/other'
    elif case == 'missing_page': raw['page'].pop('next_cursor')
    elif case == 'claim': raw['qualified'] = True
    elif case == 'read_scope':
        return replace(plan, capture_result_read_scope=replace(plan.capture_result_read_scope,
                                                              action_id=999))
    elif case == 'caller_state':
        state = deepcopy(plan.state_rows); state['device_files'][row.row_id]['source_action_id'] = 999
        return replace(plan, state_rows=state)
    else: raise AssertionError(case)
    changed = replace(row, before=replace(row.before, values=before), after=replace(row.after, values=after))
    return replace(plan, events=(replace(event, rows=(changed,), evidence=evidence),))


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('case', ['source', 'role', 'evidence_page', 'pair', 'wrong_file',
    'wrong_page', 'wrong_run', 'wrong_attempt', 'wrong_activity', 'wrong_operation',
    'wrong_key', 'missing_page', 'claim', 'caller_state', 'read_scope'])
async def test_forged_original_page_ownership_rolls_back_whole_group(tmp_path, bounded, case):
    owned, runtime, _, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    try:
        before = _snapshot(owned); key = new_operation_key()
        receipt = commit_operation(_AlteredCommand(_command(runtime, saved), key, bounded=bounded,
            alter=lambda plan: _alter_event(plan, case)), key, owned)
        assert receipt.kind == 'rolled_back', (case, receipt.error)
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_index', 'binding', 'activity', 'physical_attempt', 'baseline', 'format'])
async def test_real_sql_proof_is_required_before_saving(tmp_path, case):
    owned, runtime, source, saved = await _world(tmp_path, [_entry('orphan.LRF', format_id='osv' if case == 'format' else None)],
                                                independent_activity=True)
    try:
        db = owned.connection
        if case == 'missing_index': db.execute('DELETE FROM result_page_files WHERE page_event_id=?', (saved.ref.event_id,))
        elif case == 'binding': db.execute("UPDATE actions SET driver_id='wrong' WHERE id=?", (source,))
        elif case == 'activity': db.execute('UPDATE device_activities SET action_id=1 WHERE id=?', (int(saved.ref.ticket.target_id),))
        elif case == 'physical_attempt': db.execute('UPDATE operation_attempts SET attempt_no=99 WHERE run_id=?', (saved.ref.ticket.run_id,))
        elif case == 'baseline': db.execute('UPDATE device_activities SET ownership_mode=2,baseline_state=2 WHERE id=?', (int(saved.ref.ticket.target_id),))
        before = _snapshot(owned)
        receipt = runtime.capture.save_ownership_from_result_page(_command(runtime, saved), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('case', ['missing_index', 'binding', 'activity', 'physical_attempt', 'baseline', 'format'])
async def test_independent_guard_rechecks_actual_proof_after_proposal(tmp_path, bounded, case):
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.transaction import encode_json_value
    owned, runtime, source, saved = await _world(tmp_path, [_entry('orphan.LRF')], independent_activity=True)
    try:
        changed = []
        def alter(plan):
            db = owned.connection
            if case == 'missing_index': db.execute('DELETE FROM result_page_files WHERE page_event_id=?', (saved.ref.event_id,))
            elif case == 'binding': db.execute("UPDATE actions SET driver_id='wrong' WHERE id=?", (source,))
            elif case == 'activity': db.execute('UPDATE device_activities SET action_id=1 WHERE id=?', (int(saved.ref.ticket.target_id),))
            elif case == 'physical_attempt': db.execute('UPDATE operation_attempts SET attempt_no=99 WHERE run_id=?', (saved.ref.ticket.run_id,))
            elif case == 'baseline': db.execute('UPDATE device_activities SET ownership_mode=2,baseline_state=2 WHERE id=?', (int(saved.ref.ticket.target_id),))
            elif case == 'format':
                body = parse_exact_json(db.execute('SELECT body_json FROM history_events WHERE id=?', (saved.ref.event_id,)).fetchone()[0])
                body['evidence']['result_page']['outcome']['result']['observations'][0]['data']['entries'][0]['format_id'] = 'osv'
                db.execute('UPDATE history_events SET body_json=? WHERE id=?', (encode_json_value(body), saved.ref.event_id))
            changed.append(case)
            return plan
        before = _snapshot(owned); key = new_operation_key()
        receipt = commit_operation(_AlteredCommand(_command(runtime, saved), key,
            bounded=bounded, alter=alter), key, owned)
        assert receipt.kind == 'rolled_back', (case, receipt.error)
        from camctl.history.validators import EventValidationError
        assert changed == [case] and isinstance(receipt.error, EventValidationError), receipt.error
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_repeated_ownership_keeps_first_proof_and_checks_new_page_first(tmp_path):
    owned, runtime, _, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    try:
        request, key = _command(runtime, saved), new_operation_key()
        _ok(runtime.capture.save_ownership_from_result_page(request, key, owned))
        before = _snapshot(owned)
        result = _ok(runtime.capture.save_ownership_from_result_page(request, new_operation_key(), owned))
        assert result.disposition is files.ObservationDisposition.ALREADY
        assert _snapshot(owned) == before
        for changed in (replace(request, occurred_at=request.occurred_at + 1),
                        replace(request, page=replace(request.page, event_id=request.page.event_id + 1))):
            receipt = runtime.capture.save_ownership_from_result_page(changed, key, owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        changed = replace(request, page=replace(request.page, event_id=request.page.event_id + 1))
        assert runtime.capture.save_ownership_from_result_page(changed, new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['commit_before', 'commit_after'])
async def test_unknown_receipt_fresh_verifies_original_key_and_exact_request(tmp_path, mode):
    from ..capture.test_result_file_fact_saves import FileFactFault
    from camctl.persistence.runtime import open_existing, DbConfig, DbOpenMode
    owned, runtime, _, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    request, key = _command(runtime, saved), new_operation_key()
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    try:
        proxy = FileFactFault(owned.connection, 'ownership', mode)
        receipt = runtime.capture.save_ownership_from_result_page(request, key, replace(owned, connection=proxy))
        assert receipt.kind is DbOutcomeKind.UNKNOWN, receipt.error
        assert not proxy.armed
        # 原调用已返回；旧连接关闭后，真正新连接只核原key及原申请。
        owned.connection.close()

        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        with pytest.raises(ConsistencyError):
            runtime.capture.verify_ownership_from_result_page(request, key, owned, execution_finished=False)
        result = runtime.capture.verify_ownership_from_result_page(request, key, owned)
        if mode == 'commit_before':
            assert result is None
            _ok(runtime.capture.save_ownership_from_result_page(request, key, owned))
        else:
            assert result.file_id == request.file_id
        with pytest.raises((ConsistencyError, ValueError)):
            runtime.capture.verify_ownership_from_result_page(replace(request, occurred_at=request.occurred_at + 1), key, owned)
        before = _snapshot(owned)
        _ok(runtime.capture.save_ownership_from_result_page(request, key, owned))
        assert _snapshot(owned) == before
        owned.connection.close()
        with pytest.raises(sqlite3.ProgrammingError):
            runtime.capture.verify_ownership_from_result_page(request, new_operation_key(), owned)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
async def test_old_ownership_branch_cannot_use_new_page_method(tmp_path, bounded):
    from camctl.history.events import load_event_registry
    owned, runtime, source, saved = await _world(tmp_path, [_entry('a.OSV')])
    try:
        method = int(enum_for('device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE)
        with pytest.raises(ValueError):
            files.OwnershipSave(saved.file_ids[0][1], source, method, 2, {}, runtime.wall_us())
        with pytest.raises(ValueError):
            files.OwnershipSave(saved.file_ids[0][1], source, 1, 3, {}, runtime.wall_us())
        def old_branch(plan):
            event, = plan.events; row, = event.rows
            before, after = dict(row.before.values), dict(row.after.values)
            after['ownership_evidence_json'] = {'method': method, 'observation': {}}
            row = replace(row, before=replace(row.before, values=before), after=replace(row.after, values=after))
            reason = load_event_registry()['events']['DEVICE_FILE_OBSERVED']['branches']['OWNERSHIP']['reason']
            return replace(plan, events=(replace(event, reason=reason, rows=(row,), evidence={}),))
        before = _snapshot(owned); key = new_operation_key()
        receipt = commit_operation(_AlteredCommand(_command(runtime, saved), key,
            bounded=bounded, alter=old_branch), key, owned)
        assert receipt.kind == 'rolled_back'
        assert '独立原页分支' in str(receipt.error)
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('fake_single_context', [False, True])
async def test_real_legal_file_prefix_cannot_join_single_ownership_group(tmp_path, bounded, fake_single_context):
    from camctl.persistence.repositories.capture import _FilePresenceCommand
    from camctl.persistence.transaction import CommandPlan, TransactionScope
    from camctl.contracts.history_values import TransactionRange
    owned, runtime, _, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    request, key = _command(runtime, saved), new_operation_key()
    class PrefixCommand:
        def plan(self, scope):
            # 两份真实合法单事件提案以独立临时scope形成，再用实际scope分配2事件。
            def separate(command):
                return command.plan(TransactionScope(scope.connection, scope.max_txn_id, scope.max_event_id))
            prefix = separate(_FilePresenceCommand(files.FilePresenceSave(request.file_id, 2, request.occurred_at), new_operation_key()))
            ownership = separate(_FileOwnershipFromResultPageCommand(request, key))
            allocation = scope.allocate(2)
            first, = prefix.events; second, = ownership.events
            second = replace(second, event_id=allocation.last_event_id)
            state = deepcopy(ownership.state_rows)
            combined = CommandPlan((first, second), {**prefix.owners, **ownership.owners}, state,
                capture_result_read_scope=ownership.capture_result_read_scope)
            if not bounded: return combined
            def context(event, transaction):
                facts = row_facts(scope.connection, 'device_files', request.file_id)
                if fake_single_context and event is second:
                    transaction = TransactionRange(transaction.txn_id, event.event_id, event.event_id)
                return EventContext(transaction, combined.owners,
                    {'device_files': {request.file_id: facts}},
                    current_reads=SqlEventReads(scope.connection, ownership.capture_result_read_scope))
            return BoundedCommandPlan(iter(combined.events), context, None, lambda connection: None)
    try:
        before = _snapshot(owned)
        receipt = commit_operation(PrefixCommand(), key, owned)
        assert receipt.kind == 'rolled_back', receipt.error
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['old', 'future', 'middle'])
async def test_replay_override_rejects_non_current_boundaries(tmp_path, case):
    from camctl.contracts.history_values import HistoryBoundary
    owned, runtime, _, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    try:
        db = owned.connection
        original, raw, event, transaction = result_pages._at_record(db, saved.ref.event_id)
        latest = result_pages._latest_history_boundary(db)
        # 实际预插独立单event header；尚没有任何本组event/投影。
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO history_transactions(id,operation_key,first_event_id,last_event_id) VALUES(?,?,?,?)',
            (latest.txn_id + 1, str(new_operation_key()), latest.last_event_id + 1, latest.last_event_id + 1))
        if case == 'old':
            prior = result_pages.read_transaction_range(db, latest.txn_id - 1)
            boundary = HistoryBoundary(prior.txn_id, prior.last_event_id)
        elif case == 'future': boundary = HistoryBoundary(latest.txn_id + 1, latest.last_event_id + 1)
        else: boundary = HistoryBoundary(latest.txn_id, latest.last_event_id - 1)
        with pytest.raises(ConsistencyError):
            result_pages._replay_page_context(db, event, (original, raw), transaction, current_boundary=boundary)
        db.execute('ROLLBACK')
    finally:
        owned.connection.close()


class _BaselineCatalog(SegmentedCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] != 'camera_record': return definition
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(factory(params),
            ownership_mode=2, output_scope={'directories': ['/DCIM']}))


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
async def test_original_baseline_difference_excludes_old_file_and_survives_ownership(tmp_path, bounded):
    from camctl.capture.baseline_models import BaselineChunkSave, BaselineFixSave
    from camctl.capture.handlers import _control_call
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.file_identity import FileIdentity
    owned, runtime, source, _ = await consumer_world(tmp_path, 'record', start=False, catalog=_BaselineCatalog())
    try:
        activity, = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (source,)).fetchone()
        old = '/DCIM/a.OSV'
        old_id = _ok(runtime.capture.save_file_observation(files.FileObservationSave(
            source, old, {'path': old}, runtime.wall_us()), new_operation_key(), owned)).file_id
        _ok(runtime.capture.append_baseline(BaselineChunkSave(activity, 1,
            (FileIdentity(DeviceBinding('cam-1', 'camctl-adb'), old),), runtime.wall_us()), new_operation_key(), owned))
        _ok(runtime.capture.fix_baseline(BaselineFixSave(activity, 1, 1, runtime.wall_us()), new_operation_key(), owned))
        await _control_call(runtime, runtime.action(source), 'start_recording', 'start_confirmed',
                            activity_facts={'started_at': runtime.wall_us(), 'activity_state': 2})
        ticket = _begin_check_round(runtime, source).ticket
        request = _request(runtime, ticket, _actual(ticket,
            entries=[_entry('a.OSV'), _entry('b.LRF')], finalized=True))
        ref = _ok(runtime.capture.save_result_page(request, new_operation_key(), owned))
        saved = runtime.capture.read_result_page(ref, owned)
        assert dict(saved.file_ids).keys() == {'/DCIM/b.LRF'}
        command, key = _command(runtime, saved), new_operation_key()
        receipt = commit_operation(_AlteredCommand(command, key, bounded=bounded), key, owned)
        assert receipt.kind == 'completed', receipt.error
        assert runtime.capture.verify_ownership_from_result_page(command, key, owned).file_id == command.file_id
        before = _snapshot(owned)
        rejected = runtime.capture.save_ownership_from_result_page(replace(command, file_id=old_id), new_operation_key(), owned)
        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_policy_and_already_fixed_source_role_are_checked(tmp_path):
    from ..capture.result_consumer_fixtures import ResultCatalog
    owned, runtime, _, saved = await _world(tmp_path, [_entry('a.LRF')], catalog=ResultCatalog())
    try:
        before = _snapshot(owned)
        assert runtime.capture.save_ownership_from_result_page(_command(runtime, saved), new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()
    owned, runtime, source, saved = await _world(tmp_path / 'fixed', [_entry('a.LRF')], independent_activity=True)
    try:
        file_id = saved.file_ids[0][1]
        # 旧低层模型的独立给定前提仍可保存；新原页模型不得将已固定来源或用途改指。
        _ok(runtime.capture.save_file_ownership(files.OwnershipSave(file_id, source - 1, 1, 2,
            {}, runtime.wall_us()), new_operation_key(), owned))
        before = _snapshot(owned)
        assert runtime.capture.save_ownership_from_result_page(_command(runtime, saved), new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, 128])
async def test_empty_and_full_legal_page_remain_exact_scopes(tmp_path, count):
    owned, runtime, _, saved = await _world(tmp_path,
        [_entry(f'{number:04d}.LRF', complete=False, size=None) for number in range(count)])
    try:
        before = _snapshot(owned)
        file_id = saved.file_ids[-1][1] if count else 1
        request = files.OwnershipFromResultPageSave(saved.ref, file_id, runtime.wall_us())
        receipt = runtime.capture.save_ownership_from_result_page(request, new_operation_key(), owned)
        if not count:
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
            assert _snapshot(owned) == before
        else:
            _ok(receipt)
            assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE source_action_id IS NOT NULL').fetchone() == (1,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('kind', ['next_page', 'actual_error'])
async def test_ownership_needs_reliable_page_not_complete_scan(tmp_path, bounded, kind):
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor
    from camctl.operations.models import ErrorValue
    cursor = (DirectoryCursor(DeviceBinding('cam-1', 'camctl-adb'), ('/DCIM',),
                              0, '/DCIM/orphan.LRF') if kind == 'next_page' else None)
    error = ErrorValue('device_error', 'execution', {}) if kind == 'actual_error' else None
    owned, runtime, _, saved = await _world(tmp_path,
        [_entry('orphan.LRF', complete=False, size=None)], next_cursor=cursor, error=error)
    try:
        request, key = _command(runtime, saved), new_operation_key()
        receipt = commit_operation(_AlteredCommand(request, key, bounded=bounded), key, owned)
        assert receipt.kind == 'completed', receipt.error
        assert saved.page.next_cursor == cursor and saved.page.outcome.error == error
        assert runtime.capture.verify_ownership_from_result_page(request, key, owned).file_id == request.file_id
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_file_owner_initial_snapshot_current_restore_same_h(tmp_path):
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import branch_of, event_type_name
    from camctl.history.replay import EntityImage, RestoreSeed, restore
    from camctl.history.validators import ValidatedEvent
    from camctl.contracts.history_values import INITIAL_BOUNDARY
    from camctl.history.snapshots import SnapshotRef, prepare_snapshot
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    owned, runtime, source, saved = await _world(tmp_path, [_entry('orphan.LRF')])
    prepared = []; executor = None
    try:
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        before = history.current_boundary()
        action_count = owned.connection.execute('SELECT change_count FROM actions WHERE id=?', (source,)).fetchone()[0]
        request, key = _command(runtime, saved), new_operation_key()
        _ok(runtime.capture.save_ownership_from_result_page(request, key, owned))
        boundary = history.current_boundary()
        from camctl.contracts.enums import load_registry
        entity_type = load_registry()['history_objects']['device_file']['id']
        action_type = load_registry()['history_objects']['action']['id']
        # 当前文件、原页动作由各自owner保存；三列归属不推进action次数。
        assert owned.connection.execute('SELECT change_count FROM actions WHERE id=?', (source,)).fetchone() == (action_count,)
        current = history.restore_entity('device_file', request.file_id, boundary)[('device_files', request.file_id)]
        unowned = history.restore_entity('device_file', request.file_id, before)[('device_files', request.file_id)]
        assert unowned['source_action_id'] is None and unowned['role'] == 1
        events = []
        for raw in owned.connection.execute(f'SELECT {" ,".join("h." + name for name in result_pages._COLUMNS.split(","))}'
                ' FROM history_events h JOIN entity_event_links l ON l.event_id=h.id'
                ' WHERE l.entity_type=? AND l.entity_id=? AND h.id<=? ORDER BY h.id',
                (entity_type, request.file_id, boundary.last_event_id)):
            event = decode_event_row(raw)
            events.append(ValidatedEvent(event, event_type_name(event.event_type),
                branch_of(event.event_type, event.reason)[0], ((entity_type, request.file_id),),
                {(row.table, row.row_id): (entity_type, request.file_id) for row in event.rows}))
        initial = restore(RestoreSeed(EntityImage(entity_type, request.file_id, False, {}, 0, 0),
                                     INITIAL_BOUNDARY), events, boundary)
        from camctl.history.events import business_columns
        assert {name: current[name] for name in business_columns('device_files')} == initial.rows[('device_files', request.file_id)]
        executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
                              capacity=4, enqueue_timeout_seconds=9)
        store = SqliteSnapshotStore(path, executor)
        images = await store.load_entity_images((SnapshotRef(entity_type, request.file_id),
                                                SnapshotRef(action_type, source)))
        prepared = [prepare_snapshot(image) for image in images]
        assert (await store.save_snapshots(tuple(prepared)))[0].ref.entity_id == request.file_id
        for presence in (2, 3, 2):
            _ok(runtime.capture.save_file_presence(files.FilePresenceSave(request.file_id, presence,
                runtime.wall_us()), new_operation_key(), owned))
        with history._read_connection() as db:
            seed = history._object_seed(db, 'device_file', request.file_id, boundary, event_batch_size=1)
            assert seed.snapshot is not None and seed.snapshot.boundary == boundary
        snapshot = history.restore_entity('device_file', request.file_id, boundary, event_batch_size=1)
        assert snapshot[('device_files', request.file_id)] == current
        assert runtime.capture.verify_ownership_from_result_page(request, key, owned).file_id == request.file_id
        assert current['role'] == 3 and current['source_action_id'] == source
        assert current['original_device_file_id'] is None and current['pairing_evidence_json'] is None
        # action initial/current 同H保留原页关系，文件事实不混入action对象。
        for strategy in ('initial', 'snapshot', 'current'):
            with history.open_segment_history(source, boundary, strategy=strategy, batch_size=1) as handle:
                page = handle.read_page('result_page_files', limit=1)
                assert page.items[0].values['file_id'] == request.file_id
                assert not handle.read_page('device_files', limit=1).items
    finally:
        if executor is not None: await executor.close()
        owned.connection.close()
