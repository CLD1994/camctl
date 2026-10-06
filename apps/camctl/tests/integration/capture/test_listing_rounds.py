"""录像/照片/取消收尾列举路径轮次化的组件集成测试。

真实 SQLite、核实流程与尝试事务同可编排失败的结果列举替身组合：
照片响应确定后的产物核实、录像尾段的观察登记、等待中取消的收尾
列举各自按 results 责任的有限轮次推进——本轮列举失败保存实际结
果与重试等待，下一轮作为新轮次累计次数；预算耗尽按所属拍摄及收
场规则结束，录像活动不适用集合结论故仅收场核实流程，取消终态优
先于产物登记。会话内共享的列举缓存让等待媒体装配的录像不因轮询
反复消耗核实名额。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind as ResultFileKind
from camctl.operations.attempts import AttemptConfig

from .test_capture_contract import (
    _NOW,
    _PHOTO,
    _RECORD,
    _environment,
    _entry,
    _runtime,
    _seed_processing,
    _seed_stopped_recording,
    _value,
)

pytestmark = pytest.mark.asyncio

#: 本文件验证轮次记账与终态分区，不验证计时：核实间隔置零保持
#: 背靠背重试；间隔的时间强制由 test_retry_intervals.py 单独验证。
_IMMEDIATE_CHECK = AttemptConfig(
    max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("0"))


class _FlakyResults:
    """结果列举替身：前 failures 次调用抛通信错误，之后返回编排文件。"""

    def __init__(self, files_by_action: dict[int, tuple],
                 failures: int = 0) -> None:
        self.files_by_action = files_by_action
        self.failures = failures
        self.calls: list[int] = []

    async def list_files(self, action_id: int) -> tuple:
        self.calls.append(action_id)
        if len(self.calls) <= self.failures:
            raise RuntimeError("列举通信失败")
        return self.files_by_action.get(action_id, ())


def _run_row(owned, action_id: int):
    """核实流程行的当前状态：状态、累计次数与重试等待。"""
    return _value(
        owned,
        "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
        " WHERE responsibility_key = ?",
        f"results/{action_id}")


class TestPhotoListingRounds:
    async def test_listing_failure_retries_next_round_then_succeeds(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults(
                {11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)},
                failures=1)
            runtime = _runtime(owned, results=flaky, check_config=_IMMEDIATE_CHECK)
            await capture_handler("camera_take_photo")(11, runtime)
            # 本轮列举失败：保存失败结果与重试等待，动作保持执行中。
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (2,)
            assert _run_row(owned, 11) == (2, 1, 1)
            await capture_handler("camera_take_photo")(11, runtime)
            # 下一轮作为新轮次成功核实，流程以可靠结果收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
            assert _run_row(owned, 11) == (3, 2, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert flaky.calls == [11, 11]
        finally:
            owned.connection.close()

    async def test_exhausted_rounds_close_unconfirmed(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults({}, failures=3)
            runtime = _runtime(owned, results=flaky, check_config=_IMMEDIATE_CHECK)
            for _ in range(3):
                await capture_handler("camera_take_photo")(11, runtime)
            # 三轮全部失败后预算耗尽：不发起第四次设备列举。
            assert flaky.calls == [11, 11, 11]
            await capture_handler("camera_take_photo")(11, runtime)
            assert flaky.calls == [11, 11, 11]
            # 无法确认的集合结论与流程收场同事务保存，动作失败终态。
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (4,)
            assert _value(
                owned, "SELECT error_code FROM actions WHERE id = 11") == (12,)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = 11") == (4,)
            assert _run_row(owned, 11) == (6, 3, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()


class TestRecordingListingRounds:
    async def _stopped_tail(self, owned, results):
        """推进到停止确认后的录像尾段：启动先行，停止与处理已保存。"""
        runtime = _runtime(owned, results=results, check_config=_IMMEDIATE_CHECK)
        await capture_handler("camera_record")(12, runtime)
        _seed_processing(owned.connection, 12)
        _seed_stopped_recording(owned.connection, 12)
        owned.connection.commit()
        return runtime

    async def test_tail_failure_retries_then_succeeds(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _RECORD)
        try:
            flaky = _FlakyResults({12: (_entry("clip-1"),)}, failures=1)
            runtime = await self._stopped_tail(owned, flaky)
            await capture_handler("camera_record")(12, runtime)
            # 尾段列举失败：保存失败结果与重试等待，终态等待下一轮。
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (2,)
            assert _run_row(owned, 12) == (2, 1, 1)
            await capture_handler("camera_record")(12, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (3,)
            assert _run_row(owned, 12) == (3, 2, 0)
            assert _value(
                owned, "SELECT kind, device_file_id FROM outputs"
                " WHERE source_action_id = 12") == (1, 1)
            assert flaky.calls == [12, 12]
        finally:
            owned.connection.close()

    async def test_exhausted_rounds_close_run_without_set_conclusion(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _RECORD)
        try:
            flaky = _FlakyResults({}, failures=3)
            runtime = await self._stopped_tail(owned, flaky)
            for _ in range(3):
                await capture_handler("camera_record")(12, runtime)
            await capture_handler("camera_record")(12, runtime)
            # 录像活动不适用集合结论：仅核实流程按公共错误结构收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (4,)
            assert _value(
                owned, "SELECT error_code FROM actions WHERE id = 12") == (12,)
            assert _run_row(owned, 12) == (6, 3, 0)
            assert _value(
                owned, "SELECT json_extract(error_json, '$.code')"
                " FROM operation_runs WHERE responsibility_key = 'results/12'"
            ) == ("capture_result_unconfirmed",)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = 12") == (1,)
            # 从未成功列举：不登记产物，也不保留文件观察。
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _value(owned, "SELECT COUNT(*) FROM device_files") == (0,)
        finally:
            owned.connection.close()

    async def test_session_listing_cache_skips_repeated_rounds(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _RECORD)
        try:
            # 需要检查的录像在媒体端口未装配时保持执行中，等待装配会话。
            _seed_processing(owned.connection, 12)
            owned.connection.execute(
                "UPDATE recording_processing SET check_decision = 3"
                " WHERE action_id = 12")
            _seed_stopped_recording(owned.connection, 12)
            owned.connection.commit()
            cache: dict = {}
            flaky = _FlakyResults({12: (_entry("clip-1"),)})
            first = _runtime(owned, results=flaky, listing_cache=cache)
            await capture_handler("camera_record")(12, first)
            # 第一次推进只发起启动；停止与处理责任已由种子保存。
            await capture_handler("camera_record")(12, first)
            assert flaky.calls == [12]
            assert _run_row(owned, 12) == (2, 1, 1)
            # 同会话再次推进：已观察的列举事实直接复用，不消耗核实名额。
            second = _runtime(owned, results=flaky, listing_cache=cache)
            await capture_handler("camera_record")(12, second)
            assert flaky.calls == [12]
            assert _run_row(owned, 12) == (2, 1, 1)
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (2,)
        finally:
            owned.connection.close()


class TestCanceledTimelapseCloseRounds:
    async def _cancel_after_send(self, owned, results):
        """先正常发送并安排等待，再置取消并种已确认的停止。"""
        runtime = _runtime(owned, results=results, check_config=_IMMEDIATE_CHECK)
        await capture_handler("camera_timelapse")(13, runtime)
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 13")
        owned.connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (30, 13, NULL, 2, NULL, 'stop/13', 13, NULL, NULL, NULL,"
            " 3, 1, 3, '10', '1', 0, NULL)")
        owned.connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
            " intent_event_id, result_event_id, max_attempts_used,"
            " effect_state, result_json)"
            " VALUES (40, 30, 1, 2, 1, 1, 3, 3, '{}')")
        owned.connection.commit()
        return runtime

    async def test_close_failure_retries_then_confirms(
            self, tmp_path: Path) -> None:
        from .test_capture_contract import _TIMELAPSE

        owned = _environment(tmp_path, _TIMELAPSE)
        try:
            flaky = _FlakyResults({13: (_entry("sequence-1"),)}, failures=1)
            runtime = await self._cancel_after_send(owned, flaky)
            await capture_handler("camera_timelapse")(13, runtime)
            # 收尾列举失败：保留取消待收场事实，下一轮重新核实。
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (2,)
            assert _run_row(owned, 13) == (2, 1, 1)
            await capture_handler("camera_timelapse")(13, runtime)
            # 成功轮以可靠结果收场，已拍完文件与取消终态同事务登记。
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (6,)
            assert _run_row(owned, 13) == (3, 2, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert flaky.calls == [13, 13]
        finally:
            owned.connection.close()

    async def test_close_exhausted_cancels_without_outputs(
            self, tmp_path: Path) -> None:
        from .test_capture_contract import _TIMELAPSE

        owned = _environment(tmp_path, _TIMELAPSE)
        try:
            flaky = _FlakyResults({}, failures=3)
            runtime = await self._cancel_after_send(owned, flaky)
            for _ in range(3):
                await capture_handler("camera_timelapse")(13, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (2,)
            await capture_handler("camera_timelapse")(13, runtime)
            # 取消终态优先：耗尽时不登记产物，集合结论按无法确认收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (6,)
            assert _run_row(owned, 13) == (6, 3, 0)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = 13") == (4,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()
