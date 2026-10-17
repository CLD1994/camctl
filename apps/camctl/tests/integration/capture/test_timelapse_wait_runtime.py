"""延时等待实际处理入口的重配置、锚点与会话计时。"""

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind

from .test_capture_contract import _TIMELAPSE, _NOW, _entry, _environment, _runtime

pytestmark = pytest.mark.asyncio


def _configured_runtime(owned, *, extra, wall, monotonic):
    runtime = _runtime(owned, files={13: (_entry("sequence-1"),)})
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, extra)
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: monotonic[0]
    return runtime


@pytest.mark.parametrize("extra", [0, 5000])
async def test_new_session_reconfigures_unfinished_wait_from_original_sent_at(tmp_path, extra):
    owned = _environment(tmp_path, _TIMELAPSE)
    try:
        first = _configured_runtime(owned, extra=1000, wall=[_NOW], monotonic=[10_000_000_000])
        handler = capture_handler("camera_timelapse")
        await handler(13, first)
        second = _configured_runtime(owned, extra=extra, wall=[_NOW + 100_000], monotonic=[20_000_000_000])
        await handler(13, second)
        row = owned.connection.execute(
            "SELECT sent_at, result_wait_margin_ms, extra_wait_ms_used, expected_check_at"
            " FROM device_activities WHERE action_id = 13").fetchone()
        assert row == (_NOW, 200, extra, _NOW + (1200 + extra) * 1000)
        assert second.driver.calls == []
        before = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        await handler(13, second)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == before
    finally:
        owned.connection.close()


async def test_completed_wait_is_not_reopened_by_new_extra_wait(tmp_path):
    owned = _environment(tmp_path, _TIMELAPSE)
    try:
        first = _configured_runtime(owned, extra=1000, wall=[_NOW], monotonic=[10_000_000_000])
        await capture_handler("camera_timelapse")(13, first)
        saved = first.timelapse.complete_wait(
            WaitCompletedSave(13, _NOW + 2_200_000), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        second = _configured_runtime(owned, extra=9000, wall=[_NOW + 2_200_000], monotonic=[30_000_000_000])
        await capture_handler("camera_timelapse")(13, second)
        row = owned.connection.execute(
            "SELECT extra_wait_ms_used, expected_check_at, wait_completed_event_id"
            " FROM device_activities WHERE action_id = 13").fetchone()
        assert row == (1000, _NOW + 2_200_000, saved.value.event_id)
        assert second.results.calls == [13]
    finally:
        owned.connection.close()


async def test_forward_wall_clock_change_does_not_finish_current_session_wait(tmp_path):
    owned = _environment(tmp_path, _TIMELAPSE)
    try:
        wall, monotonic = [_NOW], [10_000_000_000]
        runtime = _configured_runtime(owned, extra=1000, wall=wall, monotonic=monotonic)
        await capture_handler("camera_timelapse")(13, runtime)
        wall[0] += 100_000_000
        monotonic[0] += 100_000_000
        await capture_handler("camera_timelapse")(13, runtime)
        assert runtime.results.calls == []
        assert owned.connection.execute(
            "SELECT wait_completed_event_id FROM device_activities WHERE action_id = 13").fetchone() == (None,)
    finally:
        owned.connection.close()


async def test_backward_wall_clock_change_does_not_extend_current_session_wait(tmp_path):
    owned = _environment(tmp_path, _TIMELAPSE)
    try:
        wall, monotonic = [_NOW], [10_000_000_000]
        runtime = _configured_runtime(owned, extra=1000, wall=wall, monotonic=monotonic)
        await capture_handler("camera_timelapse")(13, runtime)
        wall[0] -= 100_000_000
        monotonic[0] += 2_200_000_000
        await capture_handler("camera_timelapse")(13, runtime)
        assert runtime.results.calls == [13]
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 13").fetchone() == (3,)
    finally:
        owned.connection.close()


async def test_send_return_anchor_precedes_result_persistence_delay(tmp_path):
    owned = _environment(tmp_path, _TIMELAPSE)
    try:
        wall, monotonic = [_NOW], [10_000_000_000]
        runtime = _configured_runtime(owned, extra=1000, wall=wall, monotonic=monotonic)
        control, finish = runtime.driver.control, runtime.finish

        async def delayed_control(request):
            response = await control(request)
            wall[0] += 200_000
            monotonic[0] += 200_000_000
            return response

        def delayed_finish(*args, **kwargs):
            finish(*args, **kwargs)
            wall[0] += 5_000_000
            monotonic[0] += 5_000_000_000

        runtime.driver.control = delayed_control
        runtime.finish = delayed_finish
        await capture_handler("camera_timelapse")(13, runtime)
        assert owned.connection.execute(
            "SELECT sent_at, expected_check_at FROM device_activities WHERE action_id = 13").fetchone() == (
                _NOW + 200_000, _NOW + 2_400_000)
        await capture_handler("camera_timelapse")(13, runtime)
        assert runtime.results.calls == [13]
    finally:
        owned.connection.close()


async def test_new_natural_wait_uses_frozen_startup_and_reconfigures_current_deadline(tmp_path):
    import json
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(target_duration_ms=300000, result_wait_margin_ms=30000, startup_margin_ms=1000)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    wall, mono, extra = [_NOW], [5000000000], [0]
    runtime = _runtime(owned, files={13: (_entry('sequence-1'),)})
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(300000, 30000, extra[0], startup_margin_ms=1000)
    try:
        handler = capture_handler('camera_timelapse')
        await handler(13, runtime)
        assert owned.connection.execute('SELECT expected_check_at FROM device_activities WHERE id=13').fetchone() == (_NOW+331000000,)
        wall[0] += 330000000
        mono[0] += 330000000000
        await handler(13, runtime)
        assert runtime.results.calls == []
        extra[0] = 2000
        await handler(13, runtime)
        assert runtime.timelapse_deadlines[13] == 338000000000
        assert owned.connection.execute('SELECT expected_check_at,extra_wait_ms_used FROM device_activities WHERE id=13').fetchone() == (_NOW+333000000,2000)
        wall[0] += 1000000
        mono[0] += 1000000000
        await handler(13, runtime)
        assert runtime.results.calls == []
        wall[0] += 2000000
        mono[0] += 2000000000
        await handler(13, runtime)
        assert runtime.results.calls == [13]
    finally:
        owned.connection.close()


@pytest.mark.parametrize('offset_ms', [0, 2000])
async def test_wait_repository_independently_rejects_missing_or_duplicate_startup_margin(tmp_path, offset_ms):
    import json
    from camctl.capture.timelapse import WaitPlan, WaitKind
    from camctl.persistence.repositories.timelapse import ScheduleWait
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(startup_margin_ms=1000, target_duration_ms=1000, result_wait_margin_ms=200)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    runtime = _runtime(owned)
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, startup_margin_ms=1000)
    try:
        await capture_handler('camera_timelapse')(13, runtime)
        saved = runtime.timelapse.reconfigure_wait(ScheduleWait(13,
            WaitPlan(WaitKind.WAIT_THEN_CHECK, check_at_utc=_NOW+(1200+offset_ms)*1000),
            200, 0, _NOW), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


@pytest.mark.parametrize('failure', ['rollback', 'unknown_before_commit', 'unknown_after_commit'])
async def test_reconfigure_rollback_does_not_add_extra_wait_twice(tmp_path, failure):
    import json
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(startup_margin_ms=1000, target_duration_ms=1000, result_wait_margin_ms=200)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    extra = [0]
    runtime = _runtime(owned)
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, extra[0], startup_margin_ms=1000)
    advance = capture_handler('camera_timelapse')
    try:
        await advance(13, runtime)
        extra[0] = 2000
        save = runtime.timelapse.reconfigure_wait
        def fail_save(*args):
            if failure == 'unknown_after_commit':
                assert save(*args).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.ROLLED_BACK if failure == 'rollback' else DbOutcomeKind.UNKNOWN)
        runtime.timelapse.reconfigure_wait = fail_save
        with pytest.raises(ConsistencyError):
            await advance(13, runtime)
        runtime.timelapse.reconfigure_wait = save
        await advance(13, runtime)
        assert runtime.timelapse_deadlines[13] == 9200000000
        assert owned.connection.execute('SELECT expected_check_at FROM device_activities WHERE id=13').fetchone() == (_NOW+4200000,)
        assert runtime.driver.calls == ['start_timelapse']
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_overdue_restart_with_unsaved_completion_reconfigures_from_original_anchor(tmp_path):
    import json
    from dataclasses import replace
    from camctl.persistence.models import DbOutcome
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(startup_margin_ms=1000, target_duration_ms=1000, result_wait_margin_ms=200)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    extra = [0]
    runtime = _runtime(owned)
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, extra[0], startup_margin_ms=1000)
    advance = capture_handler('camera_timelapse')
    try:
        await advance(13, runtime)
        runtime = replace(runtime, timelapse_deadlines={}, natural_wait_states={},
            wall_us=lambda: _NOW+3000000, monotonic_ns=lambda: 10000000000)
        complete = runtime.timelapse.complete_wait
        runtime.timelapse.complete_wait = lambda *args: DbOutcome(DbOutcomeKind.ROLLED_BACK)
        with pytest.raises(ConsistencyError):
            await advance(13, runtime)
        runtime.timelapse.complete_wait = complete
        extra[0] = 2000
        await advance(13, runtime)
        assert runtime.timelapse_deadlines[13] == 11200000000
    finally:
        owned.connection.close()


async def _mapping_world(tmp_path, startup, *, advance=True):
    import json
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(target_duration_ms=1000, result_wait_margin_ms=200)
    if startup: spec['startup_margin_ms'] = startup
    else: spec.pop('startup_margin_ms', None)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    wall, mono, extra = [_NOW], [5000000000], [0]
    runtime = _runtime(owned, files={13: (_entry('sequence-1'),)})
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, extra[0], startup_margin_ms=startup)
    if advance:
        await capture_handler('camera_timelapse')(13, runtime)
    return owned, runtime, wall, mono, extra


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('failure', ['before_commit', 'after_commit', 'uncommitted_projection'])
async def test_unknown_schedule_retains_original_key_and_publishes_only_reliable_arrangements(tmp_path, startup, failure):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome
    owned, runtime, wall, mono, extra = await _mapping_world(tmp_path, startup)
    save = runtime.timelapse.reconfigure_wait
    calls, fault = [], [True]
    def uncertain(command, key, connection):
        calls.append((command, key))
        if fault[0]:
            fault[0] = False
            if failure == 'after_commit':
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            elif failure == 'uncommitted_projection':
                owned.connection.execute('BEGIN')
                owned.connection.execute('UPDATE device_activities SET extra_wait_ms_used=?,expected_check_at=? WHERE id=13',
                    (command.extra_wait_ms, command.plan.check_at_utc))
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.timelapse.reconfigure_wait = uncertain
    try:
        original_deadline = runtime.timelapse_deadlines[13]
        extra[0] = 1000
        with pytest.raises(ConsistencyError):
            await capture_handler('camera_timelapse')(13, runtime)
        assert runtime.timelapse_deadlines[13] == original_deadline
        extra[0] = 2000
        await capture_handler('camera_timelapse')(13, runtime)
        first_command, first_key = calls[0]
        if failure != 'after_commit':
            assert calls[1] == (first_command, first_key)
        assert calls[-1][0].extra_wait_ms == 2000 and calls[-1][1] != first_key
        assert owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at FROM device_activities WHERE id=13').fetchone() == (
            2000, _NOW + (3200000 if startup == 0 else 4200000))
        assert runtime.timelapse_deadlines[13] == (8200000000 if startup == 0 else 9200000000)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (2,)
        assert runtime.driver.calls == ['start_timelapse']
    finally:
        if owned.connection.in_transaction: owned.connection.execute('ROLLBACK')
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_unknown_completion_is_resolved_before_adopting_changed_extra_wait(tmp_path, startup, committed):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await _mapping_world(tmp_path, startup)
    runtime = _runtime(owned)
    runtime.wall_us = lambda: _NOW+3000000
    runtime.monotonic_ns = lambda: 10000000000
    runtime.wait_config = first.wait_config
    save = runtime.timelapse.complete_wait
    calls, fault = [], [True]
    def uncertain(command, key, connection):
        calls.append((command, key))
        if fault[0]:
            fault[0] = False
            if committed:
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.timelapse.complete_wait = uncertain
    try:
        with pytest.raises((ConsistencyError, AssertionError)):
            _advance_timelapse_natural_wait(runtime, runtime.action(13))
        extra[0] = 2000
        event = _advance_timelapse_natural_wait(runtime, runtime.action(13))
        row = owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at,wait_completed_event_id FROM device_activities WHERE id=13').fetchone()
        if committed:
            assert event == row[2] and event is not None
            assert row[:2] == (0, _NOW+(1200000 if startup == 0 else 2200000))
            assert len(calls) == 1
        else:
            assert event is None and row[2] is None
            assert row[:2] == (2000, _NOW+(3200000 if startup == 0 else 4200000))
            assert runtime.timelapse_deadlines[13] == (10200000000 if startup == 0 else 11200000000)
        assert runtime.driver.calls == [] and runtime.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_repeated_unknown_completion_verifies_each_original_attempt_before_resend(tmp_path, startup):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await _mapping_world(tmp_path, startup)
    runtime = _runtime(owned)
    runtime.wall_us = lambda: _NOW+3000000
    runtime.monotonic_ns = lambda: 10000000000
    runtime.wait_config = first.wait_config
    complete, verify = runtime.timelapse.complete_wait, runtime.timelapse.verify_wait
    saves, checks = [], []
    def uncertain(command, key, connection):
        saves.append((command, key))
        if len(saves) == 2:
            assert complete(command, key, connection).kind is DbOutcomeKind.COMPLETED
        return DbOutcome(DbOutcomeKind.UNKNOWN)
    def verifying(command, key, connection, **kwargs):
        checks.append((command, key))
        return verify(command, key, connection, **kwargs)
    runtime.timelapse.complete_wait = uncertain
    runtime.timelapse.verify_wait = verifying
    try:
        for _ in range(2):
            with pytest.raises(ConsistencyError):
                _advance_timelapse_natural_wait(runtime, runtime.action(13))
        event = _advance_timelapse_natural_wait(runtime, runtime.action(13))
        assert event is not None
        assert len(saves) == 2 and len(checks) == 2
        assert saves[0] == saves[1] == checks[0] == checks[1]
        assert runtime.driver.calls == [] and runtime.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_continuous_extra_wait_changes_ignore_wall_jumps_and_failed_candidates(tmp_path, startup):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.persistence.models import DbOutcome
    owned, runtime, wall, mono, extra = await _mapping_world(tmp_path, startup)
    try:
        reference = runtime.natural_wait_states[13].reference
        save = runtime.timelapse.reconfigure_wait
        extra[0] = 3000
        runtime.timelapse.reconfigure_wait = lambda *args: DbOutcome(DbOutcomeKind.ROLLED_BACK)
        with pytest.raises(ConsistencyError):
            _advance_timelapse_natural_wait(runtime, runtime.action(13))
        runtime.timelapse.reconfigure_wait = save
        for value, offset in [(2000, 1000000000), (500, -1000000000), (0, 2000000000), (4000, -2000000000)]:
            extra[0], wall[0] = value, _NOW+offset
            assert _advance_timelapse_natural_wait(runtime, runtime.action(13)) is None
            assert runtime.natural_wait_states[13].reference is reference
            assert runtime.timelapse_deadlines[13] == 5000000000+(startup+1200+value)*1000000
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=15 AND json_extract(body_json,'$.reason')=2").fetchone() == (4,)
        assert runtime.driver.calls == ['start_timelapse'] and runtime.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_negative_logical_deadline_survives_completion_rollback_and_smaller_extra_wait(tmp_path, startup):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await _mapping_world(tmp_path, startup)
    runtime = _runtime(owned)
    runtime.wall_us = lambda: _NOW+10000000
    runtime.monotonic_ns = lambda: 1000000000
    runtime.wait_config = first.wait_config
    complete = runtime.timelapse.complete_wait
    runtime.timelapse.complete_wait = lambda *args: DbOutcome(DbOutcomeKind.ROLLED_BACK)
    try:
        extra[0] = 2000
        with pytest.raises(ConsistencyError):
            _advance_timelapse_natural_wait(runtime, runtime.action(13))
        assert runtime.timelapse_deadlines[13] == -5800000000+startup*1000000
        extra[0] = 500
        runtime.timelapse.complete_wait = complete
        event = _advance_timelapse_natural_wait(runtime, runtime.action(13))
        assert event is not None
        assert runtime.timelapse_deadlines[13] == -7300000000+startup*1000000
        row = owned.connection.execute('SELECT extra_wait_ms_used,expected_check_at FROM device_activities WHERE id=13').fetchone()
        assert row == (500, _NOW+(startup+1700)*1000)
        assert runtime.driver.calls == [] and runtime.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_initial_unknown_schedule_keeps_ack_reference_and_original_request(tmp_path, startup, committed):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.persistence.models import DbOutcome
    owned, runtime, wall, mono, extra = await _mapping_world(tmp_path, startup, advance=False)
    save = runtime.timelapse.schedule_wait
    calls = []
    def uncertain(command, key, connection):
        calls.append((command, key))
        if len(calls) == 1:
            if committed:
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.timelapse.schedule_wait = uncertain
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler('camera_timelapse')(13, runtime)
        state = runtime.natural_wait_states[13]
        assert state.applied is None and 13 not in runtime.timelapse_deadlines
        reference = state.reference
        wall[0] += 1000000000
        mono[0] += 100000000
        extra[0] = 2000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(13)) is None
        assert state.reference is reference
        assert len(calls) == (1 if committed else 2)
        if not committed: assert calls[0] == calls[1]
        assert runtime.timelapse_deadlines[13] == 8200000000+startup*1000000
        assert runtime.driver.calls == ['start_timelapse'] and runtime.results.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_uncommitted_completion_projection_does_not_become_a_saved_fact(tmp_path, startup):
    from camctl.capture.handlers import _advance_timelapse_natural_wait
    from camctl.persistence.models import DbOutcome
    owned, first, wall, mono, extra = await _mapping_world(tmp_path, startup)
    runtime = _runtime(owned)
    runtime.wall_us = lambda: _NOW+3000000
    runtime.monotonic_ns = lambda: 10000000000
    runtime.wait_config = first.wait_config
    def uncertain(command, key, connection):
        owned.connection.execute('BEGIN')
        owned.connection.execute('UPDATE device_activities SET wait_completed_event_id=999999 WHERE id=13')
        return DbOutcome(DbOutcomeKind.UNKNOWN)
    runtime.timelapse.complete_wait = uncertain
    try:
        with pytest.raises(ConsistencyError):
            _advance_timelapse_natural_wait(runtime, runtime.action(13))
        extra[0] = 2000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(13)) is None
        assert not owned.connection.in_transaction
        assert owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE id=13').fetchone() == (None,)
        assert runtime.timelapse_deadlines[13] == 10200000000+startup*1000000
        assert runtime.driver.calls == [] and runtime.results.calls == []
    finally:
        if owned.connection.in_transaction: owned.connection.execute('ROLLBACK')
        owned.connection.close()
