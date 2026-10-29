"""延时等待实际处理入口的重配置、锚点与会话计时。"""

import pytest

from camctl.capture.models import WaitCompletedSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind

from .test_capture_contract import _NOW
from .timelapse_wait_fixtures import wait_case
from ..support.result_cases import result_case

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("extra", [0, 5000])
async def test_new_session_reconfigures_unfinished_wait_from_original_sent_at(tmp_path, wait_case, extra):
    owned, runtime, wall, mono, current_extra = await wait_case.world(tmp_path,
        wall=[_NOW], mono=[10_000_000_000], extra=[1000], advance=True)
    try:
        await wait_case.reopen(wall=[_NOW + 100_000], mono=[20_000_000_000], extra=[extra])
        await wait_case.drive()
        row = owned.connection.execute("SELECT sent_at,result_wait_margin_ms,extra_wait_ms_used,expected_check_at FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone()
        assert row == (_NOW, 200, extra, _NOW + (1200 + extra) * 1000)
        assert wait_case.calls == []
        before = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        await wait_case.drive()
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == before
    finally:
        owned.connection.close()


async def test_completed_wait_is_not_reopened_by_new_extra_wait(tmp_path, wait_case):
    from camctl.persistence.repositories.timelapse import TimelapseRepository
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path,
        mono=[10_000_000_000], extra=[1000], advance=True)
    try:
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: TimelapseRepository().complete_wait(
            WaitCompletedSave(wait_case.aid, _NOW + 2_200_000), key, current), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        await wait_case.reopen(wall=[_NOW + 2_200_000], mono=[30_000_000_000], extra=[9000])
        await wait_case.drive()
        row = owned.connection.execute("SELECT extra_wait_ms_used,expected_check_at,wait_completed_event_id FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone()
        assert row == (1000, _NOW + 2_200_000, saved.value.event_id)
        assert wait_case.results.calls == [wait_case.aid]
    finally:
        owned.connection.close()


async def test_forward_wall_clock_change_does_not_finish_current_session_wait(tmp_path, wait_case):
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path,
        mono=[10_000_000_000], extra=[1000], advance=True)
    try:
        wall[0] += 100_000_000
        mono[0] += 100_000_000
        await wait_case.drive()
        assert wait_case.results.calls == []
        assert owned.connection.execute("SELECT wait_completed_event_id FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (None,)
    finally:
        owned.connection.close()


async def test_backward_wall_clock_change_preserves_deadline_and_defers_completion(tmp_path, wait_case):
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path,
        mono=[10_000_000_000], extra=[1000], advance=True)
    try:
        wall[0] -= 100_000_000
        mono[0] += 2_200_000_000
        await wait_case.drive()
        assert wait_case.deadline == 12200000000
        assert wait_case.results.calls == []
        assert owned.connection.execute("SELECT wait_completed_event_id FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (None,)
        wall[0] = _NOW + 2_200_000
        await wait_case.drive()
        assert wait_case.results.calls == [wait_case.aid]
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (wait_case.aid,)).fetchone() == (3,)
    finally:
        owned.connection.close()


async def test_send_return_anchor_precedes_result_persistence_delay(tmp_path, wait_case, monkeypatch):
    from camctl.persistence.repositories.capture import CaptureRepository
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path,
        mono=[10_000_000_000], extra=[1000])
    case = wait_case.case
    response = case.driver.control.return_value
    async def delayed_control(request):
        wall[0] += 200_000
        mono[0] += 200_000_000
        return response
    save = CaptureRepository.finish_start_result
    def delayed_save(repository, *args, **kwargs):
        receipt = save(repository, *args, **kwargs)
        wall[0] += 5_000_000
        mono[0] += 5_000_000_000
        return receipt
    case.driver.control.side_effect = delayed_control
    monkeypatch.setattr(CaptureRepository, "finish_start_result", delayed_save)
    try:
        await wait_case.drive()
        assert owned.connection.execute("SELECT sent_at,expected_check_at FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (_NOW + 200_000, _NOW + 2_400_000)
        await wait_case.drive()
        assert wait_case.results.calls == [wait_case.aid]
    finally:
        owned.connection.close()


async def test_new_natural_wait_uses_frozen_startup_and_reconfigures_current_deadline(tmp_path, wait_case):
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path,
        startup=1000, duration_ms=300000, margin_ms=30000, advance=True)
    try:
        assert owned.connection.execute("SELECT expected_check_at FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (_NOW + 331000000,)
        wall[0] += 330000000
        mono[0] += 330000000000
        await wait_case.drive()
        assert wait_case.results.calls == []
        extra[0] = 2000
        await wait_case.drive()
        assert wait_case.deadline == 338000000000
        assert owned.connection.execute("SELECT expected_check_at,extra_wait_ms_used FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (_NOW + 333000000, 2000)
        wall[0] += 1000000
        mono[0] += 1000000000
        await wait_case.drive()
        assert wait_case.results.calls == []
        wall[0] += 2000000
        mono[0] += 2000000000
        await wait_case.drive()
        assert wait_case.results.calls == [wait_case.aid]
    finally:
        owned.connection.close()


@pytest.mark.parametrize('offset_ms', [0, 2000])
async def test_wait_repository_independently_rejects_missing_or_duplicate_startup_margin(tmp_path, wait_case, offset_ms):
    from camctl.capture.timelapse import WaitPlan, WaitKind
    from camctl.persistence.repositories.timelapse import ScheduleWait, TimelapseRepository
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=1000, advance=True)
    try:
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: TimelapseRepository().reconfigure_wait(
            ScheduleWait(wait_case.aid, WaitPlan(WaitKind.WAIT_THEN_CHECK,
                check_at_utc=_NOW + (1200 + offset_ms) * 1000), 200, 0, _NOW), key, current), key=key)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


@pytest.mark.parametrize('failure', ['rollback', 'unknown_before_commit', 'unknown_after_commit'])
async def test_reconfigure_rollback_does_not_add_extra_wait_twice(tmp_path, wait_case, failure):
    import sqlite3
    from dataclasses import replace
    from .test_record_media_result_settlement import _TrackedCommitFailure
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=1000, advance=True)
    save = wait_case.save("reconfigure_wait")
    def fail_save(command, key, current):
        if failure == 'rollback':
            def authorize(operation, table, _column, _database, _source):
                return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_UPDATE and table == 'device_activities' else sqlite3.SQLITE_OK
            current.connection.set_authorizer(authorize)
            try:
                receipt = save(command, key, current)
            finally:
                current.connection.set_authorizer(None)
        else:
            fault = _TrackedCommitFailure(current.connection, failure == 'unknown_after_commit')
            receipt = save(command, key, replace(current, connection=fault))
            assert fault.commit_calls == 1 and receipt.kind is DbOutcomeKind.UNKNOWN
        return receipt
    try:
        extra[0] = 2000
        wait_case.hooks['reconfigure_wait'] = fail_save
        with pytest.raises(ConsistencyError):
            await wait_case.drive()
        del wait_case.hooks['reconfigure_wait']
        await wait_case.drive()
        assert wait_case.deadline == 9200000000
        assert owned.connection.execute("SELECT expected_check_at FROM device_activities WHERE action_id=?", (wait_case.aid,)).fetchone() == (_NOW + 4200000,)
        assert wait_case.calls == ['start_timelapse']
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_overdue_restart_with_unsaved_completion_reconfigures_from_original_anchor(tmp_path, wait_case):
    import sqlite3
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=1000, advance=True)
    await wait_case.reopen(wall=[_NOW + 3000000], mono=[10000000000])
    complete = wait_case.save('complete_wait')
    def rejected(command, key, current):
        def authorize(operation, table, _column, _database, _source):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_UPDATE and table == 'device_activities' else sqlite3.SQLITE_OK
        current.connection.set_authorizer(authorize)
        try:
            return complete(command, key, current)
        finally:
            current.connection.set_authorizer(None)
    try:
        wait_case.hooks['complete_wait'] = rejected
        with pytest.raises(ConsistencyError):
            await wait_case.drive()
        del wait_case.hooks['complete_wait']
        extra[0] = 2000
        await wait_case.drive()
        assert wait_case.deadline == 11200000000
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('failure', ['before_commit', 'after_commit', 'uncommitted_projection'])
async def test_unknown_schedule_retains_original_key_and_publishes_only_reliable_arrangements(tmp_path, wait_case, startup, failure):
    from camctl.persistence.models import DbOutcome
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    save = wait_case.save('reconfigure_wait')
    calls, fault = [], [True]
    def uncertain(command, key, current):
        calls.append((command, key))
        if fault[0]:
            fault[0] = False
            if failure == 'after_commit':
                assert save(command, key, current).kind is DbOutcomeKind.COMPLETED
            elif failure == 'uncommitted_projection':
                current.connection.execute('BEGIN')
                current.connection.execute('UPDATE device_activities SET extra_wait_ms_used=?,expected_check_at=? WHERE action_id=?',
                    (command.extra_wait_ms, command.plan.check_at_utc, wait_case.aid))
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, current)
    wait_case.hooks['reconfigure_wait'] = uncertain
    try:
        original_deadline = wait_case.deadline
        extra[0] = 1000
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        assert wait_case.deadline == original_deadline
        extra[0] = 2000
        await wait_case.drive(narrow=True)
        first_command, first_key = calls[0]
        if failure != 'after_commit':
            assert calls[1] == (first_command, first_key)
        assert calls[-1][0].extra_wait_ms == 2000 and calls[-1][1] != first_key
        assert owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone() == (2000, _NOW + (3200000 if startup == 0 else 4200000))
        assert wait_case.deadline == (8200000000 if startup == 0 else 9200000000)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (2,)
        assert wait_case.calls == ['start_timelapse']
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_unknown_completion_is_resolved_before_adopting_changed_extra_wait(tmp_path, wait_case, startup, committed):
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    await wait_case.reopen(wall=[_NOW + 3000000], mono=[10000000000])
    save = wait_case.save('complete_wait')
    calls, fault = [], [True]
    def uncertain(command, key, current):
        calls.append((command, key))
        if fault[0]:
            fault[0] = False
            if committed:
                assert save(command, key, current).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, current)
    wait_case.hooks['complete_wait'] = uncertain
    try:
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        extra[0] = 2000
        event = await wait_case.drive(narrow=True)
        row = owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at,wait_completed_event_id FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone()
        if committed:
            assert event == row[2] and event is not None
            assert row[:2] == (0, _NOW + (1200000 if startup == 0 else 2200000))
            assert len(calls) == 1
        else:
            assert event is None and row[2] is None
            assert row[:2] == (2000, _NOW + (3200000 if startup == 0 else 4200000))
            assert wait_case.deadline == (10200000000 if startup == 0 else 11200000000)
        assert wait_case.calls == [] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_repeated_unknown_completion_verifies_each_original_attempt_before_resend(tmp_path, wait_case, startup):
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    await wait_case.reopen(wall=[_NOW + 3000000], mono=[10000000000])
    complete, verify = wait_case.save('complete_wait'), wait_case.save('verify_wait')
    saves, checks = [], []
    def uncertain(command, key, current):
        saves.append((command, key))
        if len(saves) == 2:
            assert complete(command, key, current).kind is DbOutcomeKind.COMPLETED
        return DbOutcome(DbOutcomeKind.UNKNOWN)
    def verifying(command, key, current, **options):
        checks.append((command, key))
        return verify(command, key, current, **options)
    wait_case.hooks.update(complete_wait=uncertain, verify_wait=verifying)
    try:
        for _ in range(2):
            with pytest.raises(ConsistencyError):
                await wait_case.drive(narrow=True)
        event = await wait_case.drive(narrow=True)
        assert event is not None
        assert len(saves) == 2 and len(checks) == 2
        assert saves[0] == saves[1] == checks[0] == checks[1]
        assert wait_case.calls == [] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_continuous_extra_wait_changes_ignore_wall_jumps_and_failed_candidates(tmp_path, wait_case, startup):
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    def sent_source():
        return owned.connection.execute("SELECT d.sent_at,t.result_event_id FROM device_activities d JOIN operation_runs r ON r.activity_id=d.id JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?", (f"start/{wait_case.aid}",)).fetchone()
    try:
        reference = sent_source()
        extra[0] = 3000
        wait_case.hooks['reconfigure_wait'] = wait_case.reject_projection('reconfigure_wait')
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        del wait_case.hooks['reconfigure_wait']
        for value, offset in [(2000, 1000000000), (500, -1000000000), (0, 2000000000), (4000, -2000000000)]:
            extra[0], wall[0] = value, _NOW + offset
            assert await wait_case.drive(narrow=True) is None
            assert sent_source() == reference
            assert wait_case.deadline == 5000000000 + (startup + 1200 + value) * 1000000
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (4,)
        assert wait_case.calls == ['start_timelapse'] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_negative_logical_deadline_survives_completion_rollback_and_smaller_extra_wait(tmp_path, wait_case, startup):
    owned, first, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    await wait_case.reopen(wall=[_NOW + 10000000], mono=[1000000000])
    wait_case.hooks['complete_wait'] = wait_case.reject_projection('complete_wait')
    try:
        extra[0] = 2000
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        assert wait_case.deadline == -5800000000 + startup * 1000000
        extra[0] = 500
        del wait_case.hooks['complete_wait']
        event = await wait_case.drive(narrow=True)
        assert event is not None
        assert wait_case.deadline == -7300000000 + startup * 1000000
        row = owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone()
        assert row == (500, _NOW + (startup + 1700) * 1000)
        assert wait_case.calls == [] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_initial_unknown_schedule_keeps_ack_reference_and_original_request(tmp_path, wait_case, startup, committed):
    from camctl.persistence.models import DbOutcome
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=startup)
    save = wait_case.save('schedule_wait')
    calls = []
    def uncertain(command, key, current):
        calls.append((command, key))
        if len(calls) == 1:
            if committed:
                assert save(command, key, current).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, current)
    wait_case.hooks['schedule_wait'] = uncertain
    try:
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        assert wait_case.deadline is None
        reference = owned.connection.execute('SELECT sent_at FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone()
        wall[0] += 1000000000
        mono[0] += 100000000
        extra[0] = 2000
        assert await wait_case.drive(narrow=True) is None
        assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone() == reference
        assert len(calls) == (1 if committed else 2)
        if not committed:
            assert calls[0] == calls[1]
        assert wait_case.deadline == 8200000000 + startup * 1000000
        assert wait_case.calls == ['start_timelapse'] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_uncommitted_completion_projection_does_not_become_a_saved_fact(tmp_path, wait_case, startup):
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await wait_case.world(tmp_path, startup=startup, advance=True)
    runtime = await wait_case.reopen(wall=[_NOW + 3000000], mono=[10000000000])
    def uncertain(command, key, current):
        current.connection.execute('BEGIN')
        current.connection.execute('UPDATE device_activities SET wait_completed_event_id=999999 WHERE action_id=?', (wait_case.aid,))
        return DbOutcome(DbOutcomeKind.UNKNOWN)
    wait_case.hooks['complete_wait'] = uncertain
    try:
        with pytest.raises(ConsistencyError):
            await wait_case.drive(narrow=True)
        extra[0] = 2000
        assert await wait_case.drive(narrow=True) is None
        assert not await runtime.database.read(lambda current: current.connection.in_transaction)
        assert owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone() == (None,)
        assert wait_case.deadline == 10200000000 + startup * 1000000
        assert wait_case.calls == [] and wait_case.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('boundary', ['total_duration', 'utc_deadline', 'monotonic_deadline', 'extra_wait'])
async def test_unrepresentable_natural_wait_is_rejected_before_any_start(tmp_path, wait_case, startup, boundary):
    from camctl.contracts.values import MAX_OBJECT_ID
    # 9999 年仍可由计划协议表示，并使合法最大延迟能够覆盖 UTC 整数上界。
    initial_utc = 253370764800000000 if boundary == 'utc_deadline' else _NOW
    wall, mono, extra = [initial_utc], [5000000000], [0]
    duration_ms = MAX_OBJECT_ID if boundary == 'total_duration' else 1000
    if boundary == 'monotonic_deadline':
        mono[0] = MAX_OBJECT_ID - 1000
    elif boundary == 'extra_wait':
        extra[0] = MAX_OBJECT_ID
    owned, runtime, wall, mono, extra = await wait_case.world(tmp_path, startup=startup,
        duration_ms=duration_ms, wall=wall, mono=mono, extra=extra,
        now_us=initial_utc,
        max_delay_ms=((MAX_OBJECT_ID // 1000000) * 1000000 - initial_utc) // 1000
        if boundary == 'utc_deadline' else 5000)
    if boundary == 'utc_deadline':
        # 计划日期与延迟策略均经正式受理；时钟到达合法窗口末端时，
        # 自然等待截止将超出持久整数范围，不能新建或派发 START。
        wall[0] = (MAX_OBJECT_ID // 1000000) * 1000000
    try:
        with pytest.raises(ValueError, match='精确整数范围'):
            await wait_case.drive()
        assert wait_case.calls == [] and wait_case.results.calls == []
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?", (f"start/{wait_case.aid}",)).fetchone() == (0,)
        assert owned.connection.execute('SELECT sent_at,started_at FROM device_activities WHERE action_id=?', (wait_case.aid,)).fetchone() == (None, None)
    finally:
        owned.connection.close()
