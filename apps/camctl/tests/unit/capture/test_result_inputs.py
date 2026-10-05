"""结果集合核实与等待完成的输入契约单元测试。"""

from __future__ import annotations

import pytest

from camctl.capture.models import (
    ResultSetPhase,
    ResultSetSave,
    WaitCompletedSave,
)

_NOW = 1_750_000_000_000_000


def test_begin_rejects_conclusion_members() -> None:
    """开始分支只推进核实状态，携带结论事实属于输入错误。"""
    with pytest.raises(ValueError, match="开始分支"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.BEGIN,
            contract="task_scope_files")


def test_conclusion_requires_contract_and_observation() -> None:
    """结论分支缺少规则标识或结构化依据都拒绝。"""
    with pytest.raises(ValueError, match="规则标识"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            observation={"files": []})
    with pytest.raises(ValueError, match="结构化依据"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.UNSATISFIED,
            contract="task_scope_files")


@pytest.mark.parametrize("phase,status", [
    (ResultSetPhase.COMPLETE, "failed"),
    (ResultSetPhase.UNSATISFIED, "completed"),
    (ResultSetPhase.UNCONFIRMED, "running"),
])
def test_capture_status_must_match_phase(phase, status) -> None:
    """采集状态与结论分支不符拒绝。"""
    capture = {"status": status}
    if status != "completed":
        capture["error"] = {"code": "capture_unsatisfied"}
    with pytest.raises(ValueError, match="结论不符"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=phase,
            contract="task_scope_files", observation={}, capture=capture,
            evidence=None if phase is ResultSetPhase.UNCONFIRMED else {
                "method": "known_failure", "observation": {}})


def test_non_completed_capture_requires_error_member() -> None:
    """失败与未知采集状态必须携带错误成员；完成状态不得携带。"""
    with pytest.raises(ValueError, match="错误成员"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.UNSATISFIED,
            contract="task_scope_files", observation={},
            capture={"status": "failed"},
            evidence={"method": "known_failure", "observation": {}})
    with pytest.raises(ValueError, match="错误成员"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            capture={"status": "completed", "error": {"code": "x"}},
            evidence={"method": "time_and_outputs", "observation": {}})


@pytest.mark.parametrize("count", [-1, 1.5, True, 2**53])
def test_captured_count_must_be_safe_integer(count) -> None:
    """采集次数必须是 0～2^53-1 的安全整数。"""
    with pytest.raises(ValueError, match="采集次数"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            capture={"status": "completed", "captured_count": count},
            evidence={"method": "time_and_outputs", "observation": {}})


def test_evidence_requires_method_and_observation() -> None:
    """判定依据必须有非空方法与实际观察对象，未知成员拒绝。"""
    with pytest.raises(ValueError, match="方法"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            evidence={"observation": {}})
    with pytest.raises(ValueError, match="实际观察"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            evidence={"method": "time_and_outputs"})
    with pytest.raises(ValueError, match="未知成员"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            evidence={"method": "time_and_outputs", "observation": {},
                      "totals": 3})


def test_unconfirmed_rejects_determination() -> None:
    """无法确认分支不判定采集结果；满足分支不携带核实错误。"""
    with pytest.raises(ValueError, match="不判定"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.UNCONFIRMED,
            contract="task_scope_files", observation={},
            evidence={"method": "time_and_outputs", "observation": {}})
    with pytest.raises(ValueError, match="核实错误"):
        ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
            contract="task_scope_files", observation={},
            error={"code": "x"},
            evidence={"method": "time_and_outputs", "observation": {}})


def test_wait_completed_requires_typed_identities() -> None:
    """等待完成只携带活动身份与事实时刻，类型不符拒绝。"""
    with pytest.raises(ValueError):
        WaitCompletedSave(action_id=0, occurred_at=_NOW)
    with pytest.raises(ValueError):
        WaitCompletedSave(action_id=1, occurred_at="now")
