"""范围清理从真实受理进入执行时，先解析并保存来源。"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.contracts.values import new_operation_key
from camctl.outputs.cleanup_flow import StartCleanupAction
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_cleanup_flow import (
    _Catalog, _CleanupDriver, _cleanup_plan, _config, _photo_plan,
    _registry, _run_photo_session, _scalar, _submit,
)
from .test_obtain_flow import _await_query, _future_schedule


pytestmark = pytest.mark.asyncio


async def _complete_cleanup(tmp_path, cfg, driver):
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    # 只隔离设备登记；清理推进、文件维护及报告子进程使用正式装配。
    with patch('camctl.devices.drivers.runtime.current_registry', return_value=_registry(driver)):
        try:
            outcome = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.01), 10)
            assert outcome.succeeded, (outcome.reason, outcome.details)
        finally:
            close_runtime(deps)
    reports = list(Path(cfg.paths.ready).glob('status-report-*.json'))
    assert len(reports) == 1
    return json.loads(reports[0].read_bytes())


@pytest.mark.parametrize('source_field', ['plan_instance_id', 'action_instance_id'])
async def test_cross_instance_cleanup_resolves_source_before_targets(tmp_path, source_field):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1'))
    output_id = await _run_photo_session(tmp_path, cfg)
    source_plan, source_action = _scalar(db, 'SELECT plan_id,id FROM actions WHERE name=\'shoot\'')
    identity = source_plan if source_field == 'plan_instance_id' else source_action
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'source': {source_field: str(identity)}}))
    assert _scalar(db, 'SELECT source_resolution_state FROM actions WHERE type=5') == (1,)

    driver = _CleanupDriver()
    report = await _complete_cleanup(tmp_path, cfg, driver)

    assert _scalar(db, 'SELECT status,source_resolution_state,resolved_source_plan_id,target_selection_state FROM actions WHERE type=5') == (3, 2, source_plan, 2)
    assert _scalar(db, 'SELECT availability,cleanup_status FROM outputs WHERE id=?', output_id) == (3, 4)
    assert len(driver.delete_calls) == 1
    assert _scalar(db, 'SELECT COUNT(*) FROM obtain_source_selections') == (0,)
    reported = [action for plan in report['plans'] for action in plan['actions']
                if action['type'] == 'delete_action_outputs']
    assert len(reported) == 1 and reported[0]['status'] == 'succeeded'
    with sqlite3.connect(db) as connection:
        events = [(event_id, event_type, json.loads(body))
                  for event_id, event_type, body in connection.execute(
                      'SELECT id,event_type,body_json FROM history_events '
                      'WHERE event_type IN (3,4) ORDER BY id')]
        # 来源成员与唯一末固定事件共同提交，之后才允许固定清理目标。
        assert [(kind, body['reason']) for _, kind, body in events] == [(3, 1), (3, 1), (4, 2)]
        members, fixed, targets = events
        header = members[2]['evidence']['source_resolution_request']
        assert fixed[2]['evidence']['source_resolution_request'] == header
        cleanup_action, = connection.execute('SELECT id FROM actions WHERE type=5').fetchone()
        assert header['action_id'] == cleanup_action
        assert header['source'] == {source_field: identity}
        assert header['member_count'] == 1
        assert header['source_plan_id'] == source_plan
        assert (header['first_event_id'], header['last_event_id']) == (members[0], fixed[0])
        assert connection.execute(
            'SELECT first_event_id,last_event_id FROM history_transactions WHERE operation_key=?',
            (header['operation_key'],),
        ).fetchone() == (members[0], fixed[0])
    member, = members[2]['rows']
    assert member['table'] == 'action_dependencies'
    assert member['before'] == {'exists': False, 'values': {}}
    assert member['after'] == {'exists': True, 'values': {
        'action_id': cleanup_action, 'depends_on_action_id': source_action,
    }}
    assert fixed[2]['rows'] == [{
        'table': 'actions', 'id': cleanup_action,
        'before': {'exists': True, 'values': {
            'source_resolution_state': 1, 'resolved_source_plan_id': None,
        }},
        'after': {'exists': True, 'values': {
            'source_resolution_state': 2, 'resolved_source_plan_id': source_plan,
        }},
    }]
    assert targets[0] > fixed[0]


@pytest.mark.parametrize('source_field', ['plan_instance_id', 'action_instance_id'])
async def test_missing_cleanup_source_is_action_failure(tmp_path, source_field):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _cleanup_plan('1', {'source': {source_field: '999'}}))
    driver = _CleanupDriver()

    await _complete_cleanup(tmp_path, cfg, driver)

    assert _scalar(db, 'SELECT status,source_resolution_state,error_code FROM actions WHERE type=5') == (4, 3, 20)
    assert _scalar(db, 'SELECT status FROM plans') == (3,)
    assert _scalar(db, 'SELECT COUNT(*) FROM cleanup_items') == (0,)
    assert driver.delete_calls == []


@pytest.mark.parametrize('source_has_capture', [False, True])
async def test_empty_scope_cleanup_succeeds(tmp_path, source_has_capture):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    if source_has_capture:
        plan = _photo_plan('1')
        plan['actions'][0]['params'] = {'unexpected': True}
    else:
        plan = {'request_id': '1', 'created_at': '2026-01-15 08:00:00',
                'name': '没有拍摄来源', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
                                                  'params': {'target': {'request_id': '999'}}}]}
    await _submit(tmp_path, cfg, plan)
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'source': {'plan_instance_id': '1'}}))
    driver = _CleanupDriver()

    await _complete_cleanup(tmp_path, cfg, driver)

    assert _scalar(db, 'SELECT status,source_resolution_state,resolved_source_plan_id,target_selection_state FROM actions WHERE type=5') == (3, 2, 1, 2)
    assert _scalar(db, 'SELECT COUNT(*) FROM cleanup_items') == (0,)
    assert driver.delete_calls == []


async def test_running_cleanup_resumes_pending_source_without_new_request(tmp_path):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1'))
    await _run_photo_session(tmp_path, cfg)
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'source': {'plan_instance_id': '1'}}))
    action_id, = _scalar(db, 'SELECT id FROM actions WHERE type=5')
    owned = open_existing(db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        started = OutputsRepository().start_cleanup_action(
            StartCleanupAction(action_id, time.time_ns() // 1000), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
    finally:
        owned.connection.close()
    assert _scalar(db, 'SELECT status,source_resolution_state FROM actions WHERE type=5') == (2, 1)
    driver = _CleanupDriver()

    await _complete_cleanup(tmp_path, cfg, driver)

    assert _scalar(db, 'SELECT status,source_resolution_state FROM actions WHERE id=?', action_id) == (3, 2)
    assert _scalar(db, 'SELECT COUNT(*) FROM plans') == (2,)
    assert len(driver.delete_calls) == 1


async def test_cleanup_waits_for_unfinished_source_and_can_be_canceled(tmp_path):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1', scheduled_at=_future_schedule(3600)))
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'source': {'plan_instance_id': '1'}}))
    driver = _CleanupDriver()
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    with patch('camctl.devices.drivers.runtime.current_registry', return_value=_registry(driver)):
        task = asyncio.create_task(execute_command(deps, None, poll_interval_s=0.01))
        try:
            await _await_query(db, 'SELECT source_resolution_state,target_selection_state FROM actions WHERE type=5', (2, 1))
            assert _scalar(db, 'SELECT COUNT(*) FROM cleanup_items') == (0,)
            assert driver.delete_calls == []
            await _submit(tmp_path, cfg, {'request_id': '3', 'created_at': '2026-01-15 08:00:00',
                'name': '取消等待中的清理', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
                    'params': {'target': {'request_id': '2'}}}]})
            await _await_query(db, 'SELECT status,cancel_requested FROM actions WHERE type=5', (6, 1))
            assert _scalar(db, 'SELECT COUNT(*) FROM cleanup_items') == (0,)
            assert _scalar(db, 'SELECT source_resolution_state FROM actions WHERE type=5') == (2,)
            assert driver.delete_calls == [] and driver.control_calls == []
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            close_runtime(deps)
