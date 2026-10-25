"""无可靠当前扫描时，只发布保留文件及原有限核实的未知结论。"""

from dataclasses import dataclass
from types import SimpleNamespace

from camctl.capture.media import RecordingFailure
from camctl.capture.segment_models import DefaultScopeState, SegmentAssessmentStatus
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.persistence.transaction import row_facts


@dataclass(frozen=True)
class RetainedResultDisposition:
    status: SegmentAssessmentStatus = SegmentAssessmentStatus.UNCONFIRMED
    gap_count: None = None

    @property
    def default_scope(self):
        return SimpleNamespace(state=DefaultScopeState.UNKNOWN, member_count=None)


def verify_retained_disposition_sources(ref, db):
    """核实原 H0、完整成员及原实际预算关闭，不用旧完整扫描补当前。"""
    if not isinstance(ref, SegmentResultRef):
        raise TypeError("跨轮保留处置必须引用实际集合")
    from .segment_result_sets import read_result_set
    from .segment_result_members import _fixed_count
    from .segment_check_conclusions import read_verified_conclusion

    reader = SimpleNamespace(connection=db)
    setrow = read_result_set(ref, reader)
    canceled = setrow["publication_kind"] == int(
        enum_for("segment_sets.publication_kind").CANCELED
    )
    conclusion = read_verified_conclusion(db, setrow)
    external = conclusion["reason"] in tuple(
        int(enum_for("segment_check_conclusions.reason")[name])
        for name in ("BINDING_UNAVAILABLE", "STOP_UNCONFIRMED_CLOSED")
    )
    native = conclusion["reason"] in tuple(
        int(enum_for("segment_check_conclusions.reason")[name])
        for name in ("NATIVE_RESULT_DECIDED", "NATIVE_FINITE_CANCEL_END")
    )
    if (
        not canceled
        and setrow["publication_kind"]
        != int(enum_for("segment_sets.publication_kind").RESULT)
        or not (canceled or external or native)
        and setrow["source_kind"]
        not in (
            int(enum_for("segment_sets.source_kind").RETAINED_SOURCES),
            int(enum_for("segment_sets.source_kind").NO_RESULTS),
        )
        or setrow["state"]
        not in (
            int(enum_for("segment_sets.state").MEMBERS_FIXED),
            int(enum_for("segment_sets.state").ASSESSED),
        )
        or setrow["facts_json"] is not None
        or setrow["assessment_json"] is not None
    ):
        raise ConsistencyError("跨轮保留发布不能声明可靠当前扫描或普通完整判断")
    if _fixed_count(db, setrow) != setrow["member_count"]:
        raise ConsistencyError("保留发布遗漏原 H0 的实际成员")
    action = row_facts(db, "actions", ref.action_id)
    from camctl.devices.tasks import CaptureResultPolicy
    action6 = (action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value)
    from .segment_current import current_gaps
    from .segments import _stored_gaps
    from itertools import zip_longest

    expected_gaps = () if canceled or external or native or action6 else current_gaps(db, setrow)
    for actual, saved in zip_longest(expected_gaps, _stored_gaps(ref, reader)):
        if actual is None or saved is None or actual != saved:
            raise ConsistencyError("跨轮保留结果遗漏或改变当前明确缺口")
    if native:
        from .segment_native import native_classification

        return native_classification(db, setrow)
    return RetainedResultDisposition()


def read_saved_result_disposition(ref, db):
    """消费固定集合和已保存有限结论，只检查本次直接身份及适用分区。"""
    if not isinstance(ref, SegmentResultRef):
        raise TypeError("保存结果处置必须引用实际集合")
    from .segments import read_segment_identity
    from .segment_check_conclusions import read_fixed_result_check_conclusion
    identity = read_segment_identity(db, ref, fixed=True)
    setrow = identity.setrow
    return saved_result_disposition(setrow, read_fixed_result_check_conclusion(db, setrow))


def saved_result_disposition(setrow, conclusion):
    """对显式固定行执行本次处置判断，不取得来源或重新分类。"""
    canceled = setrow["publication_kind"] == int(enum_for("segment_sets.publication_kind").CANCELED)
    reasons = enum_for("segment_check_conclusions.reason")
    external = conclusion["reason"] in (int(reasons.BINDING_UNAVAILABLE), int(reasons.STOP_UNCONFIRMED_CLOSED))
    native = conclusion["reason"] in (int(reasons.NATIVE_RESULT_DECIDED), int(reasons.NATIVE_FINITE_CANCEL_END))
    if ((not canceled and setrow["publication_kind"] != int(enum_for("segment_sets.publication_kind").RESULT))
            or (not (canceled or external or native) and setrow["source_kind"] not in (
                int(enum_for("segment_sets.source_kind").RETAINED_SOURCES), int(enum_for("segment_sets.source_kind").NO_RESULTS)))
            or setrow["state"] not in (int(enum_for("segment_sets.state").MEMBERS_FIXED), int(enum_for("segment_sets.state").ASSESSED))
            or setrow["facts_json"] is not None or setrow["assessment_json"] is not None):
        raise ConsistencyError("保存结果的决定与固定集合适用分区不同")
    if native:
        from .segment_native import native_classification_from_saved_conclusion
        return native_classification_from_saved_conclusion(conclusion)
    return RetainedResultDisposition()


def retained_failure(request, setrow, db):
    from .segment_check_conclusions import read_fixed_result_check_conclusion
    conclusion = read_fixed_result_check_conclusion(db, setrow)
    saved_result_disposition(setrow, conclusion)
    if setrow["publication_kind"] == int(
        enum_for("segment_sets.publication_kind").CANCELED
    ):
        if request.failure is not None or request.failure_event_id is not None:
            raise ConsistencyError("普通录像取消不携带产物或执行失败")
        return 6, None, None
    raw = conclusion["current_failure_json"]
    current_failure = None if raw is None else RecordingFailure(**raw)
    reasons = enum_for("segment_check_conclusions.reason")
    if conclusion["reason"] == int(reasons.NATIVE_FINITE_CANCEL_END):
        if request.failure is not None or request.failure_event_id is not None:
            raise ConsistencyError("原生取消保留文件，不改写为执行失败")
        return 6, None, None
    if conclusion["reason"] == int(reasons.NATIVE_RESULT_DECIDED) and conclusion[
        "current_result"
    ] == int(enum_for("segment_check_conclusions.current_result").SUFFICIENT):
        if request.failure is not None or request.failure_event_id is not None:
            raise ConsistencyError("原生充分结果不能携带失败")
        return 3, None, None
    if conclusion["reason"] == int(
        enum_for("segment_check_conclusions.reason").NO_RESULT_WORK
    ):
        if raw is None:
            current_failure = RecordingFailure(
                "capture_failed",
                {
                    "activity_id": str(setrow["activity_id"]),
                    "reason": "baseline_read_failed",
                },
            )
    expected = current_failure or RecordingFailure(
        "capture_result_unconfirmed",
        {"activity_id": str(setrow["activity_id"]), "reason": "outputs_unknown"},
    )
    if (
        request.failure is None
        or request.failure.code != expected.code
        or not json_equal(request.failure.details, expected.details)
        or request.failure_event_id is not None
    ):
        raise ConsistencyError("保留结果的实际失败必须保持原当前扫描未知")
    from camctl.contracts.workflow_errors import registered_error

    return 4, registered_error(expected.code)["action_error_id"], expected.details


def verify_disposition_event(db, ref, event):
    from camctl.capture.segment_storage import document
    from camctl.history.events import load_event_registry
    from camctl.persistence.transaction import update_change

    read_saved_result_disposition(ref, db)
    expected = update_change("segment_sets", ref.set_id, {"state": 2}, {"state": 3})
    if (
        event.reason
        != load_event_registry()["events"]["SEGMENT_FACTS"]["branches"][
            "DECIDE_RESULT"
        ]["reason"]
        or event.rows != (expected,)
        or not json_equal(
            event.evidence, {"segment_request": {"ref": document(ref), "input": None}}
        )
    ):
        raise ConsistencyError("结果处置必须保持原有限决定及完整固定成员")


class DecideResultCommand:
    def __init__(self, ref, occurred_at, key):
        from camctl.contracts.values import UtcMicros, OperationKey

        if not isinstance(ref, SegmentResultRef):
            raise TypeError("结果处置需要实际集合引用")
        UtcMicros(occurred_at)
        self.ref, self.at, self.key = ref, occurred_at, OperationKey(key)

    def plan(self, scope):
        from camctl.capture.segment_storage import document
        from camctl.history.events import load_event_registry
        from camctl.history.reads import SegmentReadScope
        from camctl.persistence.transaction import (
            CommandPlan,
            event_envelope,
            saved_transaction_events,
            update_change,
        )

        db, ref = scope.connection, self.ref
        read_saved_result_disposition(ref, db)
        registry = load_event_registry()["events"]["SEGMENT_FACTS"]
        reason = registry["branches"]["DECIDE_RESULT"]["reason"]
        evidence = {"segment_request": {"ref": document(ref), "input": None}}
        saved = saved_transaction_events(db, self.key)
        if saved is not None:
            if (
                len(saved) != 1
                or saved[0]["type"] != registry["id"]
                or saved[0]["reason"] != reason
                or saved[0]["occurred_at"] != self.at
                or not json_equal(saved[0]["body"]["evidence"], evidence)
                or row_facts(db, "segment_sets", ref.set_id)["state"] != 3
            ):
                raise ConsistencyError("原键结果处置与完整原申请不同")
            return CommandPlan((), {}, {}, ref, True)
        allocation = scope.allocate(1)
        row = update_change("segment_sets", ref.set_id, {"state": 2}, {"state": 3})
        event = event_envelope(
            allocation.first_event_id,
            allocation.txn_id,
            registry["id"],
            reason,
            (row,),
            self.at,
            evidence,
        )
        return CommandPlan(
            (event,),
            {("segment_sets", ref.set_id): ("action", ref.action_id)},
            {
                "actions": {ref.action_id: row_facts(db, "actions", ref.action_id)},
                "segment_sets": {ref.set_id: row_facts(db, "segment_sets", ref.set_id)},
            },
            ref,
            segment_read_scope=SegmentReadScope(ref),
        )
