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

from camctl.capture.handlers import advance_winddown, capture_handler
from camctl.capture.results import FileKind as ResultFileKind
from camctl.operations.attempts import AttemptConfig

from .test_capture_contract import _NOW, _entry, _value, ResultsDouble
from .result_consumer_fixtures import returned
from ..support.result_cases import result_case, result_case_for
from camctl.devices.ports import DeviceCallResult
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.models import DbOutcomeKind
from camctl.capture.processing import CheckDecisionSave, CheckDecisionChoice, CheckBasis, CheckReason
from camctl.contracts.values import new_operation_key
from dataclasses import replace


pytestmark = pytest.mark.asyncio

#: 本文件验证轮次记账与终态分区，不验证计时：核实间隔置零保持
#: 背靠背重试；间隔的时间强制由 test_retry_intervals.py 单独验证。
_IMMEDIATE_CHECK = AttemptConfig(
    max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("0"))


class _FlakyResults(ResultsDouble):
    """结果列举替身：前 failures 次调用抛通信错误，之后返回编排文件。"""

    def __init__(self, files_by_action: dict[int, tuple],
                 failures: int = 0) -> None:
        super().__init__(files_by_action)
        self.failures = failures

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


async def _listing_world(tmp_path, consumer, *, media_wait=False, monkeypatch=None):
    case = result_case_for()
    world = await case.world(tmp_path, consumer, prepare_start_only=media_wait,
        assembly_options={"check_config": _IMMEDIATE_CHECK, "media_enabled": False})
    if media_wait:
        owned, runtime, aid, handler = world
        activity_id, = owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", (aid,)).fetchone()
        case.clock.wall = _NOW + 1_000_000
        case.clock.monotonic = 6_000_000_000
        case.stopper.stop.return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", activity_id))
        # 冷会话经正式保守入口实际提前停止，先交付已可靠停止的边界。
        runtime = await case.reopen(owned, cold=True)
        class StopPrepared(Exception):
            pass
        original = CaptureRepository.finish_capture_stop
        def save_stop(repository, *args, **kwargs):
            receipt = original(repository, *args, **kwargs)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=StopPrepared())
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "finish_capture_stop", save_stop)
            with pytest.raises(StopPrepared):
                await advance_winddown(aid, runtime, {}, wait_cap_s=Decimal(0))
        # 本目标验证等待媒体时的原列举复用；检查责任使用公开仓储准备。
        processing_id, = owned.connection.execute("SELECT id FROM recording_processing WHERE action_id=?", (aid,)).fetchone()
        request = CheckDecisionSave(processing_id, CheckDecisionChoice.REQUIRED,
            CheckBasis(CheckReason.INSUFFICIENT_TIMING, 60000), case.clock.wall)
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: CaptureRepository().save_check_decision(
            request, key, current), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        return owned, runtime, aid, handler
    return world


class TestPhotoListingRounds:
    async def test_listing_failure_retries_next_round_then_succeeds(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "photo")
        try:
            flaky = _FlakyResults(
                {aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)},
                failures=1)
            result_case_for().results.delegate = flaky
            await capture_handler("camera_take_photo")(aid, runtime)
            # 本轮列举失败：保存失败结果与重试等待，动作保持执行中。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (2,)
            assert _run_row(owned, aid) == (2, 1, 1)
            await capture_handler("camera_take_photo")(aid, runtime)
            # 下一轮作为新轮次成功核实，流程以可靠结果收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (3,)
            assert _run_row(owned, aid) == (3, 2, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert flaky.calls == [aid, aid]
        finally:
            owned.connection.close()

    async def test_exhausted_rounds_close_unconfirmed(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "photo")
        try:
            flaky = _FlakyResults({}, failures=3)
            result_case_for().results.delegate = flaky
            for _ in range(3):
                await capture_handler("camera_take_photo")(aid, runtime)
            # 三轮全部失败后预算耗尽：不发起第四次设备列举。
            assert flaky.calls == [aid, aid, aid]
            await capture_handler("camera_take_photo")(aid, runtime)
            assert flaky.calls == [aid, aid, aid]
            # 无法确认的集合结论与流程收场同事务保存，动作失败终态。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (4,)
            assert _value(
                owned, "SELECT error_code FROM actions WHERE id = ?", aid) == (12,)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = ?", aid) == (4,)
            assert _run_row(owned, aid) == (6, 3, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()


class TestRecordingListingRounds:
    async def test_tail_failure_retries_then_succeeds(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "record")
        try:
            flaky = _FlakyResults({aid: (_entry("clip-1"),)}, failures=1)
            result_case_for().results.delegate = flaky
            await capture_handler("camera_record")(aid, runtime)
            # 尾段列举失败：保存失败结果与重试等待，终态等待下一轮。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (2,)
            assert _run_row(owned, aid) == (2, 1, 1)
            await capture_handler("camera_record")(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (3,)
            assert _run_row(owned, aid) == (3, 2, 0)
            assert _value(
                owned, "SELECT kind, device_file_id FROM outputs"
                " WHERE source_action_id = ?", aid) == (1, 1)
            assert flaky.calls == [aid, aid]
        finally:
            owned.connection.close()

    async def test_exhausted_rounds_close_run_without_set_conclusion(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "record")
        try:
            flaky = _FlakyResults({}, failures=3)
            result_case_for().results.delegate = flaky
            for _ in range(3):
                await capture_handler("camera_record")(aid, runtime)
            await capture_handler("camera_record")(aid, runtime)
            # 录像活动不适用集合结论：仅核实流程按公共错误结构收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (4,)
            assert _value(
                owned, "SELECT error_code FROM actions WHERE id = ?", aid) == (12,)
            assert _run_row(owned, aid) == (6, 3, 0)
            assert _value(
                owned, "SELECT json_extract(error_json, '$.code')"
                " FROM operation_runs WHERE responsibility_key = ?", f"results/{aid}"
            ) == ("capture_result_unconfirmed",)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = ?", aid) == (1,)
            # 从未成功列举：不登记产物，也不保留文件观察。
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _value(owned, "SELECT COUNT(*) FROM device_files") == (0,)
        finally:
            owned.connection.close()

    async def test_session_listing_cache_skips_repeated_rounds(self, tmp_path: Path, monkeypatch) -> None:
        owned, first, aid, handler = await _listing_world(tmp_path, "record", media_wait=True, monkeypatch=monkeypatch)
        try:
            flaky = _FlakyResults({aid: (_entry("clip-1"),)})
            result_case_for().results.delegate = flaky
            await capture_handler(handler)(aid, first)
            await capture_handler(handler)(aid, first)
            assert flaky.calls == [aid]
            assert _run_row(owned, aid) == (2, 1, 1), owned.connection.execute("SELECT check_decision,check_state,check_basis_json,repair_state,source_device_file_id FROM recording_processing").fetchall()
            second = await result_case_for().factory(first.database, "cam-1")
            await capture_handler(handler)(aid, second)
            assert flaky.calls == [aid]
            assert _run_row(owned, aid) == (2, 1, 1), owned.connection.execute("SELECT check_decision,check_state,check_basis_json,repair_state,source_device_file_id FROM recording_processing").fetchall()
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
        finally:
            owned.connection.close()


class TestCanceledTimelapseCloseRounds:
    async def test_close_failure_retries_then_confirms(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "cancel")
        try:
            flaky = _FlakyResults({aid: (_entry("sequence-1"),)}, failures=1)
            result_case_for().results.delegate = flaky
            await capture_handler("camera_timelapse")(aid, runtime)
            # 收尾列举失败：保留取消待收场事实，下一轮重新核实。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (2,)
            assert _run_row(owned, aid) == (2, 1, 1)
            await capture_handler("camera_timelapse")(aid, runtime)
            # 成功轮以可靠结果收场，已拍完文件与取消终态同事务登记。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (6,)
            assert _run_row(owned, aid) == (3, 2, 0)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert flaky.calls == [aid, aid]
        finally:
            owned.connection.close()

    async def test_close_exhausted_cancels_without_outputs(
            self, tmp_path: Path) -> None:
        owned, runtime, aid, _ = await _listing_world(tmp_path, "cancel")
        try:
            flaky = _FlakyResults({}, failures=3)
            result_case_for().results.delegate = flaky
            for _ in range(3):
                await capture_handler("camera_timelapse")(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (2,)
            await capture_handler("camera_timelapse")(aid, runtime)
            # 取消终态优先：耗尽时不登记产物，集合结论按无法确认收场。
            assert _value(owned, "SELECT status FROM actions WHERE id = ?", aid) == (6,)
            assert _run_row(owned, aid) == (6, 3, 0)
            assert _value(
                owned, "SELECT result_set_state FROM device_activities"
                " WHERE id = ?", aid) == (4,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()
