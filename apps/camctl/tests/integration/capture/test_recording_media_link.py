"""录像尾段媒体链接线的组件集成测试。

真实停止确认后的录像处理器组合真实处理事务与媒体链编排：控制完
成固定无需检查决定后按控制依据登记成功；计时证据不足已固定需要
检查的处理行经媒体端口推进拷贝、检查与修复，判定装载已保存的检
查时长与修复成品，原片与修复成品同事务登记；媒体端口未装配或处
理未终局时保持等待，不提前判成功。
"""

from __future__ import annotations


from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.workflow_errors import registered_error
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.host_files.media import MediaProbe

from .test_capture_contract import _entry


pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_CONTENT = b"0123456789"


class _RepairTools:
    """工具替身：probe 返回固定时长，repair 返回完整成品。"""

    def __init__(self, duration_s: str) -> None:
        self.duration_s = duration_s
        self.calls: list[str] = []

    async def probe(self, input) -> FileTaskResult:
        self.calls.append("probe")
        return FileTaskResult(
            task_id=FileTaskId("probe"), ran=True,
            value=MediaProbe(duration_s=Decimal(self.duration_s), error=None))

    async def repair(self, input, output, *, trim_s) -> FileTaskResult:
        self.calls.append("repair")
        return FileTaskResult(
            task_id=FileTaskId("repair"), ran=True,
            value=SimpleNamespace(
                complete=True, size_bytes=len(_CONTENT), digest="a" * 64))


from ..support.capture_media_cases import ScriptTools, media_cases
from camctl.host_files.media import MediaStream


class _ScriptRepairTools(ScriptTools):
    def __init__(self, duration_s):
        value = Decimal(duration_s)
        super().__init__(MediaProbe(value, None, (
            MediaStream(0, 'video', 'h264', 64, 64, value, False),)))


async def _media(owned, tools, staging: Path):
    from ..support.capture_media_cases import ScriptTools, recording_media
    scripted = tools if isinstance(tools, ScriptTools) else _ScriptRepairTools(tools.duration_s)
    return await recording_media(owned, scripted, staging)


async def _runtime(owned, files, media=None):
    from ..support.capture_media_cases import recording_runtime
    return await recording_runtime(owned, files, media)


async def _environment(tmp_path: Path, *, check_decision: int, check_basis: str | None):
    from ..support.capture_media_cases import recording_environment
    return await recording_environment(tmp_path, check_decision=check_decision, check_basis=check_basis)


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


_INSUFFICIENT = '{"reason": 2, "target_duration_ms": 60000}'


class TestControlCompleteDecision:
    async def test_control_complete_saves_not_needed_and_succeeds(
            self, tmp_path: Path) -> None:
        owned, staging = await _environment(tmp_path, check_decision=1, check_basis=None)
        try:
            runtime = (await _runtime(owned, {1: (_entry("clip-1", size=10),)}))
            await capture_handler("camera_record")(1, runtime)
            # 控制完成即成功依据：终态与产物照常，处理决定固定为无需检查。
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason'),"
                " json_extract(check_basis_json, '$.target_duration_ms')"
                " FROM recording_processing WHERE id = 1")
            assert decision == (2, 1, 60000)
        finally:
            owned.connection.close()


class TestRequiredMediaChain:
    async def test_check_runs_and_repair_registers_repaired_output(
            self, tmp_path: Path) -> None:
        owned, staging = await _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _ScriptRepairTools("200")
            runtime = (await _runtime(
                owned, {1: (_entry("clip-1", size=10),)},
                media=(await _media(owned, tools, staging))))
            await capture_handler("camera_record")(1, runtime)
            # 计时证据不足：拷贝、检查与修复一次推进到终局；多录超门槛
            # 的修复成品与原片同事务登记为正式产物。
            assert tools.calls == ["probe", "repair"]
            # 列举确认在场，读取绑定首次声明源端摘要能力。
            assert _value(
                owned, "SELECT presence_state, checksum_support"
                " FROM device_files WHERE id = 1") == (2, 2)
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            processing = _value(
                owned,
                "SELECT check_state, repair_state, repair_output_file_id"
                " FROM recording_processing WHERE id = 1")
            assert processing[0] == 3
            assert processing[1] == 5
            assert processing[2] is not None
            kinds = owned.connection.execute(
                "SELECT kind FROM outputs WHERE source_action_id = 1"
                " ORDER BY kind").fetchall()
            assert kinds == [(1,), (2,)]
            assert _value(
                owned,
                "SELECT retention_state FROM intermediate_files WHERE id = ?",
                processing[2]) == (3,)
        finally:
            owned.connection.close()

    async def test_short_duration_fails_and_keeps_original(
            self, tmp_path: Path) -> None:
        owned, staging = await _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _ScriptRepairTools("55")
            runtime = (await _runtime(
                owned, {1: (_entry("clip-1", size=10),)},
                media=(await _media(owned, tools, staging))))
            await capture_handler("camera_record")(1, runtime)
            # 检查时长不足目标：按 recording_too_short 失败，原片保留。
            assert tools.calls == ["probe"]
            failure = _value(
                owned, "SELECT status, error_code FROM actions WHERE id = 1")
            assert failure == (4, registered_error("recording_too_short")
                               ["action_error_id"])
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE kind = 2") == (0,)
        finally:
            owned.connection.close()

    async def test_missing_media_port_keeps_action_running(
            self, tmp_path: Path) -> None:
        owned, staging = await _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            runtime = (await _runtime(owned, {1: (_entry("clip-1", size=10),)}))
            await capture_handler("camera_record")(1, runtime)
            # 媒体端口未装配：检查不能执行也不猜测结果，保持执行中等待。
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 2
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _value(
                owned, "SELECT check_state FROM recording_processing"
                " WHERE id = 1") == (1,)
        finally:
            owned.connection.close()

    async def test_missing_source_fails_source_unavailable(
            self, tmp_path: Path) -> None:
        owned, staging = await _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _ScriptRepairTools("61")
            runtime = (await _runtime(
                owned, {}, media=(await _media(owned, tools, staging))))
            await capture_handler("camera_record")(1, runtime)
            # 需要检查但没有可归属的原片：无可用输入，按有限失败收场。
            failure = _value(
                owned,
                "SELECT status, error_code,"
                " json_extract(error_details_json, '$.reason')"
                " FROM actions WHERE id = 1")
            assert failure == (4, registered_error("recording_processing_failed")
                               ["action_error_id"], "source_unavailable")
            assert tools.calls == []
        finally:
            owned.connection.close()
