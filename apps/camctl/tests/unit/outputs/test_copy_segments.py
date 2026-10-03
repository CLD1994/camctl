"""分段拷贝的段计划与保存申请纯规则。

行为契约为[进度保存的分段大小](../../../architecture/file-copy.md#进度保存的分段大小)：
E = C + min(S, N - C)，无剩余内容不产生空段；可靠进度只在字节
写入并同步成功后按事务保存。本文件只验证纯计算与输入校验，
真实文件与数据库组合由集成测试承担。
"""

from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import (
    AttemptConfig, AttemptTicket, ReadResumeRequest,
)
from camctl.outputs.copy import (
    ReliableSegment, SegmentFacts, SegmentOutcome, plan_segment,
)


# ---- 段计划分区 ----


@pytest.mark.parametrize(
    "committed,size,segment,expected",
    [
        (0, 10, 4, (0, 4, False)),      # 首段：处理到 C+S
        (4, 10, 4, (4, 8, False)),      # 中间段
        (8, 10, 4, (8, 10, True)),      # 剩余小于一段：最后一段到 N
        (6, 10, 4, (6, 10, True)),      # 剩余恰好一段：到 N 后进入完整性确认
        (0, 4, 4, (0, 4, True)),        # 整个文件恰为一段
        (3, 10, 4, (3, 7, False)),      # 旧进度不是新段大小倍数：从原位置继续
        (5, 10, 4, (5, 9, False)),      # 不向上或向下对齐
    ],
)
def test_segment_plan_partitions(committed, size, segment, expected):
    start, end, final = expected
    plan = plan_segment(SegmentFacts(
        source_size=size, committed_bytes=committed, segment_size=segment))
    assert plan.outcome is SegmentOutcome.COPY
    assert (plan.start, plan.end, plan.final) == (start, end, final)


@pytest.mark.parametrize("committed,size", [(10, 10), (7, 7), (0, 0)])
def test_no_content_left_plans_no_segment(committed, size):
    """已无文件内容需要读取：不新增空进度段，进入所属流程的完整性确认。"""
    plan = plan_segment(SegmentFacts(
        source_size=size, committed_bytes=committed, segment_size=4))
    assert plan.outcome is SegmentOutcome.ALL_COMMITTED
    assert plan.start is None and plan.end is None
    assert plan.final is False


def test_progress_beyond_source_is_consistency_error():
    with pytest.raises(ConsistencyError):
        plan_segment(SegmentFacts(source_size=6, committed_bytes=7, segment_size=4))


@pytest.mark.parametrize("field,value", [
    ("segment_size", 0), ("segment_size", -1), ("segment_size", True),
    ("segment_size", "4"), ("source_size", -1), ("source_size", True),
    ("committed_bytes", -1), ("committed_bytes", 1.5),
])
def test_invalid_segment_facts_rejected(field, value):
    values = {"source_size": 10, "committed_bytes": 0, "segment_size": 4}
    values[field] = value
    with pytest.raises(ValueError):
        plan_segment(SegmentFacts(**values))


def test_plan_requires_segment_facts():
    with pytest.raises(TypeError):
        plan_segment({"source_size": 10, "committed_bytes": 0, "segment_size": 4})


# ---- 保存申请校验 ----


def _segment(**overrides):
    values = {
        "copy_id": 7, "copy_round": 1, "committed_before": 4,
        "segment_end": 8, "synced": True, "occurred_at": 1_700_000_000_000_000,
    }
    values.update(overrides)
    return ReliableSegment(**values)


def test_reliable_segment_accepts_advancing_synced_range():
    command = _segment()
    assert command.committed_before == 4 and command.segment_end == 8


def test_unsynced_segment_cannot_be_submitted():
    """段尾同步未确认的字节不属于可靠进度，不能构造保存申请。"""
    with pytest.raises(ValueError, match="同步"):
        _segment(synced=False)


@pytest.mark.parametrize("overrides", [
    {"segment_end": 4},                       # 不推进的段
    {"segment_end": 2},                       # 范围倒退
    {"committed_before": -1},
    {"segment_end": -1},
    {"copy_round": 0},
    {"copy_round": True},
    {"copy_id": 0},
    {"occurred_at": "now"},
])
def test_reliable_segment_rejects_invalid_inputs(overrides):
    with pytest.raises(ValueError):
        _segment(**overrides)


# ---- 恢复在途读取尝试的配置申请 ----


def _ticket() -> AttemptTicket:
    return AttemptTicket(
        attempt_id=2, operation="read", target_id="7",
        responsibility_key="read/7", run_id=5,
    )


def test_read_resume_request_accepts_ticket_and_config():
    request = ReadResumeRequest(
        ticket=_ticket(), config=AttemptConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=1_700_000_000_000_000,
    )
    assert request.config.max_attempts == 3


def test_read_resume_request_requires_read_operation():
    """只有读取尝试可以恢复配置；其他操作类别直接拒绝。"""
    ticket = AttemptTicket(
        attempt_id=1, operation="stop", target_id="9",
        responsibility_key="stop/11", run_id=3,
    )
    with pytest.raises(ValueError, match="读取"):
        ReadResumeRequest(
            ticket=ticket, config=AttemptConfig(3), occurred_at=1,
        )


@pytest.mark.parametrize("overrides", [
    {"config": (3, "10", "0")},
    {"occurred_at": None},
    {"ticket": None},
])
def test_read_resume_request_rejects_invalid_inputs(overrides):
    values = {
        "ticket": _ticket(),
        "config": AttemptConfig(3, Decimal("10"), Decimal("0")),
        "occurred_at": 1_700_000_000_000_000,
    }
    values.update(overrides)
    with pytest.raises((ValueError, TypeError)):
        ReadResumeRequest(**values)
