"""延时等待实际处理入口的重配置、锚点与会话计时。"""

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
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
