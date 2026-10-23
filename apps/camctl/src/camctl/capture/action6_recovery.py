"""按冻结视频顺序检查实际内容，固定达到目标的完整文件前缀。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentCursor, SegmentKey,
    SegmentMemberRole,
)
from camctl.contracts.video_coverage import (
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
        previous_key = None
        iterator = iter(members)

        def next_original():
            for candidate in iterator:
                candidate_member = candidate.member
                if (candidate_member.role is SegmentMemberRole.ORIGINAL
                        and candidate_member.ownership is not OwnershipState.NOT_OWNED):
                    return candidate
            return None

        stored = next_original()
        while stored is not None:
            member = stored.member
            following = next_original()
            if member.key != expected:
                reason = (
                    "order_unknown"
                    if member.key is None or member.key == previous_key
                    else "segment_gap"
                )
                break
            if (member.key is not None and following is not None
                    and following.member.key == member.key):
                # 相同设备键的原片顺序无法确定。先查看相邻成员，避免在
                # 冲突组内任意选一份进行复制或媒体探测；next_original 会
                # 跨越底层分页继续，因此页边界不会改变判断。
                reason = "order_unknown"
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
            previous_key = member.key
            stored = following
    decision = finish_coverage(state, CoverageFinishFacts(
        collection_complete=collection_complete and reason is None,
        interruption_reason=reason,
    ))
    return CheckedPrefix(decision, first, last)


def finalize_pending_action6_inspections(runtime, action_id: int, *, reason: str) -> bool:
    """在来源取消或已终态时结束尚未完成的检查责任，不继续读取设备。"""
    from types import SimpleNamespace

    from camctl.capture.segment_inspections import InspectionResultSave
    from camctl.capture.segment_inspections import SegmentInspectionRepository
    from camctl.capture.segment_inspections import settle_terminal_inspection_slots_for_action
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.contracts.enums import enum_for
    from camctl.contracts.values import ConsistencyError

    owned = runtime.owned
    flow = getattr(runtime, "media", None)
    pending = (None if flow is None else flow.pending_segment_inspection_results)
    repository = SegmentInspectionRepository()
    state = enum_for("segment_inspections.state").UNCONFIRMED
    rows = owned.connection.execute(
        "SELECT id FROM segment_inspections WHERE action_id=? AND state=? ORDER BY id",
        (action_id, int(enum_for("segment_inspections.state").PENDING)),
    ).fetchall()
    for (inspection_id,) in rows:
        held = None if pending is None else pending.get(inspection_id)
        if held is not None:
            request, key = held
        else:
            inspection = repository.read(inspection_id, owned)
            metric = inspection.target_json.get("metric")
            if not isinstance(metric, str) or not metric:
                raise ConsistencyError("未结束的 Action6 检查缺少原冻结量纲")
            request = InspectionResultSave(
                inspection_id=inspection_id,
                state=int(state),
                result_json={
                    "metric": {"metric": metric, "value": None, "reason": reason},
                    "probe": None,
                    "tool": {"ran": False, "error": None},
                    "failure": {"code": "inspection_unconfirmed", "reason": reason},
                },
                occurred_at=runtime.wall_us(),
            )
            key = new_operation_key()
        outcome = repository.save_result(request, key, owned)
        if outcome.kind is DbOutcomeKind.COMPLETED:
            if pending is not None:
                pending.pop(inspection_id, None)
            continue
        if outcome.kind is DbOutcomeKind.UNKNOWN:
            if pending is not None:
                pending[inspection_id] = (request, key)
            return False
        raise ConsistencyError(f"Action6 未结束检查收场被拒绝: {outcome.error}")
    slot_flow = flow if flow is not None else SimpleNamespace(
        owned=owned, pending_read_business=runtime.pending_read_business,
    )
    # READ 仍 ACTIVE 或尚有重试等待时只保留原 slot；这不应阻断既有
    # Action6 取消终态。后续 Action6 恢复会在 READ 自身终结后补做释放。
    settle_terminal_inspection_slots_for_action(slot_flow, action_id, runtime.wall_us())
    return True


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
    from camctl.host_files.media import select_main_video_stream

    action_id = action["id"]
    resume_segment_writes(runtime.owned, runtime.pending_segment_writes, action_id)
    action = runtime.action(action_id)
    if action["status"] in common._ACTION_TERMINAL:
        finalize_pending_action6_inspections(runtime, action_id, reason="source_terminal")
        return
    if action["cancel_requested"]:
        if not finalize_pending_action6_inspections(
            runtime, action_id, reason="inspection_canceled"
        ):
            return
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
    setrow = repository.read_set(ref, runtime.owned)
    if setrow["state"] == int(enum_for("segment_sets.state").ASSESSED):
        prior = repository.read_decision(ref, runtime.owned)
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
        adopted_budget = budget if saved is None else OperationConfig.from_document(
            saved.budget_json
        )
        step = await inspect_segment(flow, SegmentInspectionRequest(
            action_id, stored.member_id, target, adopted_budget, runtime.wall_us(),
        ))
        if step.phase is SegmentInspectionPhase.WAITING:
            return None
        # FAILED 还包括读次数耗尽和媒体工具异常；它们只让覆盖前缀
        # 未确认。只有解析器成功解析后明确报告无主视频流才是已知媒体缺陷。
        probe = step.probe
        if (probe is not None and probe.error is not None
                and probe.error.split(":", 1)[0].strip() == "no_video_stream"
                and select_main_video_stream(probe) is None):
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
    unknown_scope = (
        coverage.confirmed_segments == 0
        and coverage.reason in ("start_unknown", "order_unknown")
    )
    scope = SegmentDecisionScope(
        (DefaultScopeState.UNKNOWN if unknown_scope else
         DefaultScopeState.NONEMPTY if coverage.confirmed_segments else
         DefaultScopeState.EMPTY),
        ref,
        None if unknown_scope else prefix.first_cursor,
        None if unknown_scope else prefix.last_cursor,
        None if unknown_scope else coverage.confirmed_segments,
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
