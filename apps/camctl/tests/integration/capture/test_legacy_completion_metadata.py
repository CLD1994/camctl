"""旧 v1 的完成元信息来自首次 COMPLETE 所属尝试，发现条目只证明身份。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import (
    ResultFilesPort, _advance_timelapse_natural_wait, _finish_listing_result,
    _listing_round, _register_listing,
)
from camctl.capture.result_inputs import ListedResult, observed_file
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import RESULT_EVIDENCE
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_native_completion_context import _osv
from .test_natural_wait_source_transitions import _legacy_catalog, _stopper, _finish_cancel
from .test_natural_wait_time_eligibility import _world
from .test_result_page_history import _actual

pytestmark = pytest.mark.asyncio


async def _completed_world(tmp_path, startup, size, opaque, result_first, save_mode='reliable'):
    owned, runtime, aid, advance, wall, mono, unused, stopper, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup))
    runtime.check_config = AttemptConfig(3, Decimal('1'), Decimal(0))
    runtime.evidence = RESULT_EVIDENCE
    port = create_autospec(ResultFilesPort, instance=True)
    runtime.results = port
    calls = []
    completed = {**_osv(size), 'original_name': 'completed.OSV'}
    discovery = {**completed, 'complete': False, 'size_bytes': None,
                 'kind': 'other', 'format_id': None, 'original_name': 'discovered.bin'}
    later = {**discovery, 'original_name': 'later-bad.bin'}
    async def actual(ticket, *, timeout_s):
        calls.append(ticket)
        raw = (discovery, completed, later)[len(calls)-1]
        outcome = _actual(ticket, entries=[raw], error=ErrorValue('device_error', 'device'))
        outcome = replace(outcome, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': ticket.target_id, 'entries': [raw]}),))
        entry = observed_file(raw)
        return ListedResult((replace(entry, evidence={'listing': 'opaque'} if opaque else entry.evidence),), outcome)
    port.list_round.side_effect = actual
    wall[0] = due+100
    mono[0] += (startup+1200)*1000000
    assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
    first = await _listing_round(runtime, aid)
    _register_listing(runtime, aid, first)
    _finish_listing_result(runtime, first, retry_wait=True)
    incomplete = runtime.capture.read_result_sources(calls[-1], None, 1, owned)
    assert not incomplete.items[0][1].complete
    second = await _listing_round(runtime, aid)
    if result_first:
        _finish_listing_result(runtime, second, retry_wait=True)
    requests = []
    if save_mode != 'reliable':
        save = runtime.capture.save_file_completion
        def uncertain(command, key, current):
            requests.append((command, key))
            if len(requests) <= 2:
                if save_mode == 'unknown-after' and len(requests) == 1:
                    assert save(command, key, current).kind is DbOutcomeKind.COMPLETED
                return DbOutcome(DbOutcomeKind.UNKNOWN)
            return save(command, key, current)
        runtime.capture.save_file_completion = uncertain
        with pytest.raises(ConsistencyError):
            _register_listing(runtime, aid, second)
        assert len(calls) == 2
        assert owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE id=?', (calls[-1].run_id,)).fetchone() == (2,)
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        runtime = replace(runtime, owned=owned, natural_wait_states={}, timelapse_deadlines={})
        runtime.resume_file_observations(aid)
        assert all(value == requests[0] for value in requests)
    _register_listing(runtime, aid, second)
    if not result_first:
        _finish_listing_result(runtime, second, retry_wait=True)
    facts = owned.connection.execute('SELECT id,completion_state,completion_evidence_json,size_bytes,original_name,media_type FROM device_files').fetchone()
    complete_id, = owned.connection.execute(
        "SELECT MIN(e.id) FROM history_events e,json_each(e.body_json,'$.rows') r"
        " WHERE json_extract(r.value,'$.table')='device_files' AND json_extract(r.value,'$.id')=?"
        " AND json_extract(r.value,'$.after.values.completion_state')=3"
        " AND json_extract(r.value,'$.before.values.completion_state')<>3", (facts[0],)).fetchone()
    intent_id, result_id = owned.connection.execute(
        'SELECT intent_event_id,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=2',
        (calls[-1].run_id,)).fetchone()
    assert intent_id < complete_id and (result_id < complete_id) is result_first
    return owned, runtime, aid, advance, wall, port, calls, facts, completed


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('size', [0, 50])
@pytest.mark.parametrize('opaque', [False, True])
@pytest.mark.parametrize('result_first', [False, True], ids=['file-first', 'result-first'])
async def test_legacy_retention_uses_complete_attempt_metadata(tmp_path, startup, canceled, size, opaque, result_first):
    owned, runtime, aid, advance, wall, port, calls, facts, completed = await _completed_world(
        tmp_path, startup, size, opaque, result_first)
    try:
        source = runtime.capture.read_result_sources(calls[-1], None, 1, owned)
        entry, file, identity = source.items[0]
        assert (entry.kind.value, entry.format_id, entry.original_name, entry.complete, entry.size_bytes) == (
            'video', 'osv', 'completed.OSV', True, size)
        assert identity == facts[0] and file.complete
        third = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, third)
        _finish_listing_result(runtime, third, retry_wait=True)
        if canceled:
            _apply_public_cancel(owned, aid, wall[0])
            _stopper(runtime, aid, 'failed')
        for _ in range(3):
            await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        outputs = owned.connection.execute('SELECT id,device_file_id FROM outputs').fetchall()
        assert [value[1] for value in outputs] == ([facts[0]] if size > 0 else [])
        source = runtime.capture.read_result_sources(calls[-1], None, 1, owned)
        assert source.items[0][0].original_name == 'completed.OSV'
        assert owned.connection.execute('SELECT id,completion_state,completion_evidence_json,size_bytes,original_name,media_type FROM device_files').fetchone() == facts
        assert port.list_round.await_count == 3
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        if canceled:
            await _finish_cancel(owned, wall, False)
        await advance(aid, runtime)
        assert owned.connection.execute('SELECT id,device_file_id FROM outputs').fetchall() == outputs
        assert port.list_round.await_count == 3
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('result_first', [False, True])
@pytest.mark.parametrize('save_mode', ['unknown-before', 'unknown-after'])
async def test_legacy_complete_metadata_recovers_original_unknown_key(tmp_path, startup, result_first, save_mode):
    owned, runtime, aid, advance, wall, port, calls, facts, completed = await _completed_world(
        tmp_path, startup, 50, True, result_first, save_mode)
    try:
        source = runtime.capture.read_result_sources(calls[-1], None, 1, owned)
        assert (source.items[0][0].kind.value, source.items[0][0].format_id, source.items[0][0].original_name) == ('video', 'osv', 'completed.OSV')
        assert port.list_round.await_count == 2
        assert owned.connection.execute('SELECT id,completion_state,completion_evidence_json,size_bytes,original_name,media_type FROM device_files').fetchone() == facts
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('change', ['missing_f', 'f_size', 'f_proof', 'source_incomplete', 'source_size', 'source_missing', 'missing_intent'])
async def test_legacy_complete_source_conflict_diagnoses_without_rewriting_facts(tmp_path, startup, change):
    import json
    owned, runtime, aid, advance, wall, port, calls, facts, completed = await _completed_world(
        tmp_path, startup, 50, True, False)
    try:
        file_id = facts[0]
        event_id, body = owned.connection.execute(
            "SELECT e.id,e.body_json FROM history_events e,json_each(e.body_json,'$.rows') r"
            " WHERE json_extract(r.value,'$.table')='device_files' AND json_extract(r.value,'$.id')=?"
            " AND json_extract(r.value,'$.after.values.completion_state')=3"
            " AND json_extract(r.value,'$.before.values.completion_state')<>3 ORDER BY e.id LIMIT 1", (file_id,)).fetchone()
        if change == 'missing_f':
            owned.connection.execute('DELETE FROM history_events WHERE id=?', (event_id,))
        elif change.startswith('f_'):
            body = json.loads(body)
            values = next(row['after']['values'] for row in body['rows'] if row['table'] == 'device_files')
            if change == 'f_size': values['size_bytes'] = 51
            else: values['completion_evidence_json']['observation'] = {'changed': True}
            owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?', (json.dumps(body), event_id))
        else:
            attempt_id, intent, result_id, raw = owned.connection.execute(
                'SELECT id,intent_event_id,result_event_id,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=2', (calls[-1].run_id,)).fetchone()
            if change == 'missing_intent':
                owned.connection.execute('DELETE FROM history_events WHERE id=?', (intent,))
            else:
                raw = json.loads(raw)
                entries = raw['observations'][0]['data']['entries']
                if change == 'source_incomplete': entries[0].update(complete=False, size_bytes=None)
                elif change == 'source_size': entries[0]['size_bytes'] = 51
                else: entries.clear()
                event_body = json.loads(owned.connection.execute('SELECT body_json FROM history_events WHERE id=?', (result_id,)).fetchone()[0])
                next(row['after']['values'] for row in event_body['rows'] if row['table'] == 'operation_attempts')['result_json'] = raw
                owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?', (json.dumps(event_body), result_id))
                owned.connection.execute('UPDATE operation_attempts SET result_json=? WHERE id=?', (json.dumps(raw), attempt_id))
        with pytest.raises(ConsistencyError):
            runtime.capture.read_result_sources(calls[-1], None, 1, owned)
        assert owned.connection.execute('SELECT id,completion_state,completion_evidence_json,size_bytes,original_name,media_type FROM device_files').fetchone() == facts
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert port.list_round.await_count == 2
    finally:
        owned.connection.close()
