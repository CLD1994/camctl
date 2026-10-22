"""按已发布原片范围逐项固定取回选择，整组事务共同可见。"""

from contextlib import closing
from dataclasses import dataclass, replace
from itertools import islice, zip_longest

from camctl.capture.segment_errors import SegmentErrorHeader
from camctl.capture.segment_models import DefaultScopeState
from camctl.capture.segment_storage import assessment_from_document
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.tasks import CaptureResultPolicy
from camctl.history.events import business_columns, load_event_registry
from camctl.history.reads import PlanReadScope, ReadCoverage
from camctl.history.validators import EventContext
from camctl.outputs.catalog import OutputKind
from camctl.outputs.definitions import SelectionMode, read_selection_request
from camctl.outputs.sources import (
    SourceResolution,
    ResolutionState,
    SelectionFacts,
    select_for_original,
    select_outputs,
)
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    BoundedCommandPlan,
    CommandPlan,
    commit_operation,
    event_envelope,
    next_row_id,
    read_saved_transaction_page,
    read_transaction_range,
    row_change,
    row_facts,
    update_change,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from .segment_result_sets import _boundary, _one

_HEADER = "segment_selection_request"
_EVENT = load_event_registry()["events"]["TARGETS_FIXED"]
_REASON = _EVENT["branches"]["OBTAIN"]["reason"]


@dataclass(frozen=True)
class FixSegmentSelection:
    selection_id: int
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.selection_id)
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class SegmentSelectionReadScope(PlanReadScope):
    selection_id: int


@dataclass(frozen=True)
class _SelectionProof:
    transaction: object
    scope: object
    event: object


def is_segmented_source(db, action_id):
    action = row_facts(db, "actions", action_id)
    return (
        action is not None
        and action["execution_spec_json"].get("result_policy") in (
            CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
            CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
        )
    )


def _at(db, table, identity, transaction, owner):
    row = row_facts(db, table, identity)
    if row is None or transaction is None:
        return row
    if row.get("created_event_id", 0) >= transaction.first_event_id:
        return None
    return {
        **row,
        **read_row_values_at_boundary(
            db,
            owner=owner,
            table=table,
            row_id=identity,
            columns=frozenset(business_columns(table)),
            current_values=row,
            boundary=HistoryBoundary(
                transaction.txn_id - 1, transaction.first_event_id - 1
            ),
            current_boundary=_boundary(db),
            validated_transaction=transaction,
        ),
    }


def _family(db, output_id, transaction=None):
    from .outputs import _CatalogReads, _family_members

    class FamilyReads(_CatalogReads):
        def required(self, table, identity):
            if transaction is None:
                return super().required(table, identity)
            names = {
                "actions": "action",
                "outputs": "output",
                "device_files": "device_file",
                "intermediate_files": "intermediate_file",
            }
            row = _at(db, table, identity, transaction, (names[table], identity))
            if row is None:
                raise ConsistencyError("原选择边界缺少必要文件或产物")
            return row

    return _family_members(FamilyReads(db), output_id)


def qualify(db, request, *, saved=None, batch=128):
    from .outputs import _processing_completed

    selection = row_facts(db, "obtain_source_selections", request.selection_id)
    if selection is None:
        raise ConsistencyError("原来源选择不存在")
    dependency = row_facts(db, "action_dependencies", selection["dependency_id"])
    owner = _at(
        db,
        "actions",
        dependency["action_id"],
        saved,
        ("action", dependency["action_id"]),
    )
    source = _at(
        db,
        "actions",
        dependency["depends_on_action_id"],
        saved,
        ("action", dependency["depends_on_action_id"]),
    )
    before = _at(
        db, "obtain_source_selections", selection["id"], saved, ("action", owner["id"])
    )
    if (
        before["status"] != int(enum_for("obtain_source_selections.status").PENDING)
        or owner["type"] != int(enum_for("actions.type").OBTAIN_ACTION_OUTPUTS)
        or owner["status"] != int(enum_for("actions.status").RUNNING)
        or owner["cancel_requested"]
        or not owner["execution_started"]
        or owner["source_resolution_state"] != 2
        or owner["resolved_source_plan_id"] != source["plan_id"]
        or not is_segmented_source(db, source["id"])
    ):
        raise ConsistencyError("分段选择缺少原执行中取回和固定来源资格")
    if source["status"] not in (3, 4, 5, 6) or not _processing_completed(
        db, source["id"]
    ):
        return None
    set_id = _one(
        db,
        "SELECT id FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL",
        (source["id"],),
    )
    setrow = None if set_id is None else row_facts(db, "segment_sets", set_id[0])
    if (
        setrow is not None
        and saved is not None
        and setrow["published_event_id"] >= saved.first_event_id
    ):
        raise ConsistencyError("选择不能采用原事务之后才发布的来源")
    mode, requested = read_selection_request(
        owner["execution_spec_json"], owner["input_fields_json"]
    )
    scope = DefaultScopeState.UNKNOWN
    if setrow is not None and setrow["assessment_json"] is not None:
        scope = assessment_from_document(setrow["assessment_json"]).default_scope.state
    error = details = None
    if (
        mode is not SelectionMode.EXPLICIT_IDS
        and scope is not DefaultScopeState.NONEMPTY
    ):
        code = (
            "default_segment_scope_empty"
            if scope is DefaultScopeState.EMPTY
            else "default_segment_scope_unconfirmed"
        )
        error = item_error_id("obtain_source_selections", code)
        retained = 0 if setrow is None else _one(
            db,
            "SELECT COUNT(*) FROM outputs o JOIN segment_members m ON m.file_id=o.device_file_id"
            " WHERE o.source_action_id=? AND o.kind=? AND m.set_id=? AND m.role=?",
            (source["id"], int(enum_for("outputs.kind").ORIGINAL), setrow["id"],
             int(enum_for("segment_members.role").ORIGINAL)),
        )[0]
        assessment = (
            None
            if setrow is None or setrow["assessment_json"] is None
            else assessment_from_document(setrow["assessment_json"])
        )
        from camctl.capture.segment_errors import validate_segment_error_header
        from camctl.contracts.workflow_errors import registered_error

        current = (
            validate_segment_error_header(
                "capture_segment_gap", source["error_details_json"]
            )
            if assessment is None
            and source["error_code"]
            == registered_error("capture_segment_gap")["action_error_id"]
            else None
        )
        details = SegmentErrorHeader(
            source["id"],
            getattr(assessment, "gap_count", None)
            if assessment is not None
            else None
            if current is None
            else current.gap_count,
            scope,
            0 if scope is DefaultScopeState.EMPTY else None,
            retained,
            getattr(assessment, "first_gap", None)
            if assessment is not None
            else None
            if current is None
            else current.first_gap,
        ).details(code)
    q = dict(
        request=request,
        selection=before,
        dependency=dependency,
        owner=owner,
        source=source,
        setrow=setrow,
        mode=mode,
        requested=requested,
        error=error,
        details=details,
        batch=batch,
        saved=saved,
    )
    q["count"] = sum(1 for _ in items(db, q))
    if saved is None:
        if _one(
            db,
            "SELECT 1 FROM obtain_items WHERE selection_id=? LIMIT 1",
            (selection["id"],),
        ):
            raise ConsistencyError("原 PENDING 选择已存在条目")
        q["first"] = next_row_id(db, "obtain_items") if q["count"] else 1
    else:
        q["first"] = (
            _one(
                db,
                "SELECT MIN(id) FROM obtain_items WHERE selection_id=?",
                (selection["id"],),
            )[0]
            or 1
        )
    ObjectId(q["first"] + max(0, q["count"] - 1))
    q["event_count"] = (q["count"] + batch - 1) // batch + 1
    return q


def _photo_items(db, q):
    """独立照片沿原片规则选择，不接受视频范围或预览意图过滤。"""
    if (q["setrow"] is None or q["source"]["execution_spec_json"].get("result_policy")
            != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
        return
    after = 0
    while True:
        with closing(db.execute(
            "SELECT o.id FROM outputs o JOIN segment_members m ON m.file_id=o.device_file_id"
            " WHERE o.source_action_id=? AND o.kind=? AND m.set_id=? AND m.role=?"
            " AND o.id>? ORDER BY o.id LIMIT ?",
            (q["source"]["id"], int(enum_for("outputs.kind").ORIGINAL),
             q["setrow"]["id"], int(enum_for("segment_members.role").OTHER),
             after, q["batch"]),
        )) as cursor:
            page = cursor.fetchmany(q["batch"])
        if not page:
            return
        for (output_id,) in page:
            original = next(member.entry for member in _family(db, output_id, q["saved"])
                            if member.entry.kind is OutputKind.ORIGINAL)
            yield select_for_original(q["source"]["id"], original, SelectionMode.DEFAULT)
        after = page[-1][0]


def items(db, q):
    source_id = q["source"]["id"]
    mode = q["mode"]
    if mode is SelectionMode.EXPLICIT_IDS:
        for identity in q["requested"]:
            row = row_facts(db, "outputs", identity)
            entries = (
                ()
                if row is None or row["source_action_id"] != source_id
                else tuple(member.entry for member in _family(db, identity, q["saved"]))
            )
            checked = (
                {}
                if entries
                else {identity: None if row is None else row["source_action_id"]}
            )
            yield from select_outputs(
                SourceResolution(
                    ResolutionState.FIXED,
                    source_plan_id=q["source"]["plan_id"],
                    member_action_ids=(source_id,),
                ),
                SelectionFacts(
                    source_id, True, entries, checked_output_sources=checked
                ),
                mode,
                (identity,),
            ).items
        return
    yield from _photo_items(db, q)
    if q["error"] is not None:
        return
    after = 0
    while True:
        with closing(
            db.execute(
                "SELECT ordinal,output_id FROM segment_scope_members WHERE set_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
                (q["setrow"]["id"], after, q["batch"]),
            )
        ) as cursor:
            page = cursor.fetchmany(q["batch"])
        if not page:
            return
        for ordinal, output_id in page:
            if output_id is None:
                raise ConsistencyError("已发布默认范围缺少正式原片身份")
            family = tuple(
                member.entry for member in _family(db, output_id, q["saved"])
            )
            original = next(
                entry for entry in family if entry.kind is OutputKind.ORIGINAL
            )
            if original.output_id != output_id:
                raise ConsistencyError("默认范围成员不是正式原片")
            preview = next(
                (entry for entry in family if entry.kind is OutputKind.PREVIEW), None
            )
            repaired = next(
                (entry for entry in family if entry.kind is OutputKind.REPAIRED), None
            )
            yield select_for_original(
                source_id, original, mode, preview=preview, repaired=repaired
            )
        after = page[-1][0]


def header(q):
    return {
        "selection_id": q["request"].selection_id,
        "occurred_at": q["request"].occurred_at,
        "set_id": None if q["setrow"] is None else q["setrow"]["id"],
        "published_event_id": None
        if q["setrow"] is None
        else q["setrow"]["published_event_id"],
        "item_count": q["count"],
        "first_item_id": q["first"],
        "batch_size": q["batch"],
    }


def canonical(db, q, transaction):
    from .outputs import _item_values

    iterator = iter(items(db, q))
    identity = q["first"]
    event_id = transaction.first_event_id
    evidence = {_HEADER: header(q)}
    while True:
        batch = tuple(islice(iterator, q["batch"]))
        if not batch:
            break
        rows = tuple(
            row_change(
                "obtain_items",
                identity + offset,
                _item_values(q["request"].selection_id, item),
            )
            for offset, item in enumerate(batch)
        )
        yield event_envelope(
            event_id,
            transaction.txn_id,
            _EVENT["id"],
            _REASON,
            rows,
            q["request"].occurred_at,
            evidence,
        )
        identity += len(batch)
        event_id += 1
    before = q["selection"]
    row = update_change(
        "obtain_source_selections",
        before["id"],
        {name: before[name] for name in ("status", "error_code", "error_details_json")},
        {"status": 2, "error_code": q["error"], "error_details_json": q["details"]},
    )
    yield event_envelope(
        event_id,
        transaction.txn_id,
        _EVENT["id"],
        _REASON,
        (row,),
        q["request"].occurred_at,
        evidence,
    )


def context(db, event, transaction, q):
    owner_id = q["owner"]["id"]
    state = {
        "actions": {
            owner_id: row_facts(db, "actions", owner_id),
            q["source"]["id"]: row_facts(db, "actions", q["source"]["id"]),
        },
        "plans": {q["owner"]["plan_id"]: row_facts(db, "plans", q["owner"]["plan_id"])},
        "action_dependencies": {q["dependency"]["id"]: q["dependency"]},
        "obtain_source_selections": {
            q["selection"]["id"]: row_facts(
                db, "obtain_source_selections", q["selection"]["id"]
            )
        },
        "obtain_items": {},
        "outputs": {},
    }
    for change in event.rows:
        if change.table == "obtain_items":
            state["obtain_items"][change.row_id] = row_facts(
                db, "obtain_items", change.row_id
            )
            if state["obtain_items"][change.row_id] is None:
                del state["obtain_items"][change.row_id]
    for change in event.rows:
        if change.table == "obtain_items":
            for name in ("output_id", "original_output_id", "preview_output_id"):
                identity = change.after.values.get(name)
                if identity is not None:
                    state["outputs"][identity] = row_facts(db, "outputs", identity)
    reads = SqlEventReads(
        db,
        SegmentSelectionReadScope(
            q["owner"]["plan_id"], q["batch"], q["selection"]["id"]
        ),
    )
    return EventContext(
        transaction,
        {(row.table, row.row_id): ("action", owner_id) for row in event.rows},
        state,
        read_coverage=ReadCoverage(),
        current_reads=reads,
    )


class _Command:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        db = scope.connection
        found = _one(
            db, "SELECT id FROM history_transactions WHERE operation_key=?", (self.key,)
        )
        if found is not None:
            transaction = read_transaction_range(db, found[0])
            first = read_saved_transaction_page(
                db, self.key, transaction, limit=1
            ).items[0]
            raw = first.evidence.get(_HEADER)
            if (
                not isinstance(raw, dict)
                or raw.get("selection_id") != self.request.selection_id
                or raw.get("occurred_at") != self.request.occurred_at
            ):
                raise ConsistencyError("原选择键与完整有限申请不同")
            q = qualify(db, self.request, saved=transaction, batch=raw["batch_size"])
            actual = iter(_saved_events(db, self.key, transaction))
            for expected, saved in zip_longest(canonical(db, q, transaction), actual):
                if (
                    expected is None
                    or saved is None
                    or replace(saved, change_seq=None) != expected
                ):
                    raise ConsistencyError("原完整选择事务与同边界逐项规则不同")
            from .outputs import _check_fixed_selection

            _check_fixed_selection(
                db,
                self.request.selection_id,
                {
                    "status": 2,
                    "error_code": q["error"],
                    "error_details_json": q["details"],
                },
                (
                    {"id": row.row_id, "after": {"values": dict(row.after.values)}}
                    for event in _saved_events(db, self.key, transaction)
                    for row in event.rows
                    if row.table == "obtain_items"
                ),
            )
            return CommandPlan((), {}, {}, q["count"], True)
        q = qualify(db, self.request)
        if q is None:
            return CommandPlan((), {}, {}, None, True)
        allocation = scope.allocate(q["event_count"])
        transaction = TransactionRange(
            allocation.txn_id, allocation.first_event_id, allocation.last_event_id
        )
        return BoundedCommandPlan(
            canonical(db, q, transaction),
            lambda event, txn: context(db, event, txn, q),
            q["count"],
            lambda db: None,
        )


def _saved_events(db, key, transaction):
    cursor = None
    while True:
        page = read_saved_transaction_page(db, key, transaction, cursor, limit=128)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def bind(db, event, transaction, reads, state):
    active = state.get("segment_selection")
    raw = event.evidence.get(_HEADER)
    if active is None and raw is None:
        return
    if active is None:
        if event.event_id != transaction.first_event_id or not isinstance(
            reads.scope, SegmentSelectionReadScope
        ):
            raise ConsistencyError("分段选择缺少原首事件及可靠读取范围")
        request = FixSegmentSelection(raw["selection_id"], raw["occurred_at"])
        q = qualify(db, request, batch=reads.scope.batch_size)
        if (
            not json_equal(raw, header(q))
            or q["event_count"]
            != transaction.last_event_id - transaction.first_event_id + 1
            or reads.scope.selection_id != request.selection_id
            or reads.scope.plan_id != q["owner"]["plan_id"]
        ):
            raise ConsistencyError("分段选择头与原连接实际范围不同")
        active = {
            "q": q,
            "iterator": iter(canonical(db, q, transaction)),
            "transaction": transaction,
            "done": False,
        }
        state["segment_selection"] = active
    expected = next(active["iterator"], None)
    if active["done"] or replace(event, change_seq=None) != expected:
        raise ConsistencyError("分段选择事件与原完整规范流不同")
    reads._segment_selection_proof = _SelectionProof(
        transaction, reads.scope, replace(event, change_seq=None)
    )


def verify_guard(event, context):
    proof = getattr(context.current_reads, "_segment_selection_proof", None)
    if proof != _SelectionProof(
        context.transaction,
        context.current_reads.scope,
        replace(event, change_seq=None),
    ):
        raise ConsistencyError("分段选择缺少内核原连接的完整事务核对")


def seal(reads, validated, state):
    active = state.get("segment_selection")
    if active is None:
        return
    verify_guard(
        validated.envelope,
        type(
            "Context",
            (),
            {"current_reads": reads, "transaction": active["transaction"]},
        )(),
    )
    if validated.envelope.event_id == active["transaction"].last_event_id:
        if next(active["iterator"], None) is not None:
            raise ConsistencyError("分段选择没有完整固定所有条目")
        active["done"] = True


def finish(state):
    if (
        state.get("segment_selection") is not None
        and not state["segment_selection"]["done"]
    ):
        raise ConsistencyError("分段选择缺少最终固定状态")


def fix_segment_selection(request, key, owned):
    receipt = commit_operation(_Command(request, key), key, owned)
    return DbOutcome(
        DbOutcomeKind(receipt.kind), value=receipt.result, error=receipt.error
    )
