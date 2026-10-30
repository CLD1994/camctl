"""实际 START 会话结束后，新工厂以耐久墙钟对账并停止、登记或修复。"""

import json
from pathlib import Path

import pytest

from ..support.recording_consumer_cases import NOW, reconciliation_case, recording_cases

pytestmark = pytest.mark.asyncio


def _value(owned, sql, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, sql
    return row


class TestCrossSessionReconciliation:
    async def test_satisfied_timing_stops_and_succeeds(
            self, tmp_path: Path) -> None:
        case = await reconciliation_case(tmp_path, started_at_offset_s=65)
        owned = case.owned
        try:
            await case.advance()
            # 可信计时证明 65 秒已满足目标且未超门槛：立即停止，恢复
            # 控制完成即成功依据。
            assert case.stop_calls == ["stop_recording"]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason')"
                " FROM recording_processing WHERE id = 1")
            assert decision == (2, 1)
            # 恢复对账入口的释放组合：停止成功后活动收场并释放占
            # 用，同设备下一动作不再被该活动阻挡（O-04）。
            assert _value(
                owned, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 2)
        finally:
            await case.close()

    async def test_unsatisfied_timing_keeps_waiting_then_recovers(
            self, tmp_path: Path) -> None:
        case = await reconciliation_case(tmp_path, started_at_offset_s=30)
        owned = case.owned
        try:
            await case.advance()
            # 尚未满足目标时长：不停止、不定决定，保持执行中等待。
            assert case.stop_calls == []
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 2
            assert _value(
                owned, "SELECT check_decision FROM recording_processing"
                " WHERE id = 1") == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_runs"
                " WHERE responsibility_key = 'stop/1'") == (0,)
            # 剩余时长经过后（新会话、更晚的可信墙钟）对账满足并停止；
            # 恰好达到门槛含等号，不触发修复。
            await case.restart(wall=NOW + 40_000_000)
            owned = case.owned
            await case.advance()
            assert case.stop_calls == ["stop_recording"]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason')"
                " FROM recording_processing WHERE id = 1")
            assert decision == (2, 1)
        finally:
            await case.close()

    async def test_excess_duration_triggers_repair_without_check(
            self, tmp_path: Path) -> None:
        case = await reconciliation_case(tmp_path, started_at_offset_s=120, repair=True)
        owned = case.owned
        try:
            tools = case.tools
            await case.advance()
            # 恢复停止确认的控制耗时 120 秒超过目标加余量（70 秒）：
            # 保存异常多录修复决定，不经检查直接修复，成品与原片同
            # 事务登记。
            assert case.stop_calls == ["stop_recording"]
            assert tools.calls == ["repair"]
            processing = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason'),"
                " repair_state, json_extract(repair_basis_json, '$.reason'),"
                " repair_output_file_id"
                " FROM recording_processing WHERE id = 1")
            assert processing[0] == 2
            assert processing[1] == 3
            assert processing[2] == 5
            assert processing[3] == 2
            assert processing[4] is not None
            repaired_path = Path(case.config.paths.staging) / _value(owned,
                "SELECT relative_path FROM intermediate_files WHERE id=?", processing[4])[0]
            assert repaired_path.read_bytes() == tools.content
            import hashlib
            assert _value(owned, "SELECT size_bytes,sha256 FROM intermediate_files WHERE id=?",
                processing[4]) == (len(tools.content), hashlib.sha256(tools.content).hexdigest())
            basis = json.loads(_value(owned,
                "SELECT repair_basis_json FROM recording_processing WHERE id=1")[0])
            assert basis["control_elapsed_ns"] == 120000000000
            assert "actual_duration_s" not in basis
            kinds = owned.connection.execute(
                "SELECT kind FROM outputs WHERE source_action_id = 1"
                " ORDER BY kind").fetchall()
            assert kinds == [(1,), (2,)]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
        finally:
            await case.close()
