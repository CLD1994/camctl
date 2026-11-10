"""C3 录像完成终态与正式产物登记的组件集成测试。

真实 SQLite 与 P3 事务内核组合：动作成功终态、正式产物登记与父
计划状态同事务保存，任一写入失败整组回滚；停止预算跨普通、取消
及恢复入口共用原责任；已终态动作不被改写。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.transactions import TransactionError
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture, FinishCapture, FinishDisposition
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION,
    DbConfig,
    DbOpenMode,
    open_existing,
)

from ..persistence.test_runtime import (
    _create_legacy_v1_database,
    _create_valid_database,
)


_NOW = 1_750_000_000_000_000


def _environment(
    tmp_path: Path,
    *,
    format_version: int = CURRENT_FORMAT_VERSION,
    legacy_v1_schema: bool = False,
):
    target = tmp_path / "state.db"
    if legacy_v1_schema:
        _create_legacy_v1_database(target, format_version=format_version)
    else:
        _create_valid_database(target, format_version=format_version)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    _seed_plan(connection, 1)
    _seed_action(connection, 1, 1)
    _seed_device_file(connection, 11)
    _seed_device_file(connection, 12, role=3)
    _seed_stop_flow(connection)
    _seed_activity(connection)
    connection.commit()
    return owned


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, 4242, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, plan_id: int,
                 *, cancel_requested: int = 0) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 0, 'rec', 2, 'cam-1', ?, NULL, '{}',"
        " '{\"target_duration_s\": 60}', 'camctl-adb', 1000, '{}', 2, 1, ?, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, _NOW, cancel_requested),
    )


def _seed_device_file(connection, file_id: int, *, role: int = 2) -> None:
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, 1, ?, '{}', '{}', 'video.mp4', 'video/mp4', ?, NULL, NULL,"
        " 2, 3, '{}', 1024, 3, NULL, NULL, 1, 1, 1)",
        (file_id, f"file-{file_id:04d}", role),
    )


def _seed_activity(connection) -> None:
    connection.execute(
        "INSERT INTO device_activities (id, action_id, task_key, task_locator_json,"
        " state_query_supported, stop_supported, safe_repeat_stop,"
        " start_return_meaning, completion_mode, ownership_mode, output_scope_json,"
        " baseline_state, baseline_first_event_id, baseline_last_event_id,"
        " dispatch_state, activity_state, occupancy_state, sent_at, started_at,"
        " result_wait_margin_ms, extra_wait_ms_used, expected_check_at,"
        " wait_completed_event_id, capture_json, control_elapsed_ns,"
        " completion_basis, completion_evidence_json, result_set_state,"
        " last_error_json)"
        " VALUES (1, 1, ?, NULL, 1, 1, 1, 1, 1, 1, '{}', 1, NULL, NULL, 2, 2, 1,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL)",
        (f"{1:032x}",),
    )


def _seed_stop_flow(connection) -> None:
    """预建空停止流程（责任键 stop/1，未尝试），供预算共用验证。"""
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (30, 1, NULL, 2, NULL, 'stop/1', 1, NULL, NULL, NULL, 1, 0, 3,"
        " '10', '1', 0, NULL)"
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _original_draft(file_id: int, *, complete: bool = True) -> OutputDraft:
    return OutputDraft(
        kind=OutputKind.ORIGINAL,
        file=FileReference(device_file_id=file_id),
        file_complete=complete,
        sha256=None,
    )


def _finish(action_id: int = 1, drafts=None) -> FinishCapture:
    return FinishCapture(
        action_id=action_id,
        drafts=drafts if drafts is not None else (_original_draft(11),),
        catalog_facts=OutputCatalogFacts(action_id=action_id, ownership_confirmed=True),
        occurred_at=_NOW,
    )


pytestmark = pytest.mark.asyncio


from ..support.recording_consumer_cases import (
    NOW, RecordingCase, capture_request, finish_case, with_capture, recording_cases,
)


async def _save_finish(case, method, request, key=None):
    return await case.write(getattr(CaptureRepository(), method), request, key)


def _image(case):
    return tuple(case.owned.connection.iterdump())


@pytest.mark.parametrize('has_pending_sibling,expected_plan_status', [(False, 3), (True, 2)])
async def test_finish_registers_outputs_action_and_plan_together(
    tmp_path: Path, has_pending_sibling: bool, expected_plan_status: int,
) -> None:
    report_at = datetime.fromtimestamp(NOW // 1_000_000 + 3600, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    extra_actions = ({'name': '未来报告', 'type': 'report_status',
        'scheduled_at': report_at, 'params': {'scope': 'full'}},) if has_pending_sibling else ()
    case = await finish_case(tmp_path, extra_actions=extra_actions)
    try:
        method, request, key = await case.finish_input()
        action_id = capture_request(request).action_id
        plan_id = case.row('SELECT plan_id FROM actions WHERE id=?', action_id)[0]
        members_before = case.owned.connection.execute(
            'SELECT id,name,status,error_code,execution_started FROM actions WHERE plan_id=? ORDER BY input_index',
            (plan_id,)).fetchall()
        assert members_before[0] == (action_id, '拍摄', 2, None, 1)
        assert len(members_before) == 1 + has_pending_sibling
        if has_pending_sibling:
            sibling_id = members_before[1][0]
            assert members_before[1] == (sibling_id, '未来报告', 1, None, 0)
            assert case.row('SELECT scheduled_at FROM actions WHERE id=?', sibling_id)[0] > case.wall[0]
        assert case.row('SELECT status FROM plans WHERE id=?', plan_id) == (2,)
        boundary = case.row('SELECT MAX(id) FROM history_events')[0]
        outcome = await _save_finish(case, method, request, key)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        result = outcome.value
        assert (result.action_status, result.plan_status) == (3, expected_plan_status)
        file_id = capture_request(request).drafts[0].file.device_file_id
        assert case.row('SELECT kind,device_file_id,availability,cleanup_status,source_action_id'
            ' FROM outputs WHERE id=?', result.output_ids[0]) == (1, file_id, 1, 1, action_id)
        members_after = case.owned.connection.execute(
            'SELECT id,name,status,error_code,execution_started FROM actions WHERE plan_id=? ORDER BY input_index',
            (plan_id,)).fetchall()
        assert members_after == [(action_id, '拍摄', 3, None, 1), *members_before[1:]]
        assert case.row('SELECT status FROM plans WHERE id=?', plan_id) == (expected_plan_status,)
        txns = case.owned.connection.execute(
            'SELECT DISTINCT transaction_id FROM history_events WHERE id>?', (boundary,)).fetchall()
        assert len(txns) == 1
        assert case.stop_calls == ['stop_recording']
    finally:
        await case.close()


async def test_invalid_draft_rolls_back_whole_group(tmp_path: Path) -> None:
    """真实原页的两份文件中，一项草稿不完整则整组拒绝。"""
    case = await finish_case(tmp_path, second_file=True)
    try:
        method, request, _key = await case.finish_input()
        capture = capture_request(request)
        assert len(capture.drafts) == 2
        invalid = with_capture(request, replace(capture, drafts=(
            capture.drafts[0], replace(capture.drafts[1], file_complete=False))))
        before = _image(case)
        outcome = await _save_finish(case, method, invalid)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _image(case) == before
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
        assert case.row('SELECT status FROM actions WHERE id=1') == (2,)
        assert case.row('SELECT status FROM plans WHERE id=1') == (2,)
    finally:
        await case.close()


async def test_stop_uses_original_budget_across_cancel_and_restart(tmp_path: Path) -> None:
    """正常、已生效取消和实际 worker 重开沿用同一个停止流程。"""
    case = await RecordingCase(tmp_path, stop_failures=3).start()
    stages = []
    try:
        case.wall[0], case.mono[0] = NOW + 60_000_000, 65_000_000_000
        await case.advance()
        stages.append(('normal', case.row('SELECT id,attempts_used FROM operation_runs'
            " WHERE responsibility_key='stop/1'")))
        await case.cancel()
        case.wall[0] += 2_000_000
        case.mono[0] += 2_000_000_000
        await case.advance()
        stages.append(('cancel', case.row('SELECT id,attempts_used FROM operation_runs'
            " WHERE responsibility_key='stop/1'")))
        await case.restart(wall=case.wall[0] + 2_000_000)
        await case.advance()
        assert len(case.stop_calls) == 2
        case.wall[0] += 1_000_000
        case.mono[0] += 1_000_000_000
        await case.advance()
        stages.append(('restart', case.row('SELECT id,attempts_used FROM operation_runs'
            " WHERE responsibility_key='stop/1'")))
        facts = {'stages': stages, 'calls': case.driver.calls,
            'actions': case.owned.connection.execute('SELECT id,status,cancel_requested,error_code,error_details_json FROM actions').fetchall(),
            'runs': case.owned.connection.execute('SELECT id,responsibility_key,status,attempts_used,max_attempts_used,error_json FROM operation_runs').fetchall(),
            'attempts': case.owned.connection.execute('SELECT run_id,attempt_no,status,effect_state,result_json,error_json FROM operation_attempts').fetchall()}
        (tmp_path / 'stop-budget-facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2))
        assert len({value[0] for _stage,value in stages}) == 1
        assert [value[1] for _stage,value in stages] == [1, 2, 3]
        failures = case.owned.connection.execute(
            "SELECT json_extract(error_json,'$.code'),json_extract(error_json,'$.stage')"
            " FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (stages[0][1][0],)).fetchall()
        assert failures == [('transport_timeout', 'transport')] * 3
        assert case.stop_calls == ['stop_recording'] * 3
        case.wall[0] += 2_000_000
        case.mono[0] += 2_000_000_000
        await case.advance()
        assert case.stop_calls == ['stop_recording'] * 3
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
    finally:
        await case.close()


async def test_canceled_finish_ends_action_without_outputs(tmp_path: Path) -> None:
    case = await finish_case(tmp_path, complete_files=False, canceled=True)
    try:
        method, request, key = await case.finish_input()
        assert request.drafts == ()
        outcome = await _save_finish(case, method, request, key)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert (outcome.value.action_status, outcome.value.plan_status, outcome.value.output_ids) == (6, 3, ())
        assert case.row('SELECT status FROM actions WHERE id=1') == (6,)
        assert case.row('SELECT status FROM plans WHERE id=1') == (3,)
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
    finally:
        await case.close()


async def test_canceled_finish_requires_saved_cancel_mark(tmp_path: Path) -> None:
    case = await finish_case(tmp_path, complete_files=False)
    try:
        before = _image(case)
        outcome = await _save_finish(case, 'finish_canceled_capture', FinishCanceledCapture(1, NOW))
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert case.row('SELECT status,cancel_requested FROM actions WHERE id=1') == (2, 0)
        assert _image(case) == before
    finally:
        await case.close()


@pytest.mark.parametrize('pending_sibling', [False, True], ids=['last-action', 'pending-sibling'])
async def test_canceled_finish_reuse_and_recovery(tmp_path: Path, pending_sibling: bool) -> None:
    report_at = datetime.fromtimestamp(NOW // 1_000_000 + 3600, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    extra_actions = ({'name': '未来报告', 'type': 'report_status',
        'scheduled_at': report_at, 'params': {'scope': 'full'}},) if pending_sibling else ()
    case = await finish_case(tmp_path, complete_files=False, canceled=True, extra_actions=extra_actions)
    try:
        method, command, key = await case.finish_input()
        assert method == 'finish_canceled_capture'
        plan_before = case.row('SELECT status,last_event_id FROM plans WHERE id=1')
        boundary = case.row('SELECT MAX(id) FROM history_events')[0]
        first = await _save_finish(case, method, command, key)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        plan_after = case.row('SELECT status,last_event_id FROM plans WHERE id=1')
        expected_plan = plan_before[0] if pending_sibling else 3
        assert (first.value.action_status, first.value.plan_status) == (6, expected_plan)
        if pending_sibling:
            assert plan_after == plan_before
            assert case.row("SELECT status,execution_started FROM actions WHERE name='未来报告'") == (1, 0)
        else:
            events = case.owned.connection.execute(
                'SELECT event_type,transaction_id FROM history_events WHERE id IN ('
                'SELECT last_event_id FROM actions WHERE id=1 UNION ALL SELECT last_event_id FROM plans WHERE id=1)'
            ).fetchall()
            assert {event[0] for event in events} == {8, 9}
            assert len({event[1] for event in events}) == 1
        assert len(case.owned.connection.execute(
            'SELECT DISTINCT transaction_id FROM history_events WHERE id>?', (boundary,)).fetchall()) == 1
        await case.restart(wall=NOW + 100_000_000)
        before = _image(case)
        for resend_key, request in ((key, command), (new_operation_key(), command),
            (new_operation_key(), replace(command, occurred_at=command.occurred_at + 1))):
            again = await _save_finish(case, method, request, resend_key)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert again.value.disposition is FinishDisposition.ALREADY
            assert (again.value.action_status, again.value.plan_status, again.value.output_ids) == (6, expected_plan, ())
            assert _image(case) == before
    finally:
        await case.close()


async def _complete_files_then_cancel(tmp_path: Path, consumer: str):
    case = await finish_case(tmp_path, consumer=consumer, canceled=True)
    assert case.row('SELECT source_action_id,presence_state,completion_state,size_bytes'
        ' FROM device_files WHERE id=1') == (1, 2, 3, 4096)
    assert case.row('SELECT status,cancel_requested FROM actions WHERE id=1') == (2, 1)
    assert case.results.delegate.calls == [1]
    return case


async def test_canceled_finish_registers_complete_files(tmp_path: Path) -> None:
    """延时摄影取消保留真实原页的完整文件，并与终态同事务登记。"""
    case = await _complete_files_then_cancel(tmp_path, 'timelapse')
    try:
        method, command, key = await case.finish_input()
        assert len(command.drafts) == 1
        boundary = case.row('SELECT MAX(id) FROM history_events')[0]
        outcome = await _save_finish(case, method, command, key)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert (outcome.value.action_status, outcome.value.plan_status) == (6, 3)
        assert len(outcome.value.output_ids) == 1
        assert case.row('SELECT kind,device_file_id,availability,source_action_id FROM outputs WHERE id=?',
            outcome.value.output_ids[0]) == (1, 1, 1, 1)
        assert case.row('SELECT status FROM actions WHERE id=1') == (6,)
        assert len(case.owned.connection.execute('SELECT DISTINCT transaction_id FROM history_events WHERE id>?',
            (boundary,)).fetchall()) == 1
        before = _image(case)
        for resend in (key, new_operation_key()):
            again = await _save_finish(case, method, command, resend)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert again.value.disposition is FinishDisposition.ALREADY
            assert again.value.output_ids == outcome.value.output_ids
            assert _image(case) == before
        assert case.results.delegate.calls == [1]
        discard = await _save_finish(case, method,
            FinishCanceledCapture(1, command.occurred_at + 1))
        assert discard.kind is DbOutcomeKind.ROLLED_BACK
        assert _image(case) == before
    finally:
        await case.close()


async def test_canceled_record_finish_discards_complete_files(tmp_path: Path) -> None:
    """录像取消拒绝产物草稿，保留已经可靠保存的完整文件和原页。"""
    case = await _complete_files_then_cancel(tmp_path, 'record')
    try:
        method, command, key = await case.finish_input()
        files = case.owned.connection.execute('SELECT * FROM device_files ORDER BY id').fetchall()
        before = _image(case)
        rejected = await _save_finish(case, method, replace(command,
            drafts=(_original_draft(1),), catalog_facts=OutputCatalogFacts(1, ownership_confirmed=True)))
        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        assert type(rejected.error) is TransactionError
        assert '录像取消' in str(rejected.error) and '不登记正式产物' in str(rejected.error)
        assert _image(case) == before
        assert case.row('SELECT status FROM actions WHERE id=1') == (2,)
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
        result = await _save_finish(case, method, command, key)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert (result.value.action_status, result.value.output_ids) == (6, ())
        assert case.row('SELECT status FROM actions WHERE id=1') == (6,)
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
        assert case.owned.connection.execute('SELECT * FROM device_files ORDER BY id').fetchall() == files
        assert case.results.delegate.calls == [1]
    finally:
        await case.close()


@pytest.mark.parametrize('consumer', ['record', 'timelapse'])
async def test_canceled_finish_rejects_incomplete_draft(tmp_path: Path, consumer) -> None:
    """取消录像禁止草稿；允许保留文件的延时分支也拒绝不完整草稿。"""
    case = await finish_case(tmp_path, consumer=consumer, canceled=True)
    try:
        method, command, _key = await case.finish_input()
        before = _image(case)
        outcome = await _save_finish(case, method, replace(command,
            drafts=(_original_draft(1, complete=False),),
            catalog_facts=OutputCatalogFacts(1, ownership_confirmed=True)))
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        if consumer == 'record':
            assert type(outcome.error) is TransactionError
            assert '录像取消' in str(outcome.error) and '不登记正式产物' in str(outcome.error)
        else:
            assert type(outcome.error) is ValueError
            assert '文件尚未完成' in str(outcome.error)
            assert 'device_file_id=1' in str(outcome.error)
        assert case.row('SELECT COUNT(*) FROM outputs') == (0,)
        assert case.row('SELECT status FROM actions WHERE id=1') == (2,)
        assert _image(case) == before
    finally:
        await case.close()
