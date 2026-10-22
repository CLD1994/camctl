"""按冻结视频顺序检查实际内容，固定达到目标的完整文件前缀。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentCursor, SegmentKey,
    SegmentMemberRole,
)
from camctl.capture.video_coverage import (
    CoverageAccumulator, CoverageDecision, CoverageFinishFacts, CoverageTarget,
    VideoMetric, advance_coverage, finish_coverage,
)


@dataclass(frozen=True)
class CheckedPrefix:
    coverage: CoverageDecision
    first_cursor: SegmentCursor | None
    last_cursor: SegmentCursor | None


async def inspect_continuous_prefix(
    members: Iterable,
    target: CoverageTarget,
    *,
    start: SegmentKey | None,
    collection_complete: bool,
    inspect: Callable[[object], Awaitable[VideoMetric | None]],
) -> CheckedPrefix | None:
    """逐个消费已冻结成员；None 表示当前检查仍在推进。

    inspect 返回可靠指标或带原因的未知指标，尚在读取时返回 None。
    已保存指标由检查仓储恢复，因而重入不会重新拷贝已完成文件。
    照片和预览不提供顺序、起点或覆盖量。
    """
    state = CoverageAccumulator(target)
    first = last = None
    reason = None
    if start is None:
        reason = "start_unknown"
    else:
        expected = start
        for stored in members:
            member = stored.member
            if member.role is not SegmentMemberRole.ORIGINAL:
                continue
            if member.key != expected:
                reason = "order_unknown" if member.key is None else "segment_gap"
                break
            if (member.ownership is not OwnershipState.OWNED
                    or member.completion is not FileCompletionState.COMPLETE
                    or member.size_bytes is None):
                reason = "file_unconfirmed"
                break
            if member.size_bytes == 0:
                reason = "empty_original"
                break
            metric = await inspect(stored)
            if metric is None:
                return None
            state = advance_coverage(state, metric)
            if state.interruption_reason is not None:
                reason = state.interruption_reason
                break
            if first is None:
                first = member.cursor
            last = member.cursor
            if state.reached:
                break
            expected = member.key.successor()
    decision = finish_coverage(state, CoverageFinishFacts(
        collection_complete=collection_complete and reason is None,
        interruption_reason=reason,
    ))
    return CheckedPrefix(decision, first, last)


async def advance_action6_recovery(runtime, action, decision=None):
    """实际处理器的异常入口；检查事实可跨推进和会话恢复。"""
    from dataclasses import replace

    from camctl.capture import handlers as common
    from camctl.capture.action6_completion import (
        advance_action6_segmented_recording, classify_action6_segment_set,
        prepare_action6_segment_set, publish_action6_segment_result,
    )
    from camctl.capture.segment_completion import (
        _control_facts, _members, release_segment_occupancy, resume_segment_writes,
    )
    from camctl.capture.segment_inspections import (
        SegmentInspectionPhase, SegmentInspectionRequest, inspect_segment,
    )
    from camctl.capture.segment_models import (
        CoverageStatus, DefaultScopeState, EvidenceState, SegmentDecisionScope,
        SegmentDecisionStatus,
    )
    from camctl.contracts.enums import enum_for
    from camctl.devices.segment_profiles import profile_for_policy
    from camctl.outputs.qualification import OperationConfig
    from camctl.persistence.repositories.segments import SegmentRepository
    from camctl.persistence.repositories.segment_inspections import read_inspection_for_member
    from camctl.persistence.transaction import row_facts

    action_id = action["id"]
    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    action = runtime.action(action_id)
    if action["status"] in common._ACTION_TERMINAL:
        return
    if action["cancel_requested"]:
        await advance_action6_segmented_recording(runtime, action, decision)
        return
    # 受限时钟会话只保存控制和文件进度；媒体检查由正常会话执行。
    if runtime.media is None:
        return
    repository = SegmentRepository()
    ref = await prepare_action6_segment_set(runtime, action, None)
    if ref is None:
        return
    release_segment_occupancy(runtime, action_id)
    setrow = repository.read_result_set(ref, runtime.owned)
    prior = repository.read_assessment(ref, runtime.owned)
    if prior is not None:
        publish_action6_segment_result(runtime, action, ref, prior)
        return
    activity = row_facts(runtime.owned.connection, "device_activities", setrow["activity_id"])
    spec = action["execution_spec_json"]
    target = profile_for_policy(spec["result_policy"]).coverage_target(
        action["effective_params_json"], spec,
        enum_for("actions.type")(action["type"]).name.lower(),
    )
    flow = runtime.media
    budget = OperationConfig(flow.max_read_attempts, flow.read_idle_timeout_s,
                             flow.retry_interval_s)
    explicit_media_failure = False

    async def inspect(stored):
        nonlocal explicit_media_failure
        existing = read_inspection_for_member(action_id, stored.member_id, runtime.owned)
        saved = existing.inspection
        adopted_budget = budget if saved is None else OperationConfig(**saved.budget_json)
        step = await inspect_segment(flow, SegmentInspectionRequest(
            action_id, stored.member_id, target, adopted_budget, runtime.wall_us(),
        ))
        if step.phase is SegmentInspectionPhase.WAITING:
            return None
        if step.phase is SegmentInspectionPhase.FAILED:
            explicit_media_failure = True
        return step.metric or VideoMetric(target.metric, None,
                                         step.reason or "inspection_unconfirmed")

    prefix = await inspect_continuous_prefix(
        _members(repository, ref, runtime), target,
        start=repository.read_start(ref, runtime.owned).key,
        collection_complete=setrow["source_kind"] == int(
            enum_for("segment_sets.source_kind").COMPLETE_SCAN),
        inspect=inspect,
    )
    if prefix is None:
        return
    coverage = prefix.coverage
    scope = SegmentDecisionScope(
        DefaultScopeState.NONEMPTY if coverage.confirmed_segments else DefaultScopeState.EMPTY,
        ref, prefix.first_cursor, prefix.last_cursor, coverage.confirmed_segments,
    )
    facts = classify_action6_segment_set(
        action, ref, repository, runtime.owned,
        coverage_status=CoverageStatus(coverage.status.value),
        confirmed=coverage.confirmed, checked_count=coverage.confirmed_segments,
        default_scope=scope,
    )
    result = facts.decision
    native = action["type"] == int(enum_for("actions.type").CAMERA_TIMELAPSE)
    if native:
        control_complete = common._natural_file_completion_context(runtime, action_id) is not None
    else:
        control_complete = _control_facts(runtime, action, activity)[0] is EvidenceState.CONFIRMED
    explicit_failure = (
        result.status is SegmentDecisionStatus.FAILED
        or explicit_media_failure
        or coverage.status.value == "confirmed_short"
        or coverage.reason in ("segment_gap", "empty_original")
    )
    if explicit_failure:
        result = replace(result, status=SegmentDecisionStatus.FAILED,
                         failure_code="capture_failed", reason="invalid_outputs")
    elif control_complete or coverage.status.value == "reached":
        result = replace(result, status=SegmentDecisionStatus.SUCCEEDED,
                         failure_code=None, reason=coverage.reason)
    else:
        result = replace(result, status=SegmentDecisionStatus.UNCONFIRMED,
                         failure_code="capture_result_unconfirmed",
                         reason=coverage.reason or "completion_unknown")
    publish_action6_segment_result(runtime, action, ref, result)
