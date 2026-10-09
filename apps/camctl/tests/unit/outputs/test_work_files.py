"""中间文件清理判定与历史扫描预算的纯规则单元测试。

对应[取回中间文件的保留与清理](../../../../architecture/obtaining-outputs.md#取回中间文件的保留与清理)
决策表与[中间文件清理的运行预算](../../../../architecture/file-handoff.md#中间文件清理的运行预算)：
保留状态优先于清理判定，归属与操作状态分别核实，历史扫描按
固定上界、剩余额度与单轮绕回有限推进。
"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.outputs.work_files import (
    CleanupScan, WorkFileAction, WorkFileFacts, WorkFileLimits,
    classify_work_file,
)

_RETENTION = enum_for("intermediate_files.retention_state")
_CLEANUP = enum_for("intermediate_files.cleanup_state")
_PURPOSE = enum_for("intermediate_files.purpose")


def _facts(**overrides):
    base = dict(
        file_id=28,
        purpose=int(_PURPOSE.DELIVERY_COPY),
        owner_action_id=None,
        owner_delivery_id=41,
        relative_path="deliveries/28.bin",
        retention_state=int(_RETENTION.RELEASABLE),
        cleanup_state=int(_CLEANUP.PENDING),
        owner_finished=True,
        operations_stopped=True,
    )
    base.update(overrides)
    return WorkFileFacts(**base)


# ---- 清理判定决策表 ----


def test_owner_active_file_is_kept():
    """归属交付或动作尚未终态：保留文件及其续传、发布责任。"""
    decision = classify_work_file(_facts(owner_finished=False))
    assert decision.action is WorkFileAction.KEEP_REQUIRED


@pytest.mark.parametrize("retention", [
    int(_RETENTION.PROMOTED), int(_RETENTION.HANDED_OFF),
])
def test_promoted_and_handed_off_are_not_managed(retention):
    """正式产物与已交接副本不归中间文件自动清理管理。"""
    decision = classify_work_file(_facts(
        retention_state=retention,
        cleanup_state=int(_CLEANUP.NOT_NEEDED),
    ))
    assert decision.action is WorkFileAction.NOT_MANAGED


def test_completed_cleanup_is_already_cleaned():
    decision = classify_work_file(
        _facts(cleanup_state=int(_CLEANUP.COMPLETED)))
    assert decision.action is WorkFileAction.ALREADY_CLEANED


def test_owner_active_takes_priority_over_operations():
    decision = classify_work_file(
        _facts(owner_finished=False, operations_stopped=False))
    assert decision.action is WorkFileAction.KEEP_REQUIRED


def test_running_operations_keep_file():
    """归属已终态但相关操作未确认停止：先收场，不删除仍可能被使用的副本。"""
    decision = classify_work_file(_facts(operations_stopped=False))
    assert decision.action is WorkFileAction.KEEP_IN_USE


@pytest.mark.parametrize("retention, cleanup", [
    (int(_RETENTION.REQUIRED), int(_CLEANUP.NOT_NEEDED)),
    (int(_RETENTION.RELEASABLE), int(_CLEANUP.PENDING)),
    (int(_RETENTION.RELEASABLE), int(_CLEANUP.RUNNING)),
    (int(_RETENTION.RELEASABLE), int(_CLEANUP.FAILED)),
    (int(_RETENTION.RELEASABLE), int(_CLEANUP.UNKNOWN)),
])
def test_finished_and_stopped_candidate_is_deletable(retention, cleanup):
    """归属终态且操作停止：REQUIRED 由编排先释放，其余直接清理。"""
    decision = classify_work_file(
        _facts(retention_state=retention, cleanup_state=cleanup))
    assert decision.action is WorkFileAction.DELETE


def test_decision_carries_reason():
    decision = classify_work_file(
        _facts(retention_state=int(_RETENTION.PROMOTED),
               cleanup_state=int(_CLEANUP.NOT_NEEDED)))
    assert isinstance(decision.reason, str) and decision.reason


# ---- 事实输入校验 ----


@pytest.mark.parametrize("overrides", [
    {"purpose": int(_PURPOSE.DELIVERY_COPY), "owner_action_id": 31,
     "owner_delivery_id": None},
    {"purpose": int(_PURPOSE.RECORDING_INPUT), "owner_action_id": None,
     "owner_delivery_id": 41},
    {"owner_action_id": None, "owner_delivery_id": None},
    {"owner_action_id": 31, "owner_delivery_id": 41},
])
def test_purpose_owner_combination_is_validated(overrides):
    with pytest.raises(ValueError):
        _facts(**overrides)


@pytest.mark.parametrize("field, value", [
    ("retention_state", 0), ("retention_state", 5),
    ("retention_state", True),
    ("cleanup_state", 0), ("cleanup_state", 7),
    ("cleanup_state", True),
    ("purpose", 0), ("purpose", 5), ("purpose", True),
])
def test_state_values_are_validated(field, value):
    with pytest.raises(ValueError):
        _facts(**{field: value})


@pytest.mark.parametrize("overrides", [
    {"file_id": 0}, {"file_id": -1}, {"file_id": True},
    {"relative_path": ""}, {"relative_path": 28},
    {"owner_finished": 1}, {"operations_stopped": None},
])
def test_fact_types_are_validated(overrides):
    with pytest.raises(ValueError):
        _facts(**overrides)


def test_classify_requires_facts_type():
    with pytest.raises(TypeError):
        classify_work_file({"file_id": 28})


# ---- 运行预算 ----


@pytest.mark.parametrize("batch, limit", [
    (0, 1), (1, 0), (-1, 4), (True, 4), (4, True), (1.5, 4), (4, 4.0),
])
def test_limits_reject_invalid_values(batch, limit):
    with pytest.raises(ValueError):
        WorkFileLimits(batch_size=batch, limit_per_run=limit)


def test_limits_accept_minimum_one():
    limits = WorkFileLimits(batch_size=1, limit_per_run=1)
    assert limits.batch_size == 1 and limits.limit_per_run == 1


def _scan(**overrides):
    base = dict(start_after=0, ceiling=10, remaining=4, batch_size=2)
    base.update(overrides)
    return CleanupScan(**base)


def test_scan_query_caps_batch_to_remaining():
    scan = _scan(start_after=2, after_id=2, remaining=3, batch_size=32)
    assert scan.next_query() == ((2, 3), scan)


def test_scan_query_stops_without_budget():
    assert _scan(remaining=0).next_query() is None


def test_scan_query_stops_without_scope():
    assert _scan(ceiling=0).next_query() is None


def test_scan_wraps_once_after_ceiling():
    """到达固定上界后绕回候选范围起点，绕回后到本次起点即结束。"""
    scan = _scan(start_after=6, after_id=10)
    query = scan.next_query()
    assert query is not None
    (after, limit), wrapped_scan = query
    assert after == 0 and limit == 2
    assert wrapped_scan.wrapped
    # 绕回段读空后位置推进到本次起点，再查询即结束。
    finished = wrapped_scan.exhausted_to_end()
    assert finished.after_id == 6
    assert finished.next_query() is None


def test_scan_advances_position_and_budget():
    scan = _scan(start_after=6, after_id=6)
    step = scan.advanced(7)
    assert step.after_id == 7 and step.remaining == 3
    assert step.next_query() == ((7, 2), step)


def test_scan_full_round_golden_path():
    """四条候选跨上界分布：第一轮读到上界，绕回后读剩余，额度耗尽结束。"""
    scan = _scan(start_after=6, after_id=6, remaining=4, batch_size=2)
    window = {7, 8, 10, 3}
    processed: set[int] = set()
    seen: list[tuple[int, int]] = []
    query = scan.next_query()
    while query is not None:
        (after, limit), scan = query
        seen.append((after, limit))
        # 模拟数据库返回本段区间仍未完成的候选。
        batch = [file_id for file_id in sorted(window)
                 if after < file_id <= scan.segment_ceiling()
                 and file_id not in processed][:limit]
        if not batch:
            scan = scan.exhausted_to_end()
        else:
            for file_id in batch:
                processed.add(file_id)
                scan = scan.advanced(file_id)
        query = scan.next_query()
    assert seen == [(6, 2), (8, 2), (0, 1)]
    assert scan.remaining == 0


def test_scan_wrapped_segment_stops_at_start():
    """绕回段的读取上界是本次起始继续位置，不含其后的记录。"""
    scan = _scan(start_after=6, after_id=0, wrapped=True, remaining=2)
    assert scan.segment_ceiling() == 6
    stepped = scan.advanced(5)
    # 5 仍在起点之前，绕回段可以继续读取。
    assert stepped.next_query() == ((5, 1), stepped)
    finished = stepped.advanced(6)
    assert finished.next_query() is None


@pytest.mark.parametrize("start,expected", [(20, 20), (100, 100), (900, 100)])
def test_wrapped_scan_never_crosses_fixed_registration_ceiling(start, expected):
    scan = _scan(start_after=start, after_id=0, ceiling=100, wrapped=True)
    assert scan.segment_ceiling() == expected
    exhausted = scan.exhausted_to_end()
    assert exhausted.after_id == expected
    assert exhausted.next_query() is None


def test_cursor_above_current_candidates_wraps_only_to_fixed_ceiling():
    scan = _scan(start_after=900, after_id=900, ceiling=100)
    (_after, _limit), wrapped = scan.next_query()
    assert wrapped.segment_ceiling() == 100
    assert wrapped.advanced(100).next_query() is None


def test_scan_rejects_checked_record_past_current_fixed_segment():
    scan = _scan(start_after=900, after_id=0, ceiling=100, wrapped=True)
    with pytest.raises(ValueError):
        scan.advanced(500)


def test_cursor_above_empty_scope_does_not_create_wrapped_window():
    scan = _scan(start_after=900, after_id=900, ceiling=0)
    assert scan.next_query() is None
