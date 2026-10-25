"""Action6 分段原结果的公共分类、范围保存与共同终态。"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from camctl.capture.media import RecordingFailure
from camctl.capture.results import (
    CaptureAssessment,
    CaptureFile,
    CaptureFileSet,
    FileKind,
    ProductRequirements,
    ProductRule,
    assess_capture_files,
)
from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.devices.tasks import CaptureResultPolicy
from camctl.capture.segment_models import (
    CoverageMetric,
    CoverageStatus,
    DefaultScopeState,
    FileCompletionState,
    OwnershipState,
    SegmentDecision,
    SegmentDecisionScope,
    SegmentDecisionStatus,
    SegmentMember,
    SegmentMemberRole,
    SegmentSourceKind,
    SegmentSetRef,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.devices.segment_profiles import SegmentFileKind, profile_for_policy
from camctl.devices.drivers.adb_cameras.action6_formats import format_for_suffix
from camctl.persistence.repositories.segments import SegmentRepository, _eligible_original
from camctl.persistence.transaction import row_facts


@dataclass(frozen=True, slots=True)
class Action6CollectionFacts:
    """只保存完整来源分类所需的有限摘要，不持有文件数组。"""

    decision: SegmentDecision
    assessment: CaptureAssessment
    video_count: int
    video_order_known: bool
    video_gap: bool


def action6_decision_failure(activity_id: int, decision: SegmentDecision):
    """把有限 Action6 决策映射成共同终态事务可核实的错误。"""
    if decision.status in (
        SegmentDecisionStatus.SUCCEEDED,
        SegmentDecisionStatus.CANCELED,
    ):
        return None
    if decision.failure_code == "capture_failed":
        details = {
            "activity_id": str(activity_id),
            "reason": decision.reason
            if decision.reason in ("no_outputs", "invalid_outputs")
            else "invalid_outputs",
        }
    else:
        details = {
            "activity_id": str(activity_id),
            "reason": "completion_unknown"
            if decision.reason == "completion_unknown"
            else "outputs_unknown",
        }
    return RecordingFailure(decision.failure_code, details)


def _action_type(action) -> str:
    return enum_for("actions.type")(action["type"]).name.lower()


def classify_action6_members(
    action,
    ref: SegmentSetRef,
    members: Iterable[SegmentMember],
    *,
    complete: bool,
    coverage_status: CoverageStatus = CoverageStatus.NOT_PERFORMED,
    confirmed: Decimal | int | None = None,
    checked_count: int = 0,
    canceled: bool = False,
    default_scope: SegmentDecisionScope | None = None,
) -> Action6CollectionFacts:
    """从冻结成员重算照片要求与全体合格主视频，结果不读取设备或媒体。"""
    spec = action["execution_spec_json"]
    policy = spec.get("result_policy")
    profile = profile_for_policy(policy)
    target = profile.coverage_target(
        action.get("effective_params_json") or {}, spec, _action_type(action)
    )
    if target is None:
        raise ConsistencyError("Action6 分段策略缺少冻结覆盖目标")
    native_cancel_all_qualified = (
        canceled
        and policy == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
        and _action_type(action) == "camera_timelapse"
    )
    rules = tuple(ProductRule.from_json(item) for item in spec.get("product_rules", ()))
    scope_facts = {
        "count": 0,
        "first": None,
        "last": None,
        "cursor_missing": False,
        "prefix_count": 0,
        "prefix_first": None,
        "prefix_last": None,
        "ordered": True,
        "prefix_open": True,
        "last_key": None,
        "gap": False,
    }

    def products():
        for member in members:
            parsed = profile.classify_file(member.identity)
            is_video = (
                parsed.kind is SegmentFileKind.VIDEO
                and member.role is SegmentMemberRole.ORIGINAL
            )
            is_photo = (
                parsed.kind is SegmentFileKind.PHOTO
                and member.role is SegmentMemberRole.OTHER
            )
            if is_video and _eligible_original(member):
                scope_facts["count"] += 1
                if member.cursor is None:
                    scope_facts["ordered"] = False
                    scope_facts["cursor_missing"] = True
                else:
                    previous_key = scope_facts["last_key"]
                    if previous_key is not None:
                        if member.key <= previous_key:
                            scope_facts["ordered"] = False
                        else:
                            try:
                                if previous_key.successor() != member.key:
                                    scope_facts["gap"] = True
                                    scope_facts["prefix_open"] = False
                            except ValueError:
                                scope_facts["gap"] = True
                                scope_facts["prefix_open"] = False
                    scope_facts["last_key"] = member.key
                    scope_facts["first"] = (
                        member.cursor
                        if scope_facts["first"] is None
                        else min(scope_facts["first"], member.cursor)
                    )
                    scope_facts["last"] = (
                        member.cursor
                        if scope_facts["last"] is None
                        else max(scope_facts["last"], member.cursor)
                    )
                    if scope_facts["prefix_open"]:
                        scope_facts["prefix_count"] += 1
                        if scope_facts["prefix_first"] is None:
                            scope_facts["prefix_first"] = member.cursor
                        scope_facts["prefix_last"] = member.cursor
            if not (is_video or is_photo):
                continue
            suffix = "." + member.identity.rpartition(".")[2]
            file_format = format_for_suffix(suffix)
            yield CaptureFile(
                file_id=member.identity,
                kind=FileKind.VIDEO if is_video else FileKind.PHOTO,
                complete=member.completion is FileCompletionState.COMPLETE,
                ownership_confirmed=member.ownership is OwnershipState.OWNED,
                format_id=None if file_format is None else file_format.format_id,
                size_bytes=member.size_bytes,
            )

    assessment = assess_capture_files(
        CaptureFileSet(files=products(), set_finalized=complete),
        ProductRequirements(frozenset({FileKind.VIDEO}), rules),
    )
    if default_scope is None:
        if scope_facts["count"] == 0:
            scope = SegmentDecisionScope(
                DefaultScopeState.EMPTY, ref, None, None, 0
            )
        elif native_cancel_all_qualified and not scope_facts["cursor_missing"]:
            scope = SegmentDecisionScope(
                DefaultScopeState.NONEMPTY,
                ref,
                scope_facts["first"],
                scope_facts["last"],
                scope_facts["count"],
            )
        elif not scope_facts["ordered"]:
            scope = SegmentDecisionScope(
                DefaultScopeState.UNKNOWN, ref, None, None, None
            )
        else:
            scope_count = (
                scope_facts["prefix_count"]
                if scope_facts["gap"]
                else scope_facts["count"]
            )
            scope_first = (
                scope_facts["prefix_first"]
                if scope_facts["gap"]
                else scope_facts["first"]
            )
            scope_last = (
                scope_facts["prefix_last"]
                if scope_facts["gap"]
                else scope_facts["last"]
            )
            if scope_count == 0:
                scope = SegmentDecisionScope(
                    DefaultScopeState.EMPTY, ref, None, None, 0
                )
            else:
                scope = SegmentDecisionScope(
                    DefaultScopeState.NONEMPTY,
                    ref,
                    scope_first,
                    scope_last,
                    scope_count,
                )
    else:
        scope = default_scope

    reason = None
    failure_code = None
    if canceled:
        status = SegmentDecisionStatus.CANCELED
    elif not complete or not assessment.is_complete and not assessment.explicitly_unmet:
        status = SegmentDecisionStatus.UNCONFIRMED
        reason = "outputs_unknown"
        failure_code = "capture_result_unconfirmed"
    elif assessment.explicitly_unmet:
        status = SegmentDecisionStatus.FAILED
        reason = "no_outputs" if assessment.file_count == 0 else "invalid_outputs"
        failure_code = "capture_failed"
    elif scope_facts["gap"]:
        status = SegmentDecisionStatus.FAILED
        reason = "segment_gap"
        failure_code = "capture_failed"
    elif scope.state is DefaultScopeState.UNKNOWN:
        status = SegmentDecisionStatus.UNCONFIRMED
        reason = "video_order_unknown"
        failure_code = "capture_result_unconfirmed"
    else:
        status = SegmentDecisionStatus.SUCCEEDED

    decision = SegmentDecision(
        set_ref=ref,
        status=status,
        metric=CoverageMetric(target.metric),
        target=target.value,
        confirmed=confirmed,
        coverage_status=coverage_status,
        checked_count=checked_count,
        default_scope=scope,
        failure_code=failure_code,
        reason=reason,
    )
    return Action6CollectionFacts(
        decision=decision,
        assessment=assessment,
        video_count=scope_facts["count"],
        video_order_known=scope_facts["ordered"],
        video_gap=scope_facts["gap"],
    )


async def iter_action6_video_members(repository, ref, database):
    """按冻结顺序逐页返回全部可用于 Action6 覆盖的主视频。"""
    profile = profile_for_policy(CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value)
    cursor = None
    while True:
        page = await database.read(lambda owned: repository.read_members(ref, cursor, 128, owned))
        for stored in page.items:
            member = stored.member
            if (
                profile.classify_file(member.identity).kind is SegmentFileKind.VIDEO
                and _eligible_original(member)
                and member.cursor is not None
            ):
                yield stored
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


async def prepare_action6_segment_set(runtime, action, listing, *, canceled=False):
    """可靠地保存原完整结果集合和成员；不启动复制或媒体检查。"""
    from camctl.capture import handlers as common
    from camctl.capture.segment_completion import (
        _prepare_members,
        _save,
        register_segment_observations,
        resume_segment_writes,
    )

    action_id = action["id"]
    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
    if ref is None:
        if listing is None:
            listing = await common._listing_round(runtime, action_id)
        if listing.phase in (common.ListingPhase.IN_FLIGHT, common.ListingPhase.RETRY_WAIT):
            return None
        exhausted = listing.phase is common.ListingPhase.EXHAUSTED
        if exhausted:
            listing = await common._saved_result_listing(runtime, action_id)
        await register_segment_observations(runtime, action_id, listing)
        finalized = common._listing_finalized(listing)
        if not finalized and not (exhausted or canceled):
            await common._finish_listing_result(runtime, listing, retry_wait=True)
            return None
        if finalized:
            await common._finish_listing_result(runtime, listing, finalize_output_set=True)
        request = await runtime.database.read(lambda owned, occurred_at=runtime.wall_us(): repository.prepare_result_set(
            action_id, occurred_at, owned
        ))
        if canceled:
            from dataclasses import replace
            from camctl.capture.segment_models import PublicationKind

            cancel = await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
                "WHERE json_extract(r.value,'$.table')='actions' AND json_extract(r.value,'$.id')=? "
                "AND json_extract(r.value,'$.before.values.cancel_requested')=0 "
                "AND json_extract(r.value,'$.after.values.cancel_requested')=1 "
                "ORDER BY e.id DESC LIMIT 1",
                (action_id,),
            ).fetchone())
            if cancel is None:
                raise ConsistencyError("原生延时取消缺少实际取消请求事件")
            previous = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
            predecessor = None if previous is None else previous.set_id
            existing = await runtime.database.read(lambda owned: repository.find_canceled_result_ref(
                action_id, predecessor, cancel[0], owned
            ))
            if existing is not None:
                ref = existing
                await _prepare_members(runtime, repository, ref)
                return await runtime.database.read(lambda owned: repository.read_current_segment_ref(ref, owned))
            if previous is not None:
                previous_row = await runtime.database.read(lambda owned: repository.read_result_set(previous, owned))
                from camctl.persistence.repositories.result_pages import _at

                request = await runtime.database.read(lambda owned: replace(
                    request,
                    source_boundary=HistoryBoundary(
                        previous_row["source_txn_id"], previous_row["source_event_id"]
                    ),
                    source_kind=SegmentSourceKind[
                        enum_for("segment_sets.source_kind")(previous_row["source_kind"]).name
                    ],
                    current_scan=(
                        None
                        if previous_row["current_attempt_id"] is None
                        else _at(owned.connection, previous_row["current_final_page_event_id"])[0].ref
                    ),
                ))
            request = replace(
                request,
                publication_kind=PublicationKind.CANCELED,
                predecessor_set_id=predecessor,
                cancel_event_id=cancel[0],
            )
        ref = await _save(
            runtime,
            action_id,
            repository,
            "create_result_set",
            request,
        )
    await _prepare_members(runtime, repository, ref)
    scan_ref = await runtime.database.read(lambda owned: repository.read_current_segment_ref(ref, owned))
    return scan_ref if scan_ref is not None else ref


async def finish_action6_retained_unconfirmed(runtime, action, ref):
    """预算耗尽且没有完整当前扫描时，保留原已登记文件并明确报告未知。"""
    from camctl.capture import handlers as common
    from camctl.capture.media import RecordingFailure
    from camctl.capture.segment_completion import _save, _prepare_members, resume_segment_writes
    from camctl.capture.segment_storage import ResultCheckConclusionSave, SegmentResultRef
    from camctl.capture.segment_models import ResultCheckEndReason
    from camctl.persistence.repositories.segment_current import current_decision

    if not isinstance(ref, SegmentResultRef):
        raise TypeError("Action6 未完成扫描必须沿保留来源引用收场")
    action_id = action["id"]
    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    await _prepare_members(runtime, repository, ref)
    row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if row["state"] == int(enum_for("segment_sets.state").MEMBERS_FIXED):
        existing = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (ref.set_id,)
        ).fetchone())
        if existing is None:
            reason, _, _ = await runtime.database.read(lambda owned: current_decision(owned.connection, row))
            anchor = await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT result_event_id FROM operation_attempts WHERE run_id=? "
                "ORDER BY attempt_no DESC LIMIT 1", (row["run_id"],)
            ).fetchone())
            if anchor is None or anchor[0] is None:
                raise ConsistencyError("Action6 耗尽来源缺少实际最后结果事件")
            await _save(
                runtime, action_id, repository, "conclude_result_check",
                ResultCheckConclusionSave(
                    ref, reason, anchor[0], None, runtime.check_config, None, None,
                    runtime.wall_us(),
                ),
            )
        await _save(runtime, action_id, repository, "decide_result", ref, runtime.wall_us())
        row = await runtime.database.read(lambda owned: repository.read_result_set(ref, owned))
    if row["state"] != int(enum_for("segment_sets.state").ASSESSED):
        raise ConsistencyError("Action6 保留来源缺少可靠的有限未知结论")
    from camctl.persistence.repositories.segment_check_conclusions import read_fixed_result_check_conclusion

    conclusion = await runtime.database.read(lambda owned: read_fixed_result_check_conclusion(owned.connection, row))
    actual_failure = conclusion["current_failure_json"]
    failure = (None if actual_failure is None else RecordingFailure(**actual_failure)) or RecordingFailure(
        "capture_result_unconfirmed",
        {"activity_id": str(row["activity_id"]), "reason": "outputs_unknown"},
    )
    await _save(
        runtime, action_id, runtime.capture, "finish_segment_recording",
        FinishSegmentRecording(
            ref, row["activity_id"], row["run_id"], runtime.wall_us(), failure,
        ),
    )


async def classify_action6_segment_set(
    action,
    ref: SegmentSetRef,
    repository,
    database,
    *,
    coverage_status=CoverageStatus.NOT_PERFORMED,
    confirmed=None,
    checked_count=0,
    canceled=False,
    default_scope=None,
):
    from camctl.persistence.repositories.segment_listing import materialized_segment_members

    setrow = await database.read(lambda owned: repository.read_set(ref, owned))
    complete = setrow["source_kind"] == int(
        enum_for("segment_sets.source_kind").COMPLETE_SCAN
    ) and setrow["publication_kind"] == int(
        enum_for("segment_sets.publication_kind").RESULT
    )
    if canceled:
        complete = True
    async with materialized_segment_members(database, repository, ref) as members:
        return classify_action6_members(
            action, ref, (stored.member for stored in members()),
            complete=complete, coverage_status=coverage_status,
            confirmed=confirmed, checked_count=checked_count, canceled=canceled,
            default_scope=default_scope,
        )


async def _conclude_action6_results(runtime, action, ref):
    """按同一原 RESULTS 票保存既有有限检查责任结论。"""
    from camctl.capture.segment_completion import _save
    from camctl.capture.segment_models import ResultCheckEndReason
    from camctl.capture.segment_storage import (
        ResultCheckConclusionSave,
        SegmentResultRef,
    )

    repository = SegmentRepository()
    canceled = bool(action["cancel_requested"])
    if canceled:
        setrow = await runtime.database.read(lambda owned: repository.read_set(ref, owned))
        result_ref = await runtime.database.read(lambda owned: repository.find_canceled_result_ref(
            action["id"], setrow["predecessor_set_id"],
            setrow["cancel_event_id"], owned,
        ))
    else:
        result_ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action["id"], owned))
    if not isinstance(result_ref, SegmentResultRef):
        raise ConsistencyError("Action6 共同完成缺少已准备的原 RESULTS 集合")
    if (await runtime.database.read(lambda owned: repository.read_current_segment_ref(result_ref, owned))) != ref:
        raise ConsistencyError("Action6 结论与当前原 RESULTS 完整集合不一致")
    if (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT 1 FROM segment_check_conclusions WHERE set_id=?", (result_ref.set_id,)
    ).fetchone())):
        return
    setrow = await runtime.database.read(lambda owned: repository.read_result_set(result_ref, owned))
    run = (
        None
        if setrow["run_id"] is None
        else (await runtime.database.read(lambda owned: row_facts(owned.connection, "operation_runs", setrow["run_id"])))
    )
    if run is None and not canceled:
        raise ConsistencyError("Action6 原 RESULTS 集合缺少核实责任")
    action_type = _action_type(action)
    if canceled:
        reason = ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION
        anchor = setrow["cancel_event_id"]
        if anchor is None:
            raise ConsistencyError("Action6 取消结果缺少实际取消事件")
        prior_conclusion = (
            None
            if setrow["predecessor_set_id"] is None
            else (await runtime.database.read(lambda owned: owned.connection.execute(
                "SELECT id FROM segment_check_conclusions WHERE set_id=?",
                (setrow["predecessor_set_id"],),
            ).fetchone()))
        )
        prior_conclusion = None if prior_conclusion is None else prior_conclusion[0]
    else:
        reason = (
            ResultCheckEndReason.NATIVE_RESULT_DECIDED
            if action_type == "camera_timelapse"
            else ResultCheckEndReason.SEGMENTS_SUFFICIENT
        )
        (anchor,) = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? "
            "ORDER BY attempt_no DESC LIMIT 1",
            (run["id"],),
        ).fetchone())
        if anchor is None:
            raise ConsistencyError("Action6 原 RESULTS 责任缺少实际结束事件")
        prior_conclusion = None
    prior_end = None
    check_config = runtime.check_config if run is not None else None
    if run is not None and run["status"] not in (1, 2):
        prior_end = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
            "WHERE json_extract(r.value,'$.table')='operation_runs' "
            "AND json_extract(r.value,'$.id')=? "
            "AND json_extract(r.value,'$.before.values.status') IN (1,2) "
            "AND json_extract(r.value,'$.after.values.status') IN (3,4,5,6,7) "
            "ORDER BY e.id DESC LIMIT 1",
            (run["id"],),
        ).fetchone())
        if prior_end is None:
            raise ConsistencyError("Action6 原 RESULTS 结束缺少对应关闭事件")
        prior_end = prior_end[0]
        check_config = None
    await _save(
        runtime,
        action["id"],
        repository,
        "conclude_result_check",
        ResultCheckConclusionSave(
            result_ref,
            reason,
            anchor,
            prior_end,
            check_config,
            None,
            prior_conclusion,
            runtime.wall_us(),
        ),
    )


async def publish_action6_segment_result(runtime, action, ref, decision, *, failure=None):
    """把决策、其有序默认范围、正式产物和动作终态放进共同事务。"""
    from camctl.capture.segment_completion import _save, resume_segment_writes

    action_id = action["id"]
    await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id)
    repository = SegmentRepository()
    if not isinstance(decision, SegmentDecision) or decision.set_ref != ref:
        raise TypeError("Action6 发布必须引用相同的有限 SegmentDecision")
    setrow = await runtime.database.read(lambda owned: repository.read_set(ref, owned))
    # 取消结论要求原成员仍处于 MEMBERS_FIXED；先结论原 RESULTS
    # 责任，再将覆盖决策推进为 ASSESSED，避免状态顺序反转后无法收场。
    # 正常收场必须在保存 SegmentDecision 后结论，保证关闭事实与该决策一致。
    if action["cancel_requested"]:
        await _conclude_action6_results(runtime, action, ref)
    if setrow["state"] != int(enum_for("segment_sets.state").ASSESSED):
        await _save(
            runtime,
            action_id,
            repository,
            "save_decision",
            ref,
            decision,
            runtime.wall_us(),
        )
    else:
        persisted = await runtime.database.read(lambda owned: repository.read_decision(ref, owned))
        if persisted != decision:
            raise ConsistencyError("原集合已有不同 Action6 决策")
    if decision.default_scope.state is DefaultScopeState.NONEMPTY:
        row = await runtime.database.read(lambda owned: repository.read_set(ref, owned))
        saved = (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?",
            (setrow["id"],),
        ).fetchone()))[0]
        selected = 0
        batch = []
        profile = profile_for_policy(action["execution_spec_json"]["result_policy"])
        cursor = None
        while True:
            page = await runtime.database.read(lambda owned: repository.read_members(ref, cursor, 128, owned))
            for stored in page.items:
                member = stored.member
                if (
                    profile.classify_file(member.identity).kind is not SegmentFileKind.VIDEO
                    or not _eligible_original(member)
                    or member.cursor is None
                    or not decision.default_scope.first_cursor
                    <= member.cursor
                    <= decision.default_scope.last_cursor
                ):
                    continue
                if selected >= saved:
                    batch.append(stored.member_id)
                    if len(batch) == 128:
                        await _save(runtime, action_id, repository, "append_scope", ref, tuple(batch), runtime.wall_us())
                        batch.clear()
                selected += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        if batch:
            await _save(runtime, action_id, repository, "append_scope", ref, tuple(batch), runtime.wall_us())
        if selected != decision.default_scope.member_count:
            raise ConsistencyError("Action6 默认视频范围与冻结成员不同")
    elif decision.default_scope.state is DefaultScopeState.EMPTY:
        if (await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM segment_scope_members WHERE set_id=? LIMIT 1", (setrow["id"],)
        ).fetchone())):
            raise ConsistencyError("空 Action6 视频范围不能已有成员")

    setrow = await runtime.database.read(lambda owned: repository.read_set(ref, owned))
    if not action["cancel_requested"]:
        await _conclude_action6_results(runtime, action, ref)
    if failure is None and decision.status in (
        SegmentDecisionStatus.FAILED,
        SegmentDecisionStatus.UNCONFIRMED,
    ):
        from camctl.contracts.workflow_errors import registered_error

        code = decision.failure_code or (
            "capture_failed"
            if decision.status is SegmentDecisionStatus.FAILED
            else "capture_result_unconfirmed"
        )
        if code == "capture_failed":
            details = {
                "activity_id": str(setrow["activity_id"]),
                "reason": decision.reason
                if decision.reason in ("no_outputs", "invalid_outputs")
                else "invalid_outputs",
            }
        else:
            details = {
                "activity_id": str(setrow["activity_id"]),
                "reason": "completion_unknown"
                if decision.reason == "completion_unknown"
                else "outputs_unknown",
            }
        failure = RecordingFailure(code, details)
    if decision.status in (SegmentDecisionStatus.SUCCEEDED, SegmentDecisionStatus.CANCELED):
        failure = None
    await _save(
        runtime,
        action_id,
        runtime.capture,
        "finish_segment_recording",
        FinishSegmentRecording(
            ref,
            setrow["activity_id"],
            setrow["run_id"],
            runtime.wall_us(),
            failure,
        ),
    )


async def advance_action6_segmented_recording(runtime, action, decision):
    """普通录像与原生有限延时共用的完整结果推进。"""
    from camctl.capture import handlers as common
    from camctl.capture.segment_completion import release_segment_occupancy
    from camctl.capture.recording import RecordingPhase
    from camctl.capture.segment_completion import _control_facts
    from camctl.capture.segment_models import EvidenceState

    action_id = action["id"]
    if action["cancel_requested"]:
        if action["type"] == int(enum_for("actions.type").CAMERA_TIMELAPSE):
            ref = await prepare_action6_segment_set(
                runtime,
                action,
                (await common._saved_result_listing(runtime, action_id)),
                canceled=True,
            )
            if ref is None:
                return
            repository = SegmentRepository()
            facts = await classify_action6_segment_set(
                action, ref, repository, runtime.database, canceled=True
            )
            await release_segment_occupancy(runtime, action_id)
            await publish_action6_segment_result(runtime, action, ref, facts.decision)
        else:
            await common._finish_canceled_capture(runtime, action_id)
        return

    listing = await common._local_recording_listing(runtime, action)
    ref = await prepare_action6_segment_set(runtime, action, listing)
    if ref is None:
        return
    from camctl.capture.segment_storage import SegmentResultRef
    if isinstance(ref, SegmentResultRef):
        await finish_action6_retained_unconfirmed(runtime, action, ref)
        return
    repository = SegmentRepository()
    facts = await classify_action6_segment_set(action, ref, repository, runtime.database)
    await release_segment_occupancy(runtime, action_id)
    activity_id = await common._activity_id_of(runtime, action_id)
    activity = await runtime.database.read(lambda owned: row_facts(
        owned.connection, "device_activities", activity_id,
    ))
    _, overrun = _control_facts(runtime, action, activity)
    # RESULTS 可能刚刚保存了本轮 STOP+filewait 证据。入口传入的 decision
    # 是读取之前的快照，需依据此刻持久化事实重算，不能因此把正常完成送去媒体检查。
    from camctl.capture.handlers import _recording_port
    from camctl.capture.recording import RecordingFacts, decide_recording_next

    current_decision = decide_recording_next(
        (await _recording_port(runtime).recording_state(action_id)), RecordingFacts()
    )
    control_complete = (
        current_decision.phase is RecordingPhase.CONTROL_COMPLETE
        and overrun is not EvidenceState.CONFIRMED
    )
    if not control_complete:
        from dataclasses import replace

        reason = (
            "control_overrun"
            if overrun is EvidenceState.CONFIRMED
            else "completion_unknown"
        )
        facts = replace(
            facts,
            decision=replace(
                facts.decision,
                status=SegmentDecisionStatus.UNCONFIRMED,
                failure_code="capture_result_unconfirmed",
                reason=reason,
            ),
        )
    if control_complete and facts.decision.status is SegmentDecisionStatus.SUCCEEDED:
        await publish_action6_segment_result(runtime, action, ref, facts.decision)
        return
    from camctl.capture.action6_recovery import advance_action6_recovery

    await advance_action6_recovery(runtime, action, facts.decision)


async def advance_action6_native_result(runtime, action_id, *, canceled=False):
    action = await runtime.action(action_id)
    if action["cancel_requested"]:
        canceled = True
    if canceled:
        from camctl.capture.action6_recovery import finalize_pending_action6_inspections

        if not (await finalize_pending_action6_inspections(
            runtime, action_id, reason="source_canceled"
        )):
            return
    from camctl.capture import handlers as common
    from camctl.capture.segment_completion import _save, release_segment_occupancy

    ref = await prepare_action6_segment_set(runtime, action, None, canceled=canceled)
    if ref is None:
        return
    from camctl.capture.segment_storage import SegmentResultRef
    if isinstance(ref, SegmentResultRef):
        if canceled:
            from camctl.capture.segment_completion import finish_segmented_cancellation

            await finish_segmented_cancellation(runtime, action_id)
        else:
            await finish_action6_retained_unconfirmed(runtime, action, ref)
        return
    repository = SegmentRepository()
    facts = await classify_action6_segment_set(
        action, ref, repository, runtime.database, canceled=canceled
    )
    activity_id = await common._activity_id_of(runtime, action_id)
    activity = await runtime.database.read(lambda owned: row_facts(
        owned.connection, "device_activities", activity_id,
    ))
    # 自然等待事件在本轮原始 RESULTS 完成后仍须按冻结合同验证；完整
    # 集合和分类结果由上方的 ref/facts 提供，不从单一活动状态字段推测。
    normal_completion = (await common._natural_file_completion_context(
        runtime, action_id
    )) is not None
    if (
        not canceled
        and normal_completion
        and facts.decision.status is SegmentDecisionStatus.SUCCEEDED
        and activity["result_set_state"] not in (3, 4)
    ):
        from camctl.capture.result_scans import SavedResultEntries
        from camctl.capture.segment_storage import SegmentResultRef

        result_ref = await runtime.database.read(lambda owned: repository.read_prepared_result_ref(action_id, owned))
        if not isinstance(result_ref, SegmentResultRef):
            raise ConsistencyError("Action6 自然完成缺少已准备的原 RESULTS 集合")
        listing = await common._saved_result_listing(runtime, action_id)
        if not isinstance(listing.entries, SavedResultEntries):
            raise ConsistencyError("Action6 自然完成缺少可靠的原 RESULTS 页")
        first = (await anext(listing.entries.pages())).ref
        result_set = await runtime.database.read(lambda owned: repository.read_result_set(result_ref, owned))
        observation = {
            "first_page_event_id": first.event_id,
            "last_page_event_id": listing.entries.last_ref.event_id,
            "file_count": result_set["member_count"],
        }
        completion = await runtime.database.read(lambda owned: runtime.capture.read_result_completion(
            action_id, owned
        ))
        if completion is None:
            completion = {
                "method": common._TIME_AND_OUTPUTS_METHOD,
                "wait_completed_event_id": activity["wait_completed_event_id"],
                "observation": observation,
            }
        await _save(
            runtime,
            action_id,
            runtime.capture,
            "confirm_result_set",
            common.ResultSetSave(
                action_id,
                runtime.wall_us(),
                common.ResultSetPhase.COMPLETE,
                "result_files_listed/v2",
                observation,
                {"status": "completed"},
                completion,
            ),
        )
    await release_segment_occupancy(runtime, action_id)
    if canceled or (
        normal_completion
        and facts.decision.status is SegmentDecisionStatus.SUCCEEDED
    ):
        await publish_action6_segment_result(runtime, action, ref, facts.decision)
        return
    from dataclasses import replace

    facts = replace(
        facts,
        decision=replace(
            facts.decision,
            status=SegmentDecisionStatus.UNCONFIRMED,
            failure_code="capture_result_unconfirmed",
            reason="completion_unknown",
        ),
    )
    from camctl.capture.action6_recovery import advance_action6_recovery

    await advance_action6_recovery(runtime, action, facts.decision)
