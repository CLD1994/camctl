"""直接文件保证、原自然请求及终态产物采用各自的可靠输入。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import (
    _advance_timelapse_natural_wait, _confirm_timelapse_results,
    _listing_finalized, _listing_round, _product_requirements, _register_listing,
    capture_handler,
)
from camctl.capture.results import CaptureFileSet, FileKind, assess_capture_files
from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import RunFinish, RunOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import consumer_world
from .test_capture_contract import ResultsDouble, _entry, _PAGE_EVIDENCE
from .test_result_device_completion import device_pages
from .test_timelapse_host_timer import HostCatalog, world

pytestmark = pytest.mark.asyncio


async def test_direct_record_file_does_not_consume_unrelated_stop_history(tmp_path):
    owned, runtime, aid, _ = await consumer_world(tmp_path, 'record')
    try:
        stop_id, = owned.connection.execute(
            "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.responsibility_key=?", (f'stop/{aid}',)).fetchone()
        owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?",
            ('{"reason":1,"evidence":{},"rows":[]}', stop_id))
        owned.connection.commit()
        runtime.results = ResultsDouble({aid: (_entry('clip'),)})
        runtime.evidence = _PAGE_EVIDENCE
        listing = await _listing_round(runtime, aid)
        registered = _register_listing(runtime, aid, listing)
        assert [(file.file_id, file.complete) for file, _ in registered] == [('clip', True)]
        assert owned.connection.execute(
            'SELECT json_extract(completion_evidence_json,\'$.basis\') FROM device_files').fetchone() == (1,)
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_original_direct_query_cannot_use_a_later_natural_wait(tmp_path):
    owned, runtime, aid, _ = await consumer_world(tmp_path, 'timelapse')
    runtime.results = ResultsDouble({aid: (_entry('clip'),)})
    runtime.evidence = _PAGE_EVIDENCE
    try:
        listing = await _listing_round(runtime, aid)
        assert listing.ticket is not None
        registered = _register_listing(runtime, aid, listing)
        assert [(file.file_id, file.complete) for file, _ in registered] == [('clip', True)]
        wait_id = _advance_timelapse_natural_wait(runtime, runtime.action(aid))
        assert wait_id is not None
        intent, = owned.connection.execute(
            'SELECT intent_event_id FROM operation_attempts WHERE run_id=?',
            (listing.ticket.run_id,)).fetchone()
        assert intent < wait_id
        assessment = assess_capture_files(CaptureFileSet(
            files=(file for file, _ in registered), set_finalized=_listing_finalized(listing)),
            _product_requirements(runtime.action(aid), FileKind.VIDEO))
        assert assessment.is_complete
        before = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
        with pytest.raises(ConsistencyError):
            _confirm_timelapse_results(runtime, aid, assessment, listing.entries, wait_id, listing)
        assert owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone() == before
        assert owned.connection.execute(
            'SELECT activity_state,occupancy_state,result_set_state,completion_basis FROM device_activities').fetchone() == (1, 1, 1, 1)
        assert runtime.action(aid)['status'] == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('boundary', ['repository', 'event_guard'])
async def test_natural_result_boundary_rejects_original_intent_before_wait(tmp_path, boundary):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.repositories.capture import _history_source_row, _result_check_guard
    from camctl.persistence.transaction import event_envelope, row_facts, update_change
    owned, runtime, aid, _ = await consumer_world(tmp_path, 'timelapse')
    runtime.results = ResultsDouble({aid: (_entry('clip'),)})
    runtime.evidence = _PAGE_EVIDENCE
    try:
        listing = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, listing)
        wait_id = _advance_timelapse_natural_wait(runtime, runtime.action(aid))
        page = runtime.capture.read_last_result_page(listing.ticket, owned)
        observation = {'first_page_event_id': page.ref.event_id,
            'last_page_event_id': page.ref.event_id, 'file_count': 1}
        proof = {'method': 'time_and_outputs', 'wait_completed_event_id': wait_id,
            'observation': observation}
        command = ResultSetSave(aid, runtime.wall_us(), ResultSetPhase.COMPLETE,
            contract='result_files_listed/v2', observation=observation,
            capture={'status': 'completed'}, evidence=proof)
        before = tuple(owned.connection.iterdump())
        if boundary == 'repository':
            pending = runtime.pending_start_results[(listing.ticket.run_id, listing.ticket.attempt_id)]
            finish = replace(pending.finish, run_finish=RunFinish(RunOutcome.SUCCEEDED))
            receipt = runtime.capture.finish_result_check(finish, command, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        else:
            state = {table: {row[0]: row_facts(owned.connection, table, row[0])
                for row in owned.connection.execute(f'SELECT id FROM {table}')}
                for table in ('actions', 'device_activities', 'operation_runs', 'operation_attempts')}
            state['history_events'] = {row[0]: _history_source_row(owned.connection, row[0])
                for row in owned.connection.execute('SELECT id FROM history_events')}
            event_id = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()[0]
            txn_id = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
            event = event_envelope(event_id, txn_id, 16, 1, (update_change('device_activities', aid,
                {'result_set_state': 1, 'result_check_json': None, 'completion_basis': 1,
                 'completion_evidence_json': None, 'capture_json': None},
                {'result_set_state': 3, 'result_check_json': {'contract': command.contract,
                    'outcome': 1, 'observation': observation}, 'completion_basis': 3,
                 'completion_evidence_json': proof, 'capture_json': {'status': 'completed'}}),), runtime.wall_us())
            context = EventContext(TransactionRange(txn_id, event_id, event_id),
                {('device_activities', aid): ('action', aid)}, state)
            with pytest.raises(EventValidationError):
                _result_check_guard(event, context)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('reopen', [False, True])
async def test_known_current_format_finishes_with_original_completion_metadata(tmp_path, monkeypatch, reopen):
    owned, runtime, aid, handler, _, clock = await world(tmp_path, case='unknown_format', completion_page=1)
    import camctl.capture.handlers as handlers
    original_finish = handlers._finish_capture
    class ProcessStopped(Exception):
        pass
    def pause(*args, **kwargs):
        if reopen:
            raise ProcessStopped()
        return original_finish(*args, **kwargs)
    try:
        advance = capture_handler(handler)
        await advance(aid, runtime)
        runtime.timelapse_deadlines.clear()
        await advance(aid, runtime)
        files = owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall()
        assert len(files) == 2
        evidence = runtime.evidence
        later = device_pages(runtime, completion_page=None)
        runtime.evidence = evidence
        clock[0] += 3_000_000_000
        monkeypatch.setattr(handlers, '_finish_capture', pause)
        if reopen:
            with pytest.raises(ProcessStopped):
                await advance(aid, runtime)
            assert owned.connection.execute('SELECT result_set_state FROM device_activities').fetchone() == (3,)
            path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            runtime = replace(runtime, owned=owned, pending_start_results={}, pending_result_scans={},
                pending_file_observations={}, listing_cache={})
            monkeypatch.setattr(handlers, '_finish_capture', original_finish)
            await advance(aid, runtime)
        else:
            await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 3
        assert owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall() == files
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
        assert later.list_results.await_count == 2
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


class MinimumHostCatalog(HostCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is None or action_type != 'camera_timelapse':
            return definition
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(factory(params), product_rules=(
            {'kind': 'video', 'format_id': 'mp4', 'min_count': 1, 'exact_count': None,
             'require_pairing': False, 'min_size_bytes': 51},)))


async def test_current_minimum_size_rejection_does_not_fall_back_to_unknown_retained_format(tmp_path):
    owned, runtime, aid, handler, _, clock = await world(tmp_path,
        catalog=MinimumHostCatalog(), case='unknown_format', completion_page=1)
    try:
        advance = capture_handler(handler)
        await advance(aid, runtime)
        runtime.timelapse_deadlines.clear()
        await advance(aid, runtime)
        original = owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall()
        evidence = runtime.evidence
        later = device_pages(runtime, completion_page=None)
        runtime.evidence = evidence
        clock[0] += 3_000_000_000
        await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 4
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall() == original
        assert later.list_results.await_count == 2
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('consumer', ['photo', 'record', 'timelapse'])
@pytest.mark.parametrize('latest_present', [False, True])
async def test_v1_saved_current_set_and_terminal_retention_have_separate_inputs(tmp_path, consumer, latest_present):
    from camctl.capture.handlers import _finish_listing_result, _saved_result_listing
    from camctl.operations.attempts import AttemptConfig
    from .result_consumer_fixtures import complete_natural_wait
    owned, runtime, aid, handler = await consumer_world(tmp_path, consumer)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    runtime.results = ResultsDouble({aid: (_entry('auxiliary', kind=FileKind.OTHER),)})
    if consumer == 'timelapse':
        complete_natural_wait(runtime, aid)
    try:
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        _finish_listing_result(runtime, first, retry_wait=True)
        original = owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        latest = (_entry('current', kind=FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO),) if latest_present else ()
        runtime.results.files_by_action[aid] = latest
        second = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, second)
        _finish_listing_result(runtime, second, retry_wait=True)
        saved = _saved_result_listing(runtime, aid)
        assert [(entry.identity, entry.kind, entry.format_id) for entry in saved.entries] == [
            (entry.identity, entry.kind, entry.format_id) for entry in latest]
        current = _register_listing(runtime, aid, saved)
        assert [file.file_id for file, _ in current] == [entry.identity for entry in latest]
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)['status'] == 4
        assert runtime.results.calls == [aid, aid]
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files WHERE id=?',
            (original[0][0],)).fetchall() == original
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (1 + int(latest_present),)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities').fetchone() == (
            (3, 2) if consumer == 'record' else (2, 1) if consumer == 'photo' else (1, 1))
    finally:
        owned.connection.close()


@pytest.mark.parametrize('consumer', ['photo', 'record'])
async def test_v1_current_completion_must_match_original_fixed_size(tmp_path, consumer):
    from camctl.capture.handlers import _finish_listing_result, _saved_result_listing
    from camctl.operations.attempts import AttemptConfig
    owned, runtime, aid, _ = await consumer_world(tmp_path, consumer)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    kind = FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO
    runtime.results = ResultsDouble({aid: (_entry('current', kind=kind),)})
    try:
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        _finish_listing_result(runtime, first, retry_wait=True)
        original = owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        runtime.results.files_by_action[aid] = (replace(_entry('current', kind=kind), size_bytes=99),)
        second = await _listing_round(runtime, aid)
        current = _register_listing(runtime, aid, second)
        assert [(file.file_id, file.complete, file.size_bytes) for file, _ in current] == [('current', False, None)]
        _finish_listing_result(runtime, second, retry_wait=True)
        saved = _saved_result_listing(runtime, aid)
        assert [(file.file_id, file.complete, file.size_bytes) for file, _ in _register_listing(runtime, aid, saved)] == [
            ('current', False, None)]
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
    finally:
        owned.connection.close()


async def test_record_cancel_binding_failure_keeps_files_without_outputs(tmp_path):
    from camctl.capture.handlers import _finish_listing_result
    from camctl.devices.bindings import BindingResult, BindingStatus
    from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'record', start=False)
    try:
        await capture_handler(handler)(aid, runtime)
        runtime.results = ResultsDouble({aid: (_entry('current'),)})
        listing = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, listing)
        _finish_listing_result(runtime, listing, retry_wait=True)
        original = owned.connection.execute(
            'SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        _apply_public_cancel(owned, aid, runtime.wall_us())
        runtime.binding_check = lambda saved: BindingResult(BindingStatus.DEVICE_MISSING, saved, 'missing')
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)['status'] == 6
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
        assert runtime.results.calls == [aid]
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


_KIND_BOUNDARIES = [
    ('record', 'live', 'opaque', 'known', 0, 1),
    ('record', 'live', 'parsed', 'known', 0, 1),
    ('record', 'cache', 'opaque', 'known', 50, 51),
    ('record', 'cache', 'parsed', 'known', 50, 51),
    ('record', 'saved', 'opaque', 'known', 50, 51),
    ('record', 'live', 'opaque', 'known', 50, 1),
    ('record', 'live', 'opaque', 'unknown', 50, 1),
    ('record', 'cache', 'opaque', 'unknown', 50, 1),
    ('record', 'saved', 'opaque', 'unknown', 50, 1),
    ('record', 'live', 'parsed', 'explicit_other', 50, 1),
    ('photo', 'live', 'opaque', 'known', 50, 1),
    ('photo', 'live', 'parsed', 'known', 50, 1),
    ('photo', 'saved', 'opaque', 'known', 50, 1),
    ('photo', 'live', 'opaque', 'unknown', 50, 1),
    ('timelapse', 'saved', 'opaque', 'known', 0, 1),
    ('timelapse', 'saved', 'opaque', 'known', 50, 51),
    ('timelapse', 'saved', 'opaque', 'known', 50, 1),
    ('timelapse', 'saved', 'opaque', 'unknown', 50, 1),
]


@pytest.mark.parametrize('consumer,view,proof,current_kind,size,minimum', _KIND_BOUNDARIES,
    ids=['-'.join(map(str, case)) for case in _KIND_BOUNDARIES])
async def test_v1_terminal_uses_known_typed_kind_and_retains_unknown_current(
        tmp_path, consumer, view, proof, current_kind, size, minimum):
    """实际共享失败收尾消费live／cache对象或可靠R，不声称运行完整媒体链。"""
    from unittest.mock import create_autospec
    from camctl.capture.handlers import (
        ResultFilesPort, _close_check_unconfirmed, _finish_capture, _finish_listing_result,
        _finish_timelapse_conclusion, _retained_result_files, _saved_result_listing, _unconfirmed_failure,
    )
    from camctl.capture.result_inputs import ListedResult, observed_file
    from camctl.devices.evidence import DeviceObservation
    from camctl.operations.attempts import AttemptConfig
    from camctl.operations.models import ErrorValue
    from .result_consumer_fixtures import ResultCatalog, complete_natural_wait
    from .test_result_page_history import _actual

    class Catalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is None or action_type == 'camera_take_photo':
                return definition
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(factory(params), product_rules=(
                {'kind': 'video', 'format_id': 'osv', 'min_count': 1, 'exact_count': None,
                 'require_pairing': False, 'min_size_bytes': minimum},)))

    owned, runtime, aid, _ = await consumer_world(tmp_path, consumer, catalog=Catalog())
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    if consumer == 'timelapse':
        complete_natural_wait(runtime, aid)
    kind = FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO
    format_id = 'jpeg' if consumer == 'photo' else 'osv'
    retained_kind = FileKind.OTHER if current_kind == 'known' else kind
    retained_format = None if current_kind == 'known' else format_id
    port = create_autospec(ResultFilesPort, instance=True)
    runtime.results = port
    calls = []

    async def actual(ticket, *, timeout_s):
        calls.append(ticket)
        raw = {'identity': 'current', 'locator': {'path': '/DCIM/current'},
               'complete': True, 'size_bytes': size,
               'kind': retained_kind.value if len(calls) == 1 else kind.value,
               'format_id': retained_format if len(calls) == 1 else format_id}
        if len(calls) == 2 and current_kind != 'known':
            raw['format_id'] = None
            if current_kind == 'unknown':
                del raw['kind']
            else:
                raw['kind'] = 'other'
        outcome = _actual(ticket, entries=[raw], error=ErrorValue('device_error', 'device'))
        outcome = replace(outcome, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': ticket.target_id, 'entries': [raw]}),))
        entry = observed_file(raw)
        if proof == 'opaque':
            entry = replace(entry, evidence={'listing': 'current'})
        return ListedResult((entry,), outcome)

    port.list_round.side_effect = actual
    try:
        for _ in range(2):
            listing = await _listing_round(runtime, aid)
            registered = _register_listing(runtime, aid, listing)
            _finish_listing_result(runtime, listing, retry_wait=True)
            if len(calls) == 1:
                original = owned.connection.execute(
                    'SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        if view == 'saved':
            listing = _saved_result_listing(runtime, aid)
            registered = _register_listing(runtime, aid, listing)
        elif view == 'cache':
            runtime.listing_cache = {aid: (registered.entries, registered)}
            _, registered = runtime.listing_cache[aid]
        terminal = _retained_result_files(runtime, listing, registered)
        if consumer == 'timelapse':
            _close_check_unconfirmed(runtime, aid)
            await _finish_timelapse_conclusion(runtime, aid)
        else:
            _finish_capture(runtime, aid, registered.entries, kind, registered=registered,
                failure=_unconfirmed_failure(runtime, aid))
        expected_outputs = int(consumer == 'photo' or size >= minimum)
        assert runtime.action(aid)['status'] == 4
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (expected_outputs,)
        expected_kind = FileKind.OTHER if current_kind == 'explicit_other' else kind
        assert [(entry.kind, entry.format_id) for entry in terminal.entries] == [(expected_kind, format_id)]
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
        assert len(calls) == port.list_round.await_count == 2
        runtime.driver.control.assert_awaited_once()
        if consumer == 'record':
            runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()
