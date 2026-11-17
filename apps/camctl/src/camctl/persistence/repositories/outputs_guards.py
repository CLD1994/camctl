"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Mapping
from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.enums import enum_for
from camctl.history.decoding import decode_event_row
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.events import reason_for_branch
from camctl.history.validators import EventValidationError
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import validate_relative_file_path
from camctl.operations.models import SettlementBasis
from camctl.outputs.catalog import OutputKind
from camctl.outputs.cleanup_flow import cleanup_read_dependency_blocks, cleanup_other_delete_blocks, cleanup_own_restriction_blocks
from camctl.outputs.competition import has_product_predecessor
from camctl.outputs.processing_rules import processing_completed as _processing_completed_facts, internal_input_requirement as _internal_input_requirement
from camctl.outputs.copy import read_owner_eligible as _read_owner_eligible, segment_advances, segment_exceeds_source, verification_result_error
from camctl.outputs.handoff import publishable_copy_error
from camctl.outputs.definitions import read_excluded_output_ids, read_selection_request
from camctl.outputs.sources import CatalogEntry, OriginalOutputs, ResolutionState, SelectionFacts, SelectionMode, SourceResolution, select_outputs, first_selection_qualification_error
from camctl.persistence.repositories.transactions import row_facts, update_change as _update
from camctl.contracts.workflow_errors import item_error_id, registered_error, registered_error_spec, validate_error_details


_SOURCE_RESOLVED_EVENT = 3


_TARGETS_FIXED_EVENT = 4


_CLEANUP_CHANGED_EVENT = 24


_TARGETS_CLEANUP_REASON = 2


_TARGETS_CANCEL_REASON = 3


_TARGETS_FAIL_REASON = 4


_DELETE_ACTION_TYPE = 5


_CANCEL_ACTION_TYPE = 6


_TARGET_FIXED = 2


_CLEANUP_ITEM_STATUS = enum_for("cleanup_items.status")


_CLEANUP_RESTRICTION = enum_for("cleanup_items.restriction_state")


_CLEANUP_OUTCOME = enum_for("cleanup_items.outcome")


_READ_PERMISSION_EVENT = 21


_COPY_CHANGED_EVENT = 22


_COPY_SOURCE_FALLBACK_REASON = reason_for_branch(
    "COPY_CHANGED", "SOURCE_FALLBACK")


_DELIVERY_CHANGED_EVENT = 23


_INTERMEDIATE_FILE_EVENT = 26


_CLEANUP_CURSOR_EVENT = 32


_RECORDING_DECIDED_EVENT = 18


_RECORDING_PROCESSED_EVENT = 19


_DECIDED_CHECK_REASON = 1


_DECIDED_REPAIR_REASON = 2


_PROCESSED_CHECK_REASON = 1


_PROCESSED_REPAIR_REASON = 2


_PROCESSED_DISCARD_REASON = 3


_CLEANUP_INTENT_REASON = 3


_CLEANUP_RESULT_REASON = 4


_CURSOR_CHECKED_REASON = 1


_FIX_REASON = 1


_OBTAIN_REASON = 1


_ACTION_STATUS = enum_for("actions.status")


_ACTION_TYPE = enum_for("actions.type")


_SELECTION_STATUS = enum_for("obtain_source_selections.status")


_ITEM_STATUS = enum_for("obtain_items.status")


_ITEM_BASIS = enum_for("obtain_items.basis")


_OBTAIN_TYPE = int(_ACTION_TYPE.OBTAIN_ACTION_OUTPUTS)


_OUTPUT_KIND = enum_for("outputs.kind")


_DEVICE_FILE_ROLE = enum_for("device_files.role")


_DEVICE_FILE_COMPLETION = enum_for("device_files.completion_state")


_INTERMEDIATE_PURPOSE = enum_for("intermediate_files.purpose")


_GRANT_REASON = 1


_REJECT_REASON = 2


_RESOLVE_REASON = 4


_SLOT_REASON = 6


_RESET_REASON = 5


_SEGMENT_REASON = 2


_VERIFY_REASON = 3


_RECOPY_REASON = 4


_CONFIGURE_REASON = 7


_PHOTO_READ_START_REASON = reason_for_branch("COPY_CHANGED", "PHOTO_READ_START")


_RELEASE_REASON = 3


_PREPARE_REASON = 2


_INTENT_REASON = 3


_PUBLISH_REASON = 4


_DELIVERY_FAIL_REASON = 5


_LIFECYCLE_REASON = 2


_OUTPUT_NOT_FOUND_CODE = item_error_id("obtain_items", "output_not_found")


_OUTPUT_SOURCE_MISMATCH_CODE = item_error_id("obtain_items", "output_source_mismatch")


_RUN_KIND = enum_for("operation_runs.kind")


_RUN_STATUS = enum_for("operation_runs.status")


_AVAILABILITY = enum_for("outputs.availability")


_PRESENCE = enum_for("device_files.presence_state")


_COMPLETION = enum_for("device_files.completion_state")


_PURPOSE = enum_for("intermediate_files.purpose")


_RETENTION = enum_for("intermediate_files.retention_state")


_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")


_DELIVERY_STATUS = enum_for("deliveries.status")


_WITHDRAWAL = enum_for("deliveries.withdrawal_state")


_RESET_STATE = enum_for("file_copies.reset_state")


_VERIFICATION = enum_for("file_copies.verification_state")


_SEGMENT_INSPECTION_STATE = enum_for("segment_inspections.state")


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _required_current_facts(context, table, identity):
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"取回成员的当前关联事实缺失: {table}#{identity}")
    return facts


def _read_is_due(action, occurred_at: int) -> bool:
    """首次读取建档的时间条件；不代替会话墙钟可信性检查。"""
    if action is None or not is_json_integer(action.get("scheduled_at")):
        raise ConsistencyError("读取发起动作的计划时间缺失或无法解释")
    return action["scheduled_at"] <= occurred_at


def _guard_read_owner(event, context, action_id) -> None:
    action = context.state_rows.get("actions", {}).get(action_id)
    try:
        if not _read_owner_eligible(action):
            raise EventValidationError("首次读取建档要求发起动作执行中且未取消")
        due = _read_is_due(action, event.occurred_at)
    except ConsistencyError as error:
        raise EventValidationError(str(error)) from error
    if not due:
        raise EventValidationError("读取发起动作尚未到计划时间")


@dataclass(frozen=True)
class _CatalogMember:
    """已核对文件关系的产物行及其选择依据。"""

    row: Mapping[str, Any]
    entry: CatalogEntry


def _include_family(reads: _FamilyReads, members, source_action_id, output_id) -> None:
    if output_id in members:
        return
    for member in _family_members(reads, output_id):
        if member.row["source_action_id"] != source_action_id:
            raise ConsistencyError("产物关联集合不属于指定来源")
        members[member.entry.output_id] = member


class _CurrentCatalogReads:
    """按当前行建立关联索引，查询不扫描未来提案或重复扫描全部关联。"""

    def __init__(self, context):
        self.context = context
        self._origins = {}
        self._related = {}
        for row in context.state_rows.get("output_origins", {}).values():
            try:
                output_id, original_id = row["output_id"], row["original_output_id"]
                for identity in (output_id, original_id):
                    if not is_json_integer(identity):
                        raise ValueError("关联身份必须是整数")
                    ObjectId(int(identity))
            except (KeyError, ValueError) as error:
                raise ConsistencyError("产物原片关联身份缺失或无效") from error
            if output_id in self._origins:
                raise ConsistencyError("同一产物不能重复登记原片关联")
            self._origins[int(output_id)] = int(original_id)
            self._related.setdefault(int(original_id), []).append(int(output_id))

    def required(self, table: str, identity: int) -> Mapping[str, Any]:
        if not is_json_integer(identity):
            raise ConsistencyError(f"产物关联身份无效: {table}#{identity!r}")
        try:
            identity = ObjectId(int(identity))
        except ValueError as error:
            raise ConsistencyError(f"产物关联身份越界: {table}#{identity!r}") from error
        facts = self.context.state_rows.get(table, {}).get(identity)
        if facts is None:
            raise ConsistencyError(f"产物关联记录缺失: {table}#{identity}")
        if "id" in facts:
            if not is_json_integer(facts["id"]) or facts["id"] != identity:
                raise ConsistencyError(f"产物关联行身份矛盾: {table}#{identity}")
        # Saved JSON facts may represent an integral database ID as Decimal.
        # Once exact-integrality, equality, and ObjectId range have been checked,
        # expose the canonical integer form to strict file-path identity APIs.
        return {**facts, "id": int(identity)}

    def _require_coverage(self, column, identity):
        if not self.context.read_coverage.covers("output_origins", column, identity):
            raise EventValidationError(f"缺少原片关联完整读取范围: {column}={identity}")

    def origin(self, identity: int) -> int | None:
        self._require_coverage("output_id", identity)
        return self._origins.get(identity)

    def related(self, original_id: int) -> tuple[int, ...]:
        self._require_coverage("original_output_id", original_id)
        return tuple(sorted(self._related.get(original_id, ())))

    def catalog(self, source_action_id: int) -> tuple[_CatalogMember, ...]:
        rows = self.context.complete_rows("outputs", "source_action_id", source_action_id)
        members: dict[int, _CatalogMember] = {}
        for output_id in sorted(rows):
            _include_family(self, members, source_action_id, output_id)
        return tuple(sorted(members.values(), key=lambda member: member.entry.output_id))


class _CurrentProductReads(_CurrentCatalogReads):
    """正式资格守卫复核同一范围的当前行，不读取事务未来提案。"""

    def __new__(cls, context):
        if context.current_reads is not None:
            return _BoundedProductReads(context.current_reads)
        return super().__new__(cls)

    def optional(self, table, identity):
        rows = self.context.complete_rows(table, "id", identity)
        return self.required(table, identity) if rows else None

    def members(self, table, column, value):
        rows = self.context.complete_rows(table, column, value)
        return {identity: self.required(table, identity) for identity in rows}

    def action(self, identity):
        return self.optional("actions", identity)

    def plan_actions(self, plan_id):
        return self.members("actions", "plan_id", plan_id)

    def actions(self):
        rows = {}
        for state in (_ACTION_STATUS.PENDING, _ACTION_STATUS.RUNNING):
            rows.update(self.members("actions", "status", int(state)))
        return tuple(rows.items())

    def family(self, output_id):
        return _original_outputs(self, output_id)

    def processing_completed(self, source_id):
        rows = self.members("recording_processing", "action_id", source_id)
        if len(rows) > 1:
            raise ConsistencyError("录像处理责任重复")
        return _processing_completed_facts(next(iter(rows.values()), None))


class _BoundedProductReads:
    """资格计算与守卫各自读取当前批；不把比较对象累计到命令提案。"""

    def __init__(self, view):
        self.view = view

    def required(self, table, identity):
        return self.view.required(table, identity)

    def optional(self, table, identity):
        return self.view.optional(table, identity)

    def action(self, identity):
        return self.view.optional("actions", identity, self.view.SOURCE_COLUMNS)

    def iter_members(self, table, column, identity):
        for batch in self.view.iter_members(table, column, identity):
            yield from batch

    def iter_plan_actions(self, identity):
        for batch in self.view.iter_members("actions", "plan_id", identity, columns=self.view.SOURCE_COLUMNS):
            yield from batch

    def actions(self):
        return self.view.actions()

    def family(self, output_id):
        return _original_outputs(self, output_id)

    def origin(self, output_id):
        rows = iter(self.iter_members("output_origins", "output_id", output_id))
        first = next(rows, None)
        if next(rows, None) is not None:
            raise ConsistencyError("同一产物不能重复登记原片关联")
        return None if first is None else first[1]["original_output_id"]

    def related(self, original_id):
        result = []
        for _, row in self.iter_members("output_origins", "original_output_id", original_id):
            result.append(row["output_id"])
            if len(result) > 2:
                raise ConsistencyError("同一原片最多登记一份预览和一份修复成品")
        return tuple(result)

    def processing_completed(self, source_id):
        rows = iter(self.iter_members("recording_processing", "action_id", source_id))
        first = next(rows, None)
        if next(rows, None) is not None:
            raise ConsistencyError("录像处理责任重复")
        return _processing_completed_facts(None if first is None else first[1])


def _original_outputs(reads, output_id):
    members = _family_members(reads, output_id)
    by_kind = {member.entry.kind: member.entry for member in members}
    return OriginalOutputs(members[0].row["source_action_id"], by_kind[OutputKind.ORIGINAL],
                           by_kind.get(OutputKind.PREVIEW), by_kind.get(OutputKind.REPAIRED))


def _unconfirmed_preview_ids(reads, source, members):
    """仅为尚无正式预览的设备原片解释同源结果证据。"""
    if (source["execution_spec_json"] or {}).get("video_result_mode") is None:
        return frozenset()
    paired = {member.entry.original_output_id for member in members
              if member.entry.kind is OutputKind.PREVIEW}
    originals = {}
    for member in members:
        if (member.entry.kind is not OutputKind.ORIGINAL or member.entry.output_id in paired
                or member.row["device_file_id"] is None):
            continue
        file = reads.required("device_files", member.row["device_file_id"])
        originals[member.entry.output_id] = file["locator_json"]["path"]
    if not originals:
        return frozenset()
    from .preview_reads import PreviewReads
    if isinstance(reads, _CurrentCatalogReads):
        reader = reads.context.preview_reads
        if type(reader) is not PreviewReads:
            raise ConsistencyError("预览选择缺少来源结果读取端口")
    else:
        reader = PreviewReads(reads.connection)
    return reader.unconfirmed(source, originals)


def _host_source_purpose(kind, action_type):
    """正式主机源的角色用途；修复与单张拍摄分别限定所属动作。"""
    if kind == int(_OUTPUT_KIND.REPAIRED) and action_type == int(_ACTION_TYPE.CAMERA_RECORD):
        return _PURPOSE.REPAIR_OUTPUT
    if action_type == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO):
        if kind == int(_OUTPUT_KIND.ORIGINAL):
            return _PURPOSE.CAPTURE_ORIGINAL
        if kind == int(_OUTPUT_KIND.PREVIEW):
            return _PURPOSE.CAPTURE_PREVIEW
    raise ConsistencyError("主机产物种类与来源动作不一致")


def _verify_host_cleanup_consistency(output, source_file) -> None:
    """主机正式产物与承载文件必须对可靠清理完成保持一致。"""
    if ((output["availability"] == int(_AVAILABILITY.CLEANED))
            != (source_file["cleanup_state"] == int(_FILE_CLEANUP.COMPLETED))):
        raise ConsistencyError("主机产物清理状态与源文件完成事实不一致")


def _family_members(reads: _FamilyReads, output_id: int) -> tuple[_CatalogMember, ...]:
    """逐产物、完整来源及当前事实解释共用的固定关系边界。"""
    ObjectId(output_id)
    required, origin = reads.required, reads.origin

    target = required("outputs", output_id)
    original_id = origin(output_id)
    if target["kind"] == int(_OUTPUT_KIND.ORIGINAL):
        if original_id is not None:
            raise ConsistencyError("原片不能携带派生关联")
        original = target
        original_id = output_id
    else:
        if original_id is None:
            raise ConsistencyError("派生产物缺少原片关联")
        original = required("outputs", original_id)
        if original["kind"] != int(_OUTPUT_KIND.ORIGINAL) or origin(original_id) is not None:
            raise ConsistencyError("派生产物必须指向没有派生关联的原片")
    related_ids = reads.related(original_id)
    if len(related_ids) > 2:
        raise ConsistencyError("同一原片最多登记一份预览和一份修复成品")
    by_kind = {}
    for identity in (original_id, *related_ids):
        entry = original if identity == original_id else target if identity == output_id else required("outputs", identity)
        kind = OutputKind[_OUTPUT_KIND(entry["kind"]).name]
        if kind in by_kind or entry["source_action_id"] != original["source_action_id"]:
            raise ConsistencyError("派生产物重复或不属于原片来源")
        is_device = entry["device_file_id"] is not None
        file = required("device_files" if is_device else "intermediate_files",
                        entry["device_file_id"] if is_device else entry["intermediate_file_id"])
        if file["source_action_id" if is_device else "owner_action_id"] != original["source_action_id"]:
            raise ConsistencyError("产物承载文件不属于原片来源")
        if is_device:
            expected_role = {
                OutputKind.ORIGINAL: _DEVICE_FILE_ROLE.ORIGINAL,
                OutputKind.PREVIEW: _DEVICE_FILE_ROLE.PREVIEW,
            }.get(kind)
            if expected_role is None or file["role"] != int(expected_role):
                raise ConsistencyError("设备产物与承载文件角色不一致")
            if file["completion_state"] != int(_COMPLETION.COMPLETE):
                raise ConsistencyError("已登记设备产物必须保留文件完成事实")
            if kind is OutputKind.PREVIEW:
                if (file["original_device_file_id"] != original["device_file_id"]
                        or file["pairing_evidence_json"] is None):
                    raise ConsistencyError("预览的设备文件配对与产物原片关联不一致")
            elif (file["original_device_file_id"] is not None
                  or file["pairing_evidence_json"] is not None):
                raise ConsistencyError("原片文件不能携带预览配对")
        else:
            source_action = required("actions", original["source_action_id"])
            purpose = _host_source_purpose(entry["kind"], source_action["type"])
            if (file["owner_delivery_id"] is not None
                    or file["purpose"] != int(purpose)
                    or file["retention_state"] != int(_RETENTION.PROMOTED)
                    or file["cleanup_state"] not in (int(_FILE_CLEANUP.NOT_NEEDED), int(_FILE_CLEANUP.COMPLETED))):
                raise ConsistencyError("主机产物必须由来源动作的已提升角色文件承载")
            _verify_host_cleanup_consistency(entry, file)
            validate_relative_file_path(FilePurpose[purpose.name], file["id"], file["relative_path"])
            if kind is OutputKind.PREVIEW and original["intermediate_file_id"] is None:
                raise ConsistencyError("主机预览必须关联同源主机原图")
        size = file["size_bytes"]
        if size is not None and (not is_json_integer(size) or size < 0):
            raise ConsistencyError("产物完整长度无法解释")
        by_kind[kind] = _CatalogMember(
            row=entry,
            entry=CatalogEntry(
                output_id=identity, kind=kind, availability=entry["availability"], size_bytes=size,
                original_output_id=None if identity == original_id else original_id,
            ),
        )
    if output_id not in (original_id, *related_ids):
        raise ConsistencyError("目标产物未保留在原片派生关系中")
    return tuple(by_kind.values())


def _known_output_ids(values: Mapping[str, Any]) -> set[int]:
    """保存的关联及来源不匹配证明记录存在；裸请求不构成存在证明。"""
    known = {values.get(name) for name in ("output_id", "original_output_id", "preview_output_id")} - {None}
    if values.get("error_code") == _OUTPUT_SOURCE_MISMATCH_CODE:
        requested = values.get("requested_output_id")
        if requested is None:
            raise ConsistencyError("来源不匹配的取回项缺少原请求产物身份")
        known.add(requested)
    return known


def _item_values(selection_id: int, item: SelectedItem) -> dict[str, Any]:
    return {
        "selection_id": selection_id,
        "requested_output_id": item.requested_output_id,
        "output_id": item.output_id,
        "basis": int(item.basis),
        "original_output_id": item.original_output_id,
        "preview_output_id": item.preview_output_id,
        "preview_size": item.preview_size,
        "repaired_size": item.repaired_size,
        "status": int(item.status),
        "source_dependency": 0,
        "delivery_id": None,
        "error_code": item.error_code,
        "error_details_json": (
            dict(item.error_details) if item.error_details is not None else None
        ),
    }


_ATTEMPT_STATUS = enum_for("operation_attempts.status")


_RUN_TERMINAL = frozenset(
    int(_RUN_STATUS[member]) for member in ("SUCCEEDED", "FAILED", "CANCELED", "UNCONFIRMED", "EXPIRED")
)


def _canceled_binding_processing_settled(connection, copy, run, action):
    """取消已生效的处理副本只按原绑定读取结果释放机会。"""
    error = run.get("error_json")
    if (copy.get("processing_id") is None or action.get("cancel_requested") != 1
            or action.get("status") not in (2, 6) or run.get("status") != int(_RUN_STATUS.FAILED)
            or not isinstance(error, Mapping) or error.get("code") != "device_binding_unavailable"):
        return None
    from .binding_read_cancel import load_saved_binding_read_failure
    load_saved_binding_read_failure(connection, action["id"], copy["id"], error)
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND (t.status=1 OR t.result_json IS NULL) LIMIT 1", (action["id"],),
    )) as cursor:
        return cursor.fetchone() is None


def _deferred_photo_source(connection, source) -> bool:
    """识别原动作固定的按需照片来源，同时核对定位身份。"""
    if source is None or source.get("size_bytes") is not None or source.get("sha256") is not None:
        return False
    action = row_facts(connection, "actions", source["source_action_id"])
    if action is None or read_action_spec(action).get("photo_source_policy") != "deferred":
        return False
    from camctl.devices.drivers.hasselblad.files import DeferredCameraFileIdentity, camera_file_identity_from_source
    identity = camera_file_identity_from_source(source["identity_key"], source["locator_json"])
    return isinstance(identity, DeferredCameraFileIdentity)


def delivery_read_succeeded(connection, delivery_id):
    """交付发布以前，原读取流程及尝试必须已有可靠成功结果。"""
    from .retry_wait_reads import RetryWaitReads

    with closing(connection.execute(
        "SELECT r.id,r.status,r.attempts_used,r.retry_wait_required,"
        " t.status,t.result_json,t.result_event_id FROM file_copies c"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " LEFT JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=r.attempts_used"
        " WHERE c.delivery_id=? LIMIT 2", (int(_RUN_KIND.READ_FILE), delivery_id),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        return False
    run_id, status, used, waiting, attempt_status, result, event_id = rows[0]
    return (status == int(_RUN_STATUS.SUCCEEDED) and used > 0 and waiting == 0
            and attempt_status == int(_ATTEMPT_STATUS.SUCCEEDED)
            and result is not None and event_id is not None
            and RetryWaitReads(connection).no_unsettled_attempts(run_id))


def _running_file_operations(connection, file_id):
    with closing(connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id"
        " JOIN file_copies c ON r.copy_id=c.id"
        " WHERE (c.target_file_id=? OR c.source_intermediate_file_id=?) AND a.status=1",
        (file_id, file_id))) as cursor:
        return cursor.fetchone()[0]


def _selection_initialization_guard(event, context) -> None:
    """选择初始化守卫：每个固定来源恰好一条 PENDING 选择。

    执行期来源固定在 SOURCE_RESOLVED.FIX 内共同建立依赖与选择；
    取回开始（ACTION_STARTED）为受理时已固定的来源初始化选择。
    取消生效的取回不再普通初始化（由行规格 before 核对）。
    """
    from .source_transactions import required_stream, verify_source_guard
    reads = context.current_reads
    if reads is not None and required_stream(reads.connection, event) is not None:
        verify_source_guard(event, context)
        return
    if event.event_type == _SOURCE_RESOLVED_EVENT and event.reason == _FIX_REASON:
        raise EventValidationError("执行期来源固定缺少原申请读取范围")
    created = [r for r in event.rows if r.table == "obtain_source_selections"]
    if created:
        raise EventValidationError("取回初始化缺少可靠原申请")


def _current_selection_snapshot(context, selection_id: int, *, preview_validation_version=0) -> tuple[int, SelectionSnapshot]:
    """只按完整当前事实解释首次选择，不采用事件结果或未来事务行。"""
    reads = _CurrentCatalogReads(context)
    selection = reads.required("obtain_source_selections", selection_id)
    if (not json_equal(selection["status"], int(_SELECTION_STATUS.PENDING))
            or selection["error_code"] is not None or selection["error_details_json"] is not None):
        raise EventValidationError("首次固定要求当前选择为无错误的 PENDING")
    dependency = reads.required("action_dependencies", selection["dependency_id"])
    owner = reads.required("actions", dependency["action_id"])
    source = reads.required("actions", dependency["depends_on_action_id"])
    source_id = int(source["id"])
    from camctl.devices.tasks import CaptureResultPolicy

    source_spec = source.get("execution_spec_json")
    if (source_spec is not None
            and source_spec.get("result_policy")
            == CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value):
        raise EventValidationError("分段来源选择须核对原完整范围事务")
    for facts, columns in (
        (owner, ("type", "status", "source_resolution_state", "resolved_source_plan_id")),
        (source, ("type", "status", "plan_id")),
    ):
        if not all(is_json_integer(facts[column]) for column in columns):
            raise EventValidationError("当前动作的类型、状态或来源身份无效")
    source_plan_id = ObjectId(int(source["plan_id"]))
    error = first_selection_qualification_error(owner=owner, source=source, source_plan_id=source_plan_id)
    if error is not None:
        raise EventValidationError(error)
    if context.complete_rows("obtain_items", "selection_id", selection_id):
        raise EventValidationError("PENDING 选择已有条目，不能追加首次选择")
    mode, requested = read_selection_request(owner["execution_spec_json"], owner["input_fields_json"])
    if preview_validation_version and mode is SelectionMode.PREVIEW:
        context.preview_reads.require_writable()
    processing = context.complete_rows("recording_processing", "action_id", source_id)
    if len(processing) > 1:
        raise EventValidationError("来源动作不能存在多条处理记录")
    if not _processing_completed_facts(next(iter(processing.values()), None)):
        raise EventValidationError("来源适用产物处理未完成")
    members = reads.catalog(source_id)
    entries = tuple(member.entry for member in members)
    local_ids = {entry.output_id for entry in entries}
    checked = {}
    for identity in requested:
        if identity in local_ids:
            continue
        found = context.complete_rows("outputs", "id", identity)
        if not found:
            checked[identity] = None
        else:
            actual_source = reads.required("outputs", identity)["source_action_id"]
            if not is_json_integer(actual_source):
                raise EventValidationError("请求产物的当前来源身份无效")
            checked[identity] = int(ObjectId(int(actual_source)))
    expected = select_outputs(
        SourceResolution(state=ResolutionState.FIXED, member_action_ids=(source_id,),
                         source_plan_id=source_plan_id),
        SelectionFacts(
            source_id,
            True,
            entries,
            checked_output_sources=checked,
            source_failed=(source["status"] == int(_ACTION_STATUS.FAILED)),
            unconfirmed_preview_ids=(_unconfirmed_preview_ids(reads, source, members)
                                     if preview_validation_version and mode is SelectionMode.PREVIEW else frozenset()),
            preview_validation_version=preview_validation_version,
        ), mode, requested,
        excluded_output_ids=read_excluded_output_ids(owner["input_fields_json"]))
    return source_id, expected


def _source_selection_guard(event, context) -> None:
    """逐来源选择守卫：按保存请求核对完整结果及条目 ID 对应的顺序。"""
    if event.event_type != _TARGETS_FIXED_EVENT or event.reason != _OBTAIN_REASON:
        return
    if "segment_selection_request" in event.evidence:
        from .segment_selections import verify_guard

        verify_guard(event, context)
        return
    selection_updates = [
        row
        for row in event.rows
        if row.table == "obtain_source_selections" and row.before.exists
    ]
    items = [
        row
        for row in event.rows
        if row.table == "obtain_items" and not row.before.exists
    ]
    if len(selection_updates) != 1:
        raise EventValidationError("选择固定必须恰好更新一条来源选择")
    selection = selection_updates[0]
    if selection.before.values.get("status") != int(_SELECTION_STATUS.PENDING):
        raise EventValidationError("只有 PENDING 选择能固定")
    try:
        from camctl.outputs.preview_facts import read_preview_validation_version

        source_id, expected = _current_selection_snapshot(
            context, selection.row_id,
            preview_validation_version=read_preview_validation_version(event.evidence),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EventValidationError(f"首次选择的当前事实无法解释: {error}") from error
    items.sort(key=lambda row: row.row_id)
    if len(items) != len(expected.items) or any(
        not json_equal(row.after.values, _item_values(selection.row_id, item))
        for row, item in zip(items, expected.items)
    ):
        raise EventValidationError("选择条目的完整集合、保存顺序或依据与原请求及当前事实不一致")
    error_code = selection.after.values.get("error_code")
    details = selection.after.values.get("error_details_json")
    if not json_equal(error_code, expected.source_error_code):
        raise EventValidationError("来源选择错误与原请求及当前事实不一致")
    _validate_saved_error("obtain_source_selections", error_code, details)
    if details is not None:
        if ("source_action_instance_id" in details
                and details["source_action_instance_id"] != str(source_id)):
            raise EventValidationError("来源错误必须指向实际固定来源")
        if "original_output_id" in details:
            original = context.state_rows.get("outputs", {}).get(int(details["original_output_id"]))
            if (original is None or original.get("source_action_id") != source_id
                    or original.get("kind") != int(_OUTPUT_KIND.ORIGINAL)):
                raise EventValidationError("来源错误中的原片必须属于实际固定来源")
    for item in items:
        _validate_obtain_error(item.after.values, source_id)


def _intermediate_guard(event, context) -> None:
    """中间文件建档与字节事实守卫：用途互斥、初始状态固定。"""
    if event.event_type == _INTERMEDIATE_FILE_EVENT:
        from .host_capture import validate_host_photo_event
        validate_host_photo_event(event, context)
    if event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == _CLEANUP_RESULT_REASON:
        request = event.evidence.get("cleanup_request")
        if (not isinstance(request, Mapping) or set(request) != {"advance_cursor"}
                or type(request["advance_cursor"]) is not bool):
            raise EventValidationError("清理结果必须保存精确布尔 advance_cursor 申请")
    if event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == 1:
        for row in event.rows:
            if row.table != "intermediate_files" or row.before.exists:
                raise EventValidationError("中间文件建档必须是创建行")
            values = row.after.values
            purpose = values.get("purpose")
            if values.get("retention_state") != int(_RETENTION.REQUIRED):
                raise EventValidationError("新建中间文件必须处于 REQUIRED 保留状态")
            if values.get("cleanup_state") != int(_FILE_CLEANUP.NOT_NEEDED):
                raise EventValidationError("新建中间文件不得登记清理待办")
            if values.get("size_bytes") is not None or values.get("sha256") is not None:
                raise EventValidationError("新建中间文件尚无内容事实")
            has_delivery = values.get("owner_delivery_id") is not None
            has_action = values.get("owner_action_id") is not None
            if purpose == int(_PURPOSE.DELIVERY_COPY):
                if not has_delivery or has_action:
                    raise EventValidationError("交付副本必须归属交付且不归属动作")
            elif has_delivery or not has_action:
                raise EventValidationError("处理用途的中间文件必须归属动作")
    elif event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == _LIFECYCLE_REASON:
        for row in event.rows:
            if row.table != "intermediate_files":
                continue
            after = row.after.values
            if after.get("retention_state") == int(_RETENTION.RELEASABLE):
                facts = context.state_rows.get("intermediate_files", {}).get(row.row_id)
                if facts is not None and facts.get("purpose") in (int(_PURPOSE.CAPTURE_ORIGINAL), int(_PURPOSE.CAPTURE_PREVIEW)):
                    reads = context.photo_result_reads
                    if reads is None:
                        raise EventValidationError("照片缓存释放缺少独立原事务读取")
                    from camctl.persistence.repositories.photo_cache_reuse import photo_cache_waiters_settled
                    if not photo_cache_waiters_settled(row.row_id, SimpleNamespace(connection=reads.connection)):
                        raise EventValidationError("照片缓存仍被 H 时固定的取回使用")
                    if _running_file_operations(reads.connection, row.row_id):
                        raise EventValidationError("照片缓存仍有源或目标读取尝试未结束")
            if "sha256" in after or "size_bytes" in after:
                sync_cache_size = (event.evidence.get("photo_sync_cache_request") is not None
                                   and set(after) == {"size_bytes"} and after["size_bytes"] is not None)
                if not sync_cache_size and (after.get("sha256") is None or after.get("size_bytes") is None):
                    raise EventValidationError("完整字节事实必须同时携带长度与摘要")
    elif event.event_type == _INTERMEDIATE_FILE_EVENT \
            and event.reason == _CLEANUP_INTENT_REASON:
        for row in event.rows:
            if row.table != "intermediate_files" or not row.before.exists:
                raise EventValidationError("清理意图必须是中间文件更新行")
            after = row.after.values
            if set(after) != {"cleanup_state"}:
                raise EventValidationError("清理意图只推进清理状态")
            if after.get("cleanup_state") != int(_FILE_CLEANUP.RUNNING):
                raise EventValidationError("清理意图必须推进到 RUNNING")
            file = context.state_rows.get("intermediate_files", {}).get(row.row_id)
            if file is None or file.get("retention_state") != int(_RETENTION.RELEASABLE):
                raise EventValidationError("清理意图要求文件已释放保留")
    elif event.event_type == _INTERMEDIATE_FILE_EVENT \
            and event.reason == _CLEANUP_RESULT_REASON:
        for row in event.rows:
            if row.table != "intermediate_files" or not row.before.exists:
                raise EventValidationError("清理结果必须是中间文件更新行")
            after = row.after.values
            if set(after) - {"cleanup_state", "last_error_json"}:
                raise EventValidationError("清理结果只更新清理状态与错误")
            target = after.get("cleanup_state")
            if target not in (
                int(_FILE_CLEANUP.COMPLETED),
                int(_FILE_CLEANUP.FAILED),
                int(_FILE_CLEANUP.UNKNOWN),
            ):
                raise EventValidationError("清理结果必须进入完成或失败分区")
            error = after.get("last_error_json")
            if target == int(_FILE_CLEANUP.COMPLETED) and error is not None:
                raise EventValidationError("清理完成不得携带错误")
            if target in (
                int(_FILE_CLEANUP.FAILED), int(_FILE_CLEANUP.UNKNOWN),
            ):
                if not isinstance(error, Mapping):
                    raise EventValidationError("清理失败必须保存结构化错误")
                code, stage = error.get("code"), error.get("stage")
                details = error.get("details")
                try:
                    spec = registered_error(code)
                    if stage != spec["stage"]:
                        raise ValueError(
                            f"失败阶段与公共登记不符: {stage!r} != {spec['stage']!r}")
                    validate_error_details(code, details)
                except (TypeError, ValueError) as failure:
                    raise EventValidationError(str(failure)) from failure


def _cursor_guard(event, context) -> None:
    """历史清理游标守卫：只推进继续位置且必须指向已检查文件。"""
    if event.event_type == _CLEANUP_CURSOR_EVENT \
            and event.reason == _CURSOR_CHECKED_REASON:
        for row in event.rows:
            if row.table != "runtime_state" or not row.before.exists:
                raise EventValidationError("游标推进必须是运行状态更新行")
            after = row.after.values
            if set(after) != {"cleanup_cursor_file_id"}:
                raise EventValidationError("游标事件只更新清理继续位置")
            if after.get("cleanup_cursor_file_id") is None:
                raise EventValidationError("游标推进必须指向已检查的中间文件")


def _delivery_guard(event, context) -> None:
    """交付建档与准备阶段守卫：初始待准备，完成准备以校验为前提。"""
    if event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 1:
        for row in event.rows:
            if row.table != "deliveries" or row.before.exists:
                raise EventValidationError("交付建档必须是创建行")
            values = row.after.values
            if values.get("status") != int(_DELIVERY_STATUS.PENDING):
                raise EventValidationError("新建交付必须处于 PENDING")
            if values.get("withdrawal_state") != int(_WITHDRAWAL.NOT_REQUESTED):
                raise EventValidationError("新建交付不得携带撤回状态")
            if (
                values.get("publication_intent_event_id") is not None
                or values.get("published_event_id") is not None
                or values.get("error_json") is not None
            ):
                raise EventValidationError("新建交付不得携带发布或错误事实")
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _PREPARE_REASON:
        for row in event.rows:
            if row.table != "deliveries" or row.after.values.get("status") != \
                    int(_DELIVERY_STATUS.PREPARED):
                continue
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _INTENT_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            after = row.after.values
            if after.get("status") != int(_DELIVERY_STATUS.PUBLISHING) \
                    or after.get("publication_intent_event_id") != event.event_id:
                raise EventValidationError(
                    "发布意图必须推进到 PUBLISHING 并引用本意图事件")
            _require_publishable_copy(context, row.row_id)
            from .retry_wait_reads import RetryWaitReads
            reads = context.retry_wait_reads
            if type(reads) is not RetryWaitReads or not delivery_read_succeeded(reads.connection, row.row_id):
                raise EventValidationError("发布意图要求原读取流程可靠成功")
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _PUBLISH_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            after = row.after.values
            if after.get("status") != int(_DELIVERY_STATUS.PUBLISHED) \
                    or after.get("published_event_id") != event.event_id:
                raise EventValidationError(
                    "交接完成必须推进到 PUBLISHED 并引用本完成事件")
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 6:
        evidence = event.evidence.get("canceled_publication")
        for row in event.rows:
            if row.table != "deliveries":
                continue
            publishing = row.before.values.get("status") == int(_DELIVERY_STATUS.PUBLISHING)
            if not publishing:
                if evidence is not None:
                    raise EventValidationError("非发布中交付不能携带发布对账依据")
                continue
            if (not isinstance(evidence, Mapping)
                    or set(evidence) != {"delivery_id", "copy_id", "file_id", "location"}
                    or evidence.get("location") not in ("staging", "unknown")
                    or evidence.get("delivery_id") != row.row_id):
                raise EventValidationError("取消发布中交付缺少原副本观察依据")
            delivery = context.state_rows.get("deliveries", {}).get(row.row_id)
            copy = context.state_rows.get("file_copies", {}).get(evidence["copy_id"])
            target = context.state_rows.get("intermediate_files", {}).get(evidence["file_id"])
            action = None if delivery is None else context.state_rows.get("actions", {}).get(delivery["action_id"])
            if (delivery is None or delivery.get("publication_intent_event_id") is None
                    or action is None or action.get("type") != _OBTAIN_TYPE
                    or action.get("cancel_requested") != 1
                    or copy is None or copy.get("delivery_id") != row.row_id
                    or sum(candidate.get("delivery_id") == row.row_id for candidate
                           in context.state_rows.get("file_copies", {}).values()) != 1
                    or copy.get("target_file_id") != evidence["file_id"]
                    or target is None or target.get("owner_delivery_id") != row.row_id):
                raise EventValidationError("取消发布中交付的原责任或文件身份不一致")
            if (evidence["location"] == "unknown"
                    and delivery.get("withdrawal_state") != int(_WITHDRAWAL.UNKNOWN)):
                raise EventValidationError("未知位置结论必须已经可靠保存")
            if (evidence["location"] == "staging"
                    and delivery.get("withdrawal_state") == int(_WITHDRAWAL.UNKNOWN)):
                raise EventValidationError("未知位置不能作为 staging 副本处理")
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT \
            and event.reason == _DELIVERY_FAIL_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            error = row.after.values.get("error_json")
            if row.after.values.get("status") != int(_DELIVERY_STATUS.FAILED):
                continue
            if not isinstance(error, Mapping):
                raise EventValidationError("交付终局失败必须保存结构化错误")
            code, stage = error.get("code"), error.get("stage")
            details = error.get("details")
            if code in ("read_attempts_exhausted", "checksum_mismatch", "source_file_unconfirmed",
                        "delivery_handoff_unconfirmed"):
                delivery = context.state_rows.get("deliveries", {}).get(row.row_id)
                action = None if delivery is None else context.state_rows.get("actions", {}).get(delivery.get("action_id"))
                if action is None or action.get("cancel_requested") != 0:
                    raise EventValidationError("交付失败要求发起动作的取消尚未生效")
                if code == "delivery_handoff_unconfirmed" and action.get("status") != int(_ACTION_STATUS.RUNNING):
                    raise EventValidationError("交接未知失败要求原取回仍在执行")
            try:
                spec = registered_error(code)
                if stage != spec["stage"]:
                    raise ValueError(
                        f"失败阶段与公共登记不符: {stage!r} != {spec['stage']!r}")
                validate_error_details(code, details)
            except (TypeError, ValueError) as failure:
                raise EventValidationError(str(failure)) from failure
            # 详情按公共登记结构化：带交付身份字段的（如交接未确
            # 认）必须与本行一致；无身份字段的详情由命令装配的输
            # 入交付定位，守卫不追加身份。
            if details.get("delivery_id") is not None \
                    and details.get("delivery_id") != str(row.row_id):
                raise EventValidationError("交付失败详情必须关联本次交付")
            if code == "source_device_unavailable":
                from .preparation import PreparationReads
                from .availability_failure import verify_read_failure
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("设备不可用失败缺少原连接事实")
                verify_read_failure(context.preparation_reads.connection, event, context, row)
            if code == "device_binding_unavailable" and details.get("reason") in ("missing", "mismatch"):
                copies = [copy for copy in context.state_rows.get("file_copies", {}).values()
                          if copy.get("delivery_id") == row.row_id]
                if len(copies) == 1 and copies[0].get("source_device_file_id") is None:
                    reads = context.current_reads
                    if reads is None:
                        raise EventValidationError("照片缓存绑定失败缺少原事务事实")
                    from camctl.persistence.repositories.photo_cache_reuse import verify_photo_cache_binding_failure
                    source_id = verify_photo_cache_binding_failure(reads.connection, copies[0], before_event_id=event.event_id)
                    source = row_facts(reads.connection, "device_files", source_id)
                    observer = None if source is None else row_facts(reads.connection, "actions", source["observer_action_id"])
                    if (observer is None or (details.get("device_id"), details.get("expected_driver_id"))
                            != (observer["device_id"], observer["driver_id"])):
                        raise EventValidationError("照片缓存绑定失败与原设备身份不同")
            if code == "device_binding_unavailable" and details.get("reason") in ("physical_identity_conflict", "physical_identity_mismatch"):
                from .preparation import PreparationReads
                from .physical_alias import source_physical_alias_conflict, verify_source_identity_mismatch
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("读取失败缺少机身冲突核对")
                copies = [copy for copy in context.state_rows.get("file_copies", {}).values()
                          if copy.get("delivery_id") == row.row_id]
                if len(copies) != 1:
                    raise EventValidationError("机身冲突失败缺少唯一原拷贝")
                source = _guard_facts(context, "device_files", copies[0]["source_device_file_id"])
                observer = _guard_facts(context, "actions", source["observer_action_id"])
                origin = _guard_facts(context, "actions", source["source_action_id"])
                delivery = _guard_facts(context, "deliveries", row.row_id)
                valid = (verify_source_identity_mismatch(context.preparation_reads.connection, source, observer, origin,
                                                         event.evidence.get("read_current_identity"), action_id=delivery["action_id"])
                         if details["reason"] == "physical_identity_mismatch"
                         else source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin,
                                                             action_id=delivery["action_id"]))
                if (details.get("device_id") != observer["device_id"]
                        or details.get("expected_driver_id") != observer["driver_id"]
                        or not valid):
                    raise EventValidationError("读取失败缺少其他逻辑设备的原责任")


def _require_publishable_copy(context, delivery_id: int) -> None:
    """发布前提：唯一交付拷贝已完成校验且字节完整保存。"""
    copies = [
        facts for facts in context.state_rows.get("file_copies", {}).values()
        if facts.get("delivery_id") == delivery_id
    ]
    if len(copies) != 1:
        raise EventValidationError("准备或发布必须对应唯一交付拷贝")
    copy = copies[0]
    error = publishable_copy_error(verification_state=copy.get("verification_state"),
                                   committed_bytes=copy.get("committed_bytes"),
                                   source_size=copy.get("source_size"))
    if error is not None:
        raise EventValidationError(error)


def _photo_cache_recopy_proof(connection, copy, attempt, *, require_reset=False):
    """从原 READ_END、实际摘要不符及已登记新轮，证明照片缓存交付未完成。"""
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.read_session import ReadEnd
    from camctl.history.validators import validate_event_structure
    from camctl.persistence.fact_reads import read_event

    if (copy is None or attempt is None or copy.get("source_intermediate_file_id") is None
            or copy.get("source_device_file_id") is not None or copy.get("delivery_id") is None
            or attempt.get("copy_round") is None or copy.get("round") != attempt["copy_round"] + 1):
        return None
    with closing(connection.execute(
        "SELECT a.driver_id,a.type,a.execution_spec_json,o.device_file_id,f.owner_action_id,o.source_action_id"
        " FROM deliveries d JOIN outputs o ON o.id=d.output_id JOIN actions a ON a.id=o.source_action_id"
        " JOIN intermediate_files f ON f.id=? WHERE d.id=?",
        (copy["source_intermediate_file_id"], copy["delivery_id"]))) as cursor:
        source = cursor.fetchone()
    if source is None:
        return None
    spec = source[2] if isinstance(source[2], Mapping) else parse_exact_json(source[2])
    if (source[0] != DRIVER_ID or source[1] != int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
            or not isinstance(spec, Mapping) or spec.get("output_storage") != "device"
            or source[3] is None or source[4] != source[5]):
        return None
    end_limit = attempt.get("result_event_id") or (1 << 63) - 1
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
        " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.round')=? ORDER BY id LIMIT 2",
        (_COPY_CHANGED_EVENT, _RECOPY_REASON, attempt["intent_event_id"], end_limit,
         copy["id"], copy["round"]))) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    recopy = read_event(connection, rows[0][0])
    if (validate_event_structure(recopy.event)[:2] != ("COPY_CHANGED", "RECOPY")
            or len(recopy.event.rows) != 1):
        raise ConsistencyError("照片缓存重拷缺少原完整事件")
    row = recopy.event.rows[0]
    before, after = row.before.values, row.after.values
    end_id = before.get("read_end_event_id")
    target_hash = before.get("target_sha256")
    if (row.table != "file_copies" or row.row_id != copy["id"] or not row.before.exists or not row.after.exists
            or before.get("round") != attempt["copy_round"] or after.get("round") != copy["round"]
            or before.get("committed_bytes") != copy["source_size"] or after.get("committed_bytes") != 0
            or before.get("verification_state") != int(_VERIFICATION.MISMATCHED)
            or after.get("verification_state") != int(_VERIFICATION.NOT_PERFORMED)
            or before.get("reset_state") != int(_RESET_STATE.READY)
            or after.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
            or before.get("recopies_used") != copy["round"] - 2
            or after.get("recopies_used") != copy["round"] - 1
            or type(end_id) is not int or not isinstance(target_hash, str)
            or not isinstance(copy.get("source_sha256"), str) or target_hash == copy["source_sha256"]):
        raise ConsistencyError("照片缓存重拷与实际摘要不符或原轮次不一致")
    end_record = read_event(connection, end_id)
    end_event = end_record.event
    document = end_event.evidence.get("read_end")
    if (validate_event_structure(end_event)[:2] != ("COPY_CHANGED", "READ_END")
            or end_record.transaction.last_event_id >= recopy.transaction.first_event_id
            or not isinstance(document, Mapping) or not isinstance(document.get("ticket"), Mapping)
            or document["ticket"] != {"run_id": attempt["run_id"], "attempt_no": attempt["attempt_no"],
                "operation": "read", "target_id": str(copy["id"]), "responsibility_key": f"read/{copy['id']}"}
            or document.get("copy_round") != attempt["copy_round"]
            or document.get("source_intermediate_file_id") != copy["source_intermediate_file_id"]
            or document.get("source_device_file_id") is not None
            or document.get("target_file_id") != copy["target_file_id"]
            or document.get("source_size") != copy["source_size"]
            or document.get("committed_bytes") != copy["source_size"]
            or document.get("reset_state") != int(_RESET_STATE.READY)
            or len(end_event.rows) != 1 or end_event.rows[0].row_id != copy["id"]
            or end_event.rows[0].table != "file_copies"
            or end_event.rows[0].after.values.get("read_end_event_id") != end_id):
        raise ConsistencyError("照片缓存重拷缺少原读取实际收场事实")
    actual_end = document.get("end")
    if (not isinstance(actual_end, Mapping) or set(actual_end) != {"stopped", "bytes_read", "error"}
            or actual_end["stopped"] is not True or actual_end["error"] is not None
            or type(actual_end["bytes_read"]) is not int or not 0 <= actual_end["bytes_read"] <= copy["source_size"]):
        raise ConsistencyError("照片缓存重拷的原读取结束不完整")
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
        " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.verification_state')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.target_sha256')=? ORDER BY id DESC LIMIT 1",
        (_COPY_CHANGED_EVENT, _VERIFY_REASON, end_id, recopy.event.event_id, copy["id"],
         int(_VERIFICATION.MISMATCHED), target_hash))) as cursor:
        mismatch = cursor.fetchone()
    if mismatch is None:
        raise ConsistencyError("照片缓存重拷缺少原实际摘要不符事件")
    verification = read_event(connection, mismatch[0])
    if (validate_event_structure(verification.event)[:2] != ("COPY_CHANGED", "VERIFY")
            or len(verification.event.rows) != 1
            or verification.event.rows[0].table != "file_copies"
            or verification.event.rows[0].row_id != copy["id"]
            or verification.event.rows[0].after.values.get("verification_state") != int(_VERIFICATION.MISMATCHED)
            or verification.event.rows[0].after.values.get("target_sha256") != target_hash
            or (verification.transaction.txn_id != recopy.transaction.txn_id
                and verification.transaction.last_event_id >= recopy.transaction.first_event_id)):
        raise ConsistencyError("照片缓存原摘要不符事件与重拷事务不一致")
    if require_reset:
        with closing(connection.execute(
            "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
            " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
            " AND json_extract(body_json,'$.rows[0].id')=? ORDER BY id LIMIT 2",
            (_COPY_CHANGED_EVENT, _RESET_REASON, recopy.transaction.last_event_id, end_limit,
             copy["id"]))) as cursor:
            resets = cursor.fetchall()
        if len(resets) != 1:
            raise ConsistencyError("照片缓存重拷缺少原目标重置事件")
        reset = read_event(connection, resets[0][0])
        if (validate_event_structure(reset.event)[:2] != ("COPY_CHANGED", "RESET")
                or len(reset.event.rows) != 1 or reset.event.rows[0].table != "file_copies"
                or reset.event.rows[0].row_id != copy["id"]
                or reset.event.rows[0].before.values.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
                or reset.event.rows[0].after.values.get("reset_state") != int(_RESET_STATE.READY)):
            raise ConsistencyError("照片缓存原目标重置事件不完整")
    return ReadEnd(**actual_end)


def _copy_round_chain_proven(
        connection, copy_id, initial_round, current_round,
        intent_event_id, result_event_id, source_size) -> bool:
    """流式证明 READ_FILE 意图到结果间的连续重拷轮次。

    每次只解码一条涉及目标拷贝的历史事件，并仅累计期望轮次；内存
    不随重拷次数增长。调用方分别在写命令和独立事件守卫中校验该链。
    """
    values = (copy_id, initial_round, current_round, intent_event_id, result_event_id)
    if any(type(value) is not int or value <= 0 for value in values):
        return False
    if type(source_size) is not int or source_size < 0:
        return False
    if (initial_round > current_round
            or intent_event_id >= result_event_id):
        return False

    expected_round = initial_round
    with closing(connection.execute(
        "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,"
        " h.clock_status,h.change_seq,h.body_json"
        " FROM history_events h"
        " WHERE h.id>? AND h.id<?"
        " AND EXISTS (SELECT 1 FROM json_each(h.body_json,'$.rows') AS r"
        "  WHERE json_extract(r.value,'$.table')='file_copies'"
        "  AND json_extract(r.value,'$.id')=?)"
        " ORDER BY h.id",
        (intent_event_id, result_event_id, copy_id),
    )) as cursor:
        for stored_event in cursor:
            event = decode_event_row(stored_event)
            copy_change = None
            for change in event.rows:
                if change.table == "file_copies" and change.row_id == copy_id:
                    if copy_change is not None:
                        return False
                    copy_change = change
            if copy_change is None:
                return False

            before, after = copy_change.before.values, copy_change.after.values
            has_before_round = "round" in before
            has_after_round = "round" in after
            if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RECOPY_REASON:
                if (not copy_change.before.exists or not copy_change.after.exists
                        or not has_before_round or not has_after_round
                        or before.get("round") != expected_round
                        or after.get("round") != expected_round + 1
                        or before.get("recopies_used") != expected_round - 1
                        or after.get("recopies_used") != expected_round
                        or before.get("committed_bytes", 0 if source_size == 0 else None) != source_size
                        or after.get("committed_bytes", 0 if source_size == 0 else None) != 0
                        or before.get("reset_state") != int(_RESET_STATE.READY)
                        or after.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
                        or before.get("verification_state") != int(_VERIFICATION.MISMATCHED)
                        or after.get("verification_state") != int(_VERIFICATION.NOT_PERFORMED)
                        or "target_sha256" not in before
                        or after.get("target_sha256") is not None):
                    return False
                expected_round += 1
                continue

            if (has_before_round != has_after_round
                    or (has_before_round and before["round"] != after["round"])):
                return False

    return expected_round == current_round


def _copy_guard(event, context) -> None:
    """拷贝建档、进度段、目标重置、校验与重拷守卫。"""
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _PHOTO_READ_START_REASON:
        from camctl.outputs.photo_read import (parse_photo_read_start, decide_photo_read_start,
                                                photo_read_attempt_matches)

        try:
            if len(event.rows) != 1 or event.rows[0].table != "file_copies":
                raise ValueError("照片读取长度只能修改一份拷贝")
            row = event.rows[0]
            request = parse_photo_read_start(event.evidence["photo_read_start"], event.occurred_at)
            if request.copy_id != row.row_id:
                raise ValueError("照片读取票据和目标拷贝不同")
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            source = None if copy is None else context.state_rows.get("device_files", {}).get(copy.get("source_device_file_id"))
            origin = None if source is None else context.state_rows.get("actions", {}).get(source.get("source_action_id"))
            run = context.state_rows.get("operation_runs", {}).get(request.ticket.run_id)
            attempts = [a for a in context.state_rows.get("operation_attempts", {}).values()
                        if a.get("run_id") == request.ticket.run_id
                        and a.get("attempt_no") == request.ticket.attempt_id]
            delivery = None if copy is None else context.state_rows.get("deliveries", {}).get(copy.get("delivery_id"))
            if (copy is None or source is None or origin is None or run is None or len(attempts) != 1
                    or delivery is None or context.photo_result_reads is None
                    or copy.get("source_intermediate_file_id") is not None
                    or copy.get("processing_id") is not None or copy.get("inspection_id") is not None
                    or read_action_spec(origin).get("photo_source_policy") != "deferred"
                    or not photo_read_attempt_matches(copy_id=row.row_id, copy=copy, source=source,
                        origin=origin, run=run, attempt=attempts[0], delivery=delivery,
                        ticket=request.ticket)):
                raise ValueError("照片读取长度缺少持槽的原读取尝试及按需来源")
            completion = source.get("completion_evidence_json")
            observation = completion.get("observation") if isinstance(completion, Mapping) else None
            context.photo_result_reads.verify_device_file(source["id"], observation,
                before_event_id=event.event_id, role=source["role"], size_bytes=None,
                check_size=True, sha256=None, check_sha=True)
            decision = decide_photo_read_start(previous_size=copy["source_size"],
                committed_bytes=copy["committed_bytes"],
                reset_pending=copy["reset_state"] == int(_RESET_STATE.RESET_PENDING),
                current_size=request.size_bytes)
            expected = _update("file_copies", row.row_id,
                {name: copy[name] for name in ("source_size", "committed_bytes", "reset_state")},
                {"source_size": decision.source_size, "committed_bytes": decision.committed_bytes,
                 "reset_state": int(_RESET_STATE.RESET_PENDING if decision.reset_pending else _RESET_STATE.READY)})
            if (not json_equal(row.before.values, expected.before.values)
                    or not json_equal(row.after.values, expected.after.values)):
                raise ValueError("照片读取长度或重置状态与原申请不符")
        except (ConsistencyError, ValueError, TypeError, KeyError) as error:
            raise EventValidationError(f"照片读取长度事件无效: {error}") from error
        return
    if (event.event_type == _COPY_CHANGED_EVENT
            and event.reason == _COPY_SOURCE_FALLBACK_REASON):
        if len(event.rows) != 1 or event.rows[0].table != "file_copies":
            raise EventValidationError("读取换源只能更新一份正式取回拷贝")
        row = event.rows[0]
        copy = context.state_rows.get("file_copies", {}).get(row.row_id)
        before, after = row.before.values, row.after.values
        source_columns = {
            "source_device_file_id", "source_intermediate_file_id"}
        if (copy is None or set(before) - {"read_end_event_id"} != source_columns
                or set(after) - {"read_end_event_id"} != source_columns
                or ("read_end_event_id" in after and after["read_end_event_id"] is not None)
                or (copy.get("read_end_event_id") is not None
                    and (before.get("read_end_event_id") != copy["read_end_event_id"]
                         or "read_end_event_id" not in after or after["read_end_event_id"] is not None))
                or copy.get("source_device_file_id") is not None
                or copy.get("source_intermediate_file_id") is None
                or not all(json_equal(copy.get(name), value)
                           for name, value in before.items())
                or before["source_device_file_id"] is not None
                or before["source_intermediate_file_id"] is None
                or after["source_device_file_id"] is None
                or after["source_intermediate_file_id"] is not None
                or copy.get("delivery_id") is None
                or copy.get("processing_id") is not None
                or copy.get("inspection_id") is not None):
            raise EventValidationError("读取换源必须是同一交付拷贝从检查副本单向回到设备源")

        delivery = context.state_rows.get("deliveries", {}).get(
            copy["delivery_id"])
        attempt_id = event.evidence.get("attempt_id")
        attempt = context.state_rows.get("operation_attempts", {}).get(attempt_id)
        run_rows = [
            facts for facts in context.state_rows.get("operation_runs", {}).values()
            if facts.get("copy_id") == row.row_id
            and facts.get("kind") == int(_RUN_KIND.READ_FILE)]
        if (delivery is None or len(run_rows) != 1 or attempt is None):
            raise EventValidationError("读取换源缺少原交付、尝试或 READ_FILE 流程")
        run = run_rows[0]
        if (attempt.get("run_id") != run.get("id")
                or attempt.get("attempt_no") != run.get("attempts_used")
                or attempt.get("status") != int(_ATTEMPT_STATUS.FAILED)
                or attempt.get("intent_event_id") is None
                or attempt.get("result_event_id") is None
                or not isinstance(attempt.get("error_json"), Mapping)
                or attempt["error_json"].get("code") != "local_read_failed"
                or not isinstance(attempt["error_json"].get("details"), Mapping)
                or run.get("responsibility_key") != f"read/{row.row_id}"
                or run.get("delivery_id") != delivery.get("id")
                or run.get("status") != int(_RUN_STATUS.ACTIVE)
                or run.get("retry_wait_required") != 1
                or run.get("attempts_used", 0) >= run.get("max_attempts_used", 0)):
            raise EventValidationError("读取换源必须沿用仍有余量的原本地失败读取预算")
        reads = context.current_reads
        details = attempt["error_json"]["details"]
        if details.get("source_failed") is not True:
            if (reads is None or details.get("photo_cache_checksum_mismatch") is not True
                    or copy.get("reset_state") != int(_RESET_STATE.READY)
                    or _photo_cache_recopy_proof(reads.connection, copy, attempt, require_reset=True) is None):
                raise EventValidationError("照片缓存换源缺少原摘要不符、读取收场及目标重置")
        if (reads is None or not _copy_round_chain_proven(
                reads.connection, row.row_id, attempt.get("copy_round"),
                copy.get("round"), attempt.get("intent_event_id"),
                attempt.get("result_event_id"), copy.get("source_size"))):
            raise EventValidationError("读取换源的拷贝轮次没有原 READ_FILE 意图到结果的连续重拷链")

        item_rows = [
            facts for facts in context.state_rows.get("obtain_items", {}).values()
            if facts.get("delivery_id") == delivery.get("id")]
        if len(item_rows) != 1:
            raise EventValidationError("读取换源的交付必须属于唯一正式取回项")
        item = item_rows[0]
        output = context.state_rows.get("outputs", {}).get(delivery.get("output_id"))
        source = context.state_rows.get("device_files", {}).get(
            after["source_device_file_id"])
        source_role = (None if output is None else {
            int(_OUTPUT_KIND.ORIGINAL): int(_DEVICE_FILE_ROLE.ORIGINAL),
            int(_OUTPUT_KIND.PREVIEW): int(_DEVICE_FILE_ROLE.PREVIEW),
        }.get(output.get("kind")))
        if (output is None or source is None
                or output.get("id") != item.get("output_id")
                or source_role is None
                or output.get("device_file_id") != source.get("id")
                or source.get("source_action_id") != output.get("source_action_id")
                or source.get("role") != source_role
                or source.get("completion_state") != int(_COMPLETION.COMPLETE)
                or source.get("presence_state") != int(_PRESENCE.PRESENT)
                or output.get("availability") != int(_AVAILABILITY.AVAILABLE)):
            raise EventValidationError("读取换源必须回到同一产物仍可读取的完整原片")

        original_action = context.state_rows.get("actions", {}).get(
            output.get("source_action_id"))
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        if (original_action is not None and original_action.get("driver_id") == DRIVER_ID
                and original_action.get("type") == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
                and original_action.get("execution_spec_json", {}).get("output_storage") == "device"):
            from camctl.persistence.repositories.photo_cache_reuse import read_bound_photo_cache
            if context.current_reads is None:
                raise EventValidationError("照片读取换源缺少独立原事务读取")
            try:
                bound = read_bound_photo_cache(output_id=output["id"], obtain_action_id=delivery["action_id"],
                    selection_id=item["selection_id"], owned=SimpleNamespace(connection=context.current_reads.connection))
            except (ValueError, TypeError, KeyError) as error:
                raise EventValidationError("照片读取换源的原来源或 H 无效") from error
            if (bound is None or bound.intermediate_file_id != before["source_intermediate_file_id"]
                    or bound.source_device_file_id != source["id"] or bound.size_bytes != copy["source_size"]
                    or bound.sha256 != copy["source_sha256"]):
                raise EventValidationError("照片读取换源必须回到 H 上固定的同一角色")
            return
        from camctl.devices.tasks import CaptureResultPolicy

        check_copies = [
            facts for facts in context.state_rows.get("file_copies", {}).values()
            if facts.get("target_file_id") == before["source_intermediate_file_id"]
            and facts.get("inspection_id") is not None]
        if len(check_copies) != 1:
            raise EventValidationError("读取换源的本地来源必须是唯一分段检查副本")
        check_copy = check_copies[0]
        inspection = context.state_rows.get("segment_inspections", {}).get(
            check_copy["inspection_id"])
        input_file = context.state_rows.get("intermediate_files", {}).get(
            before["source_intermediate_file_id"])
        if (inspection is None or input_file is None
                or inspection.get("action_id") != output.get("source_action_id")
                or inspection.get("source_device_file_id") != source.get("id")
                or inspection.get("state") != int(_SEGMENT_INSPECTION_STATE.COMPLETED)
                or check_copy.get("source_device_file_id") != source.get("id")
                or check_copy.get("verification_state") not in (
                    int(_VERIFICATION.MATCHED),
                    int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE))
                or input_file.get("owner_action_id") != output.get("source_action_id")
                or input_file.get("owner_delivery_id") is not None
                or input_file.get("purpose") != int(_PURPOSE.RECORDING_INPUT)
                or input_file.get("retention_state") != int(_RETENTION.REQUIRED)
                or input_file.get("cleanup_state") != int(_FILE_CLEANUP.NOT_NEEDED)
                or input_file.get("size_bytes") != copy.get("source_size")
                or input_file.get("sha256") != check_copy.get("target_sha256")):
            raise EventValidationError("读取换源的检查副本与固定 Action6 原片不一致")
        if (original_action is None
                or output.get("kind") != int(_OUTPUT_KIND.ORIGINAL)
                or original_action.get("type") not in (
                    int(_ACTION_TYPE.CAMERA_RECORD),
                    int(_ACTION_TYPE.CAMERA_TIMELAPSE))
                or original_action.get("execution_spec_json", {}).get("result_policy")
                != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
            raise EventValidationError("读取换源仅适用于 Action6 分段视频")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RESET_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("目标重置缺少当前拷贝事实")
            if copy.get("committed_bytes") != 0:
                raise EventValidationError("重置完成要求可靠进度已经归零")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _SEGMENT_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("进度段缺少当前拷贝事实")
            before = row.before.values.get("committed_bytes")
            after = row.after.values.get("committed_bytes")
            if not isinstance(after, int) or isinstance(after, bool) \
                    or not isinstance(before, int) or isinstance(before, bool):
                raise EventValidationError("进度段必须携带整数进度范围")
            if not segment_advances(before, after):
                raise EventValidationError("进度段必须推进可靠进度")
            if segment_exceeds_source(after, copy.get("source_size")):
                raise EventValidationError("进度段不能越过固定源长度")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _VERIFY_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("校验事实缺少当前拷贝事实")
            if copy.get("committed_bytes") != copy.get("source_size"):
                raise EventValidationError("校验要求全部字节可靠保存")
            after = row.after.values
            state = after.get("verification_state")
            source = after.get("source_sha256", copy.get("source_sha256"))
            target = after.get("target_sha256", copy.get("target_sha256"))
            if "slot_device_id" in after:
                from .read_end import load_read_end
                reads = context.read_end_reads
                if (reads is None or source is None or row.before.values.get("slot_device_id") != copy.get("slot_device_id")
                        or after["slot_device_id"] is not None
                        or load_read_end(row.row_id, SimpleNamespace(connection=reads.connection)) is None):
                    raise EventValidationError("源摘要与释放机会必须关联原完整读取结束")
            error = verification_result_error(state=state, source=source, target=target,
                                              error=after.get("verification_error_json"))
            if error is not None:
                raise EventValidationError(error)
            if (copy.get("inspection_id") is not None and state in (
                    int(_VERIFICATION.MATCHED), int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE))):
                before_file = context.state_rows.get("intermediate_files", {}).get(copy.get("target_file_id"))
                file = context.transaction_rows.get("intermediate_files", {}).get(copy.get("target_file_id"))
                if (before_file is None or before_file.get("size_bytes") is not None
                        or before_file.get("sha256") is not None
                        or file is None or file.get("size_bytes") != copy.get("source_size")
                        or file.get("sha256") != target):
                    raise EventValidationError("检查副本的校验和完整文件事实必须共同保存")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RECOPY_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("重拷登记缺少当前拷贝事实")
            before = row.before.values
            after = row.after.values
            if before.get("committed_bytes", copy.get("committed_bytes")) != copy.get("source_size"):
                raise EventValidationError("重拷登记要求全部字节已经可靠保存")
            if after.get("round") != before.get("round", 0) + 1 \
                    or after.get("recopies_used") != before.get("recopies_used", -1) + 1:
                raise EventValidationError("重拷登记必须恰好增加一轮并消耗一次额度")
            if after.get("committed_bytes", copy.get("committed_bytes")) != 0:
                raise EventValidationError("重拷登记必须把可靠进度归零")
            if after.get("target_sha256") is not None:
                raise EventValidationError("新一轮拷贝不能沿用旧目标摘要")
            if "read_end_event_id" in after and after["read_end_event_id"] is not None:
                raise EventValidationError("新一轮拷贝不能建立读取结束依据")
            if (copy.get("read_end_event_id") is not None
                    and (before.get("read_end_event_id") != copy["read_end_event_id"]
                         or "read_end_event_id" not in after or after["read_end_event_id"] is not None)):
                raise EventValidationError("新一轮拷贝不能沿用原读取结束依据")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _CONFIGURE_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            limit = row.after.values.get("max_recopies_used")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise EventValidationError("判定上限必须是非负整数")
        return
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != 1:
        return
    for row in event.rows:
        if row.table != "file_copies" or row.before.exists:
            raise EventValidationError("拷贝建档必须是创建行")
        values = row.after.values
        expected = {
            "round": 1,
            "recopies_used": 0,
            "committed_bytes": 0,
            "reset_state": int(_RESET_STATE.READY),
            "verification_state": int(_VERIFICATION.NOT_PERFORMED),
            "target_sha256": None,
            "verification_error_json": None,
        }
        for column, value in expected.items():
            if values.get(column) != value:
                raise EventValidationError(
                    f"新建拷贝的 {column} 必须是 {value!r}: {values.get(column)!r}"
                )
        if values.get("source_size") is None:
            source = context.state_rows.get("device_files", {}).get(values.get("source_device_file_id"))
            reads = context.photo_result_reads
            if (values.get("delivery_id") is None or values.get("source_intermediate_file_id") is not None
                    or source is None or reads is None or not _deferred_photo_source(reads.connection, source)):
                raise EventValidationError("只有按需照片交付可以暂时不知道源长度")
            completion = source.get("completion_evidence_json")
            observation = completion.get("observation") if isinstance(completion, Mapping) else None
            try:
                reads.verify_device_file(source["id"], observation, before_event_id=event.event_id,
                    role=source["role"], size_bytes=None, check_size=True, sha256=None, check_sha=True)
            except (ConsistencyError, ValueError, TypeError, KeyError) as error:
                raise EventValidationError(f"未知长度照片来源无效: {error}") from error
        if sum(values.get(name) is not None for name in
               ("delivery_id", "processing_id", "inspection_id")) != 1:
            raise EventValidationError("拷贝必须恰归属交付、录像处理或分段检查之一")
        if values.get("processing_id") is not None:
            processing = context.state_rows.get("recording_processing", {}).get(values["processing_id"])
            try:
                requirement = _internal_input_requirement(processing)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            if requirement is not None:
                raise EventValidationError(
                    "当前处理不具备新的内部输入需求，不能创建输入拷贝")
            parent = processing
        elif values.get("inspection_id") is not None:
            parent = context.state_rows.get("segment_inspections", {}).get(values["inspection_id"])
            member = (context.state_rows.get("segment_members", {}).get(parent.get("member_id"))
                      if parent is not None else None)
            source = context.state_rows.get("device_files", {}).get(values.get("source_device_file_id"))
            if (parent is None or member is None or source is None
                    or parent.get("state") != int(_SEGMENT_INSPECTION_STATE.PENDING)
                    or parent.get("source_device_file_id") != values.get("source_device_file_id")
                    or member.get("file_id") != values.get("source_device_file_id")
                    or source.get("source_action_id") != parent.get("action_id")):
                raise EventValidationError("检查拷贝必须关联 PENDING 检查及其固定原片")
        else:
            parent = context.association_rows.get("deliveries", {}).get(values["delivery_id"])
        if parent is None:
            raise EventValidationError("新建拷贝缺少发起责任的固定关联")
        if values.get("delivery_id") is not None and values.get("source_intermediate_file_id") is not None:
            output = context.state_rows.get("outputs", {}).get(parent.get("output_id"))
            source_action = (None if output is None else
                context.state_rows.get("actions", {}).get(output.get("source_action_id")))
            if output is None or (output.get("device_file_id") is not None and source_action is None):
                raise EventValidationError("本地复用交付缺少原产物或来源动作")
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            if (output.get("device_file_id") is not None
                    and source_action.get("driver_id") == DRIVER_ID
                    and source_action.get("execution_spec_json", {}).get("output_storage") == "device"):
                members = [item for item in context.association_rows.get("obtain_items", {}).values()
                           if item.get("delivery_id") == values["delivery_id"]]
                if len(members) != 1 or context.current_reads is None:
                    raise EventValidationError("照片缓存交付缺少原取回项及独立事务读取")
                from camctl.persistence.repositories.photo_cache_reuse import read_bound_photo_cache
                try:
                    bound = read_bound_photo_cache(output_id=output["id"],
                        obtain_action_id=parent["action_id"], selection_id=members[0]["selection_id"],
                        owned=SimpleNamespace(connection=context.current_reads.connection))
                except (TypeError, ValueError, KeyError) as error:
                    raise EventValidationError("照片缓存交付的原来源或 H 无效") from error
                if (bound is None or bound.intermediate_file_id != values["source_intermediate_file_id"]
                        or bound.size_bytes != values["source_size"] or bound.sha256 != values["source_sha256"]):
                    raise EventValidationError("照片缓存交付不属于 H 时固定等待者的原角色")
        _guard_read_owner(event, context, parent.get("action_id"))


def _copy_links_guard(event, context) -> None:
    """拷贝关联守卫：每份新拷贝与唯一 READ_FILE 流程共同保存。"""
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != 1:
        return
    runs = context.association_rows.get("operation_runs", {})
    for row in event.rows:
        if row.table != "file_copies" or row.before.exists:
            continue
        copy_id = row.row_id
        values = row.after.values
        matches = [
            facts
            for facts in runs.values()
            if facts.get("kind") == int(_RUN_KIND.READ_FILE)
            and facts.get("copy_id") == copy_id
        ]
        if len(matches) != 1:
            raise EventValidationError(
                f"每份新拷贝必须恰有一条 READ_FILE 流程: copy {copy_id}"
                f" 流程 {len(matches)} 条"
            )
        run = matches[0]
        if run.get("responsibility_key") != f"read/{copy_id}":
            raise EventValidationError("READ_FILE 流程责任键与拷贝不一致")
        if run.get("delivery_id") != values.get("delivery_id"):
            raise EventValidationError("READ_FILE 流程与拷贝的交付归属不一致")


def _validate_saved_error(table, code, details) -> None:
    """错误详情使用公共登记的格式，不在报告生成时补救。"""
    if code is None:
        if details is not None:
            raise EventValidationError("没有错误码时不能保存错误详情")
        return
    try:
        name, _ = registered_error_spec(f"item_error_ids.{table}", code)
        validate_error_details(name, details)
    except (TypeError, ValueError) as error:
        raise EventValidationError(str(error)) from error


def _validate_obtain_error(values, source_id) -> None:
    details = values.get("error_details_json")
    _validate_saved_error("obtain_items", values.get("error_code"), details)
    if details is None:
        return
    identities = {
        "output_id": values.get("output_id"),
        "requested_output_id": values.get("requested_output_id"),
        "original_output_id": values.get("original_output_id"),
        "source_action_instance_id": source_id,
    }
    for name, identity in identities.items():
        if name in details and (identity is None or details[name] != str(identity)):
            raise EventValidationError(f"取回错误详情与原目标不一致: {name}")


def _obtain_member_guard(event, context) -> None:
    """核实或拒绝只推进事件发生前已固定且仍有普通资格的成员。"""
    if event.event_type != _READ_PERMISSION_EVENT or event.reason not in (
        _REJECT_REASON, _RESOLVE_REASON,
    ):
        return

    updates = [row for row in event.rows if row.table == "obtain_items" and row.before.exists]
    if len(updates) != 1:
        raise EventValidationError("取回成员核实或拒绝必须恰好推进一条已有项")
    row = updates[0]
    item = _required_current_facts(context, "obtain_items", row.row_id)
    for identity in _known_output_ids(item):
        _required_current_facts(context, "outputs", identity)
    selection = _required_current_facts(context, "obtain_source_selections", item.get("selection_id"))
    dependency = _required_current_facts(context, "action_dependencies", selection.get("dependency_id"))
    owner = _required_current_facts(context, "actions", dependency.get("action_id"))
    source = _required_current_facts(context, "actions", dependency.get("depends_on_action_id"))
    from camctl.outputs.sources import obtain_member_verification_error, obtain_source_ready
    if not obtain_source_ready(selection=selection, owner=owner, source=source):
        raise EventValidationError("取回成员要求来源拍摄已终态、选择已固定且无来源级错误，取回仍在执行中并未取消")
    error = obtain_member_verification_error(item)
    if error is not None:
        raise EventValidationError(error)
    unresolved = item["status"] == int(_ITEM_STATUS.UNRESOLVED)
    explicit = item.get("basis") == int(_ITEM_BASIS.EXPLICIT)
    if event.reason == _REJECT_REASON:
        _validate_obtain_error({**item, **row.after.values}, dependency["depends_on_action_id"])
        code = row.after.values.get("error_code")
        requested = item.get("requested_output_id")
        if code == _OUTPUT_NOT_FOUND_CODE:
            if context.complete_rows("outputs", "id", requested):
                raise EventValidationError("当前查询已有请求产物，不能保存为不存在")
        elif code == _OUTPUT_SOURCE_MISMATCH_CODE:
            target = _required_current_facts(context, "outputs", requested)
            target_source = target.get("source_action_id")
            if (not is_json_integer(target_source) or target_source <= 0
                    or target_source == dependency["depends_on_action_id"]):
                raise EventValidationError("来源不匹配必须由当前其他来源产物证明")
    if event.reason == _RESOLVE_REASON:
        if not unresolved:
            raise EventValidationError("显式核实只能推进原 UNRESOLVED 项")
        output_id = row.after.values.get("output_id")
    else:
        output_id = row.after.values.get("output_id", item.get("output_id"))
        if "output_id" in row.after.values and (not unresolved or not explicit):
            raise EventValidationError(
                "只有显式未解析项可以在拒绝时保存已确认的产物关联"
            )
    if output_id is None:
        if event.reason == _RESOLVE_REASON or not unresolved:
            raise EventValidationError("已选中或核实成功的成员必须关联真实产物")
        return
    output = _required_current_facts(context, "outputs", output_id)
    if output.get("source_action_id") != dependency.get("depends_on_action_id"):
        raise EventValidationError("取回成员的真实产物必须属于固定来源")
    if explicit and output_id != item["requested_output_id"]:
        raise EventValidationError("显式成员的真实产物必须等于原请求 ID")
    if event.reason == _REJECT_REASON and row.after.values.get("error_code") == item_error_id(
            "obtain_items", "device_binding_unavailable"):
        file_id = output.get("device_file_id")
        if file_id is None:
            raise EventValidationError("主机产物不能保存设备绑定失败")
        file = _required_current_facts(context, "device_files", file_id)
        observer = _required_current_facts(context, "actions", file["observer_action_id"])
        details = row.after.values["error_details_json"]
        if (details.get("device_id"), details.get("expected_driver_id")) != (
                observer["device_id"], observer["driver_id"]):
            raise EventValidationError("取回绑定失败必须指向原文件观察者")


def _slot_run(context, copy_id: int) -> dict[str, Any]:
    matches = [
        facts
        for facts in context.state_rows.get("operation_runs", {}).values()
        if facts.get("copy_id") == copy_id
    ]
    if len(matches) != 1:
        raise EventValidationError("机会变化必须提供拷贝的唯一 READ_FILE 流程事实")
    run = matches[0]
    if run.get("kind") != int(_RUN_KIND.READ_FILE):
        raise EventValidationError("机会变化关联的流程不是 READ_FILE")
    return run


def _has_source_fallback_retry_proof(connection, copy, attempt) -> bool:
    """核实原失败尝试已有指向同一拷贝和设备文件的换源事件。"""
    if (connection is None or not is_json_integer(attempt.get("id"))
            or not is_json_integer(attempt.get("result_event_id"))
            or not is_json_integer(copy.get("id"))
            or not is_json_integer(copy.get("source_device_file_id"))):
        return False
    from camctl.persistence.retry_wait_structure import verify_retry_wait_index
    try:
        verify_retry_wait_index(connection, "history_events_row")
    except ValueError as error:
        raise ConsistencyError(str(error)) from error
    with closing(connection.execute(
        "SELECT 1 FROM history_events e"
        " WHERE e.event_type=? AND json_extract(e.body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(e.body_json,'$.rows[0].id')=? AND e.id>?"
        " AND json_extract(e.body_json,'$.reason')=?"
        " AND json_extract(e.body_json,'$.evidence.attempt_id')=?"
        " AND json_array_length(e.body_json,'$.rows')=1"
        " AND json_extract(e.body_json,'$.rows[0].before.values.source_device_file_id') IS NULL"
        " AND json_extract(e.body_json,'$.rows[0].before.values.source_intermediate_file_id') IS NOT NULL"
        " AND json_extract(e.body_json,'$.rows[0].after.values.source_device_file_id')=?"
        " AND json_extract(e.body_json,'$.rows[0].after.values.source_intermediate_file_id') IS NULL"
        " LIMIT 1",
        (_COPY_CHANGED_EVENT, copy["id"], attempt["result_event_id"], _COPY_SOURCE_FALLBACK_REASON,
         attempt["id"], copy["source_device_file_id"]),
    )) as cursor:
        return cursor.fetchone() is not None


def _source_fallback_retry_attempt(copy, run, attempts, connection):
    """返回有连续重拷链和原换源事件证明的等待中本地源失败尝试。"""
    if (run.get("status") != int(_RUN_STATUS.ACTIVE)
            or run.get("retry_wait_required") != 1
            or run.get("attempts_used", 0) < 1
            or copy.get("source_device_file_id") is None
            or copy.get("source_intermediate_file_id") is not None):
        return None
    matches = [
        attempt for attempt in attempts.values()
        if attempt.get("run_id") == run.get("id")
        and attempt.get("attempt_no") == run.get("attempts_used")
    ]
    if len(matches) != 1:
        return None
    attempt = matches[0]
    if (attempt.get("status") != int(_ATTEMPT_STATUS.FAILED)
            or attempt.get("intent_event_id") is None
            or attempt.get("result_event_id") is None):
        return None
    error = attempt.get("error_json")
    if isinstance(error, str):
        error = parse_exact_json(error)
    if not isinstance(error, Mapping):
        return None
    details = error.get("details")
    if (error.get("code") != "local_read_failed"
            or error.get("stage") != "source_read"
            or not isinstance(details, Mapping)
            or (details.get("source_failed") is not True
                and details.get("photo_cache_checksum_mismatch") is not True)):
        return None
    if not _copy_round_chain_proven(
            connection, copy.get("id"), attempt.get("copy_round"),
            copy.get("round"), attempt.get("intent_event_id"),
            attempt.get("result_event_id"), copy.get("source_size")):
        return None
    if not _has_source_fallback_retry_proof(connection, copy, attempt):
        return None
    return attempt


def _slot_owner_action(context, copy) -> dict[str, Any]:
    action_id = None
    if copy.get("delivery_id") is not None:
        action_id = context.state_rows.get("deliveries", {}).get(
            copy["delivery_id"], {}).get("action_id")
    elif copy.get("processing_id") is not None:
        action_id = context.state_rows.get("recording_processing", {}).get(
            copy["processing_id"], {}).get("action_id")
    elif copy.get("inspection_id") is not None:
        action_id = context.state_rows.get("segment_inspections", {}).get(
            copy["inspection_id"], {}).get("action_id")
    if not is_json_integer(action_id):
        raise EventValidationError("机会授予缺少发起动作的固定关联")
    return _guard_facts(context, "actions", action_id)


def _read_slot_guard(event, context) -> None:
    """机会变化守卫：授予核对绑定与当前资格，释放核对读取及重试结束。"""
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != _SLOT_REASON:
        return
    for row in event.rows:
        if row.table != "file_copies":
            raise EventValidationError("机会变化必须作用于拷贝行")
        if set(row.after.values) != {"slot_device_id"}:
            raise EventValidationError("机会变化只保存归属列")
        copy_id = row.row_id
        copy = _guard_facts(context, "file_copies", copy_id)
        run = _slot_run(context, copy_id)
        source_id = copy.get("source_device_file_id")
        if source_id is None:
            raise EventValidationError("主机源拷贝不能占用相机读取机会")
        source = _guard_facts(context, "device_files", source_id)
        observer = _guard_facts(context, "actions", source["observer_action_id"])
        origin = _guard_facts(context, "actions", source["source_action_id"])
        binding = (observer["device_id"], observer["driver_id"])
        if (not all(isinstance(value, str) and value for value in binding)
                or binding != (origin["device_id"], origin["driver_id"])):
            raise EventValidationError("读取源观察者与可靠来源的原设备绑定不一致")
        after_slot = row.after.values["slot_device_id"]
        if after_slot is not None:
            from .file_batch_reads import FileBatchReads
            action = _slot_owner_action(context, copy)
            if (type(context.file_batch_reads) is not FileBatchReads
                    or not context.file_batch_reads.permits(action["id"], binding[0],
                        require_owned=action["type"] == int(_ACTION_TYPE.OBTAIN_ACTION_OUTPUTS))):
                raise EventValidationError("读取机会未持有原动作的同机文件批次")
            if copy.get("read_end_event_id") is not None:
                raise EventValidationError("当前轮读取已经结束，不能再次授予相机机会")
            before_slot = row.before.values.get("slot_device_id") if row.before.exists else None
            if before_slot is not None:
                raise EventValidationError("授予必须从空闲机会开始")
            if after_slot != binding[0]:
                raise EventValidationError("授予的设备必须等于原来源设备绑定")
            from .preparation import PreparationReads
            if (type(context.preparation_reads) is not PreparationReads
                    or context.preparation_reads.device_start_in_flight(binding[0])):
                raise EventValidationError("读取机会授予缺少同设备启动收场依据")
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            identity = None
            if binding[1] == DRIVER_ID:
                from .physical_alias import source_physical_alias_conflict, source_physical_identity
                if source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin):
                    raise EventValidationError("读取机会越过了其他逻辑设备的机身责任")
                identity = source_physical_identity(context.preparation_reads.connection, source, observer, origin)
                from .capture_selection import CaptureSelectionInput, read_capture_priority
                try:
                    selection = CaptureSelectionInput.from_document(event.evidence.get("capture_selection"))
                    selected = read_capture_priority(context.preparation_reads.connection, selection,
                        context.transaction.first_event_id - 1, device_id=binding[0], identity=identity,
                        occurred_at=event.occurred_at)
                except (ConsistencyError, ValueError) as error:
                    raise EventValidationError(f"读取机会缺少原拍摄选择资格: {error}") from error
                if selected is not None:
                    raise EventValidationError("读取机会越过了已选中的拍摄")
            if context.preparation_reads.availability_in_flight(binding[0], identity):
                raise EventValidationError("读取机会越过了尚未收场的设备检查")
            if run["status"] in _RUN_TERMINAL:
                raise EventValidationError("读取责任已结束，不能再授予机会")
            if run["retry_wait_required"] != 0:
                attempts = context.complete_rows(
                    "operation_attempts", "run_id", run["id"])
                reads = context.current_reads
                if (reads is None or _source_fallback_retry_attempt(
                        copy, run, attempts, reads.connection) is None):
                    raise EventValidationError("重试等待期间不授予新的读取机会")
            action = _slot_owner_action(context, copy)
            try:
                if not _read_owner_eligible(action):
                    raise EventValidationError("机会授予要求发起动作执行中且未取消")
                if not _read_is_due(action, event.occurred_at):
                    raise EventValidationError("机会授予要求发起动作已到计划时间")
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            continue
        if not row.before.exists or row.before.values.get("slot_device_id") is None:
            raise EventValidationError("释放要求当前持有机会")
        if row.before.values["slot_device_id"] != binding[0]:
            raise EventValidationError("释放的机会必须属于原来源设备")
        from .retry_wait_reads import RetryWaitReads
        reads = context.retry_wait_reads
        if type(reads) is not RetryWaitReads or not reads.no_unsettled_attempts(run["id"], context.validated_events):
            raise EventValidationError("实际读取未结束，不能释放机会")
        if copy.get("processing_id") is not None:
            action = _slot_owner_action(context, copy)
            if _canceled_binding_processing_settled(reads.connection, copy, run, action) is False:
                raise EventValidationError("取消处理的原实际调用未全部收场")
        if run["retry_wait_required"] != 0:
            raise EventValidationError("重试等待期间保留机会")
        if run["status"] not in _RUN_TERMINAL and (
                copy["source_size"] is None or copy["committed_bytes"] < copy["source_size"]):
            raise EventValidationError("源内容尚未读完，机会保留")


def _read_permission_guard(event, context) -> None:
    """授予推进一条 SELECTED 项；拒绝保存一条未建档项的最终错误。"""
    if event.event_type == _READ_PERMISSION_EVENT and event.reason == _GRANT_REASON:
        updates = [
            row
            for row in event.rows
            if row.table == "obtain_items" and row.before.exists
        ]
        if len(updates) != 1:
            raise EventValidationError("资格授予必须恰好更新一条取回项")
        row = updates[0]
        before = row.before.values
        after = row.after.values
        if (
            before.get("status") != int(_ITEM_STATUS.SELECTED)
            or before.get("source_dependency") != 0
            or before.get("delivery_id") is not None
        ):
            raise EventValidationError("只有未依赖源的 SELECTED 项能被授予")
        if (
            after.get("status") != int(_ITEM_STATUS.DELIVERY_CREATED)
            or after.get("source_dependency") != 1
            or after.get("delivery_id") is None
        ):
            raise EventValidationError("授予必须建立源依赖并回填交付")
        delivery_id = after.get("delivery_id")
        delivery = context.state_rows.get("deliveries", {}).get(delivery_id)
        if delivery is None or delivery.get("status") != int(_DELIVERY_STATUS.PENDING):
            raise EventValidationError("授予回填的交付必须已在同事务建档")
        item = _required_current_facts(context, "obtain_items", row.row_id)
        for identity in _known_output_ids(item):
            _required_current_facts(context, "outputs", identity)
        selection = _required_current_facts(context, "obtain_source_selections", item.get("selection_id"))
        dependency = _required_current_facts(context, "action_dependencies", selection.get("dependency_id"))
        if (delivery.get("action_id") != dependency.get("action_id")
                or delivery.get("output_id") != item.get("output_id")):
            raise EventValidationError("授予回填的交付必须属于原取回动作及同一产物")
        _guard_read_owner(event, context, dependency.get("action_id"))
        try:
            if has_product_predecessor(_CurrentProductReads(context), dependency["action_id"],
                                       item["output_id"], event.occurred_at):
                raise EventValidationError("较早的合格产物候选尚未取得资格")
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
    elif event.event_type == _READ_PERMISSION_EVENT and event.reason == _REJECT_REASON:
        updates = [
            row
            for row in event.rows
            if row.table == "obtain_items" and row.before.exists
        ]
        if len(updates) != 1:
            raise EventValidationError("逐项拒绝必须恰好更新一条取回项")
        after = updates[0].after.values
        if (
            after.get("status") != int(_ITEM_STATUS.FAILED)
            or after.get("error_code") is None
            or after.get("error_details_json") is None
        ):
            raise EventValidationError("逐项拒绝必须保存最终失败及错误")
        details = after.get("error_details_json")
        if isinstance(details, Mapping) and details.get("reason") in ("physical_identity_conflict", "physical_identity_mismatch"):
            from .preparation import PreparationReads
            from .physical_alias import source_physical_alias_conflict, verify_source_identity_mismatch
            if type(context.preparation_reads) is not PreparationReads:
                raise EventValidationError("读取机身冲突缺少原连接事实")
            item = _guard_facts(context, "obtain_items", updates[0].row_id)
            output = _guard_facts(context, "outputs", item["output_id"])
            source = _guard_facts(context, "device_files", output["device_file_id"])
            observer = _guard_facts(context, "actions", source["observer_action_id"])
            origin = _guard_facts(context, "actions", source["source_action_id"])
            selection = _guard_facts(context, "obtain_source_selections", item["selection_id"])
            dependency = _guard_facts(context, "action_dependencies", selection["dependency_id"])
            valid = (verify_source_identity_mismatch(context.preparation_reads.connection, source, observer, origin,
                                                     event.evidence.get("read_current_identity"), action_id=dependency["action_id"])
                     if details["reason"] == "physical_identity_mismatch"
                     else source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin,
                                                         action_id=dependency["action_id"]))
            if (details.get("device_id") != observer["device_id"]
                    or details.get("expected_driver_id") != observer["driver_id"]
                    or not valid):
                raise EventValidationError("读取机身冲突缺少未完成原责任")
    elif event.event_type == _READ_PERMISSION_EVENT and event.reason == _RELEASE_REASON:
        for row in event.rows:
            if row.table != "obtain_items" or not row.before.exists:
                continue
            if row.before.values.get("source_dependency") != 1 \
                    or row.after.values.get("source_dependency") != 0:
                raise EventValidationError("解除源依赖必须从有效依赖翻转为解除")
            item = context.state_rows.get("obtain_items", {}).get(row.row_id)
            if item is None:
                raise EventValidationError("解除源依赖缺少取回项事实")
            delivery_id = item.get("delivery_id")
            delivery = context.state_rows.get("deliveries", {}).get(delivery_id)
            if delivery_id is None or delivery is None:
                raise EventValidationError("解除源依赖缺少交付事实")
            copies = [
                facts for facts in context.state_rows.get("file_copies", {}).values()
                if facts.get("delivery_id") == delivery_id
            ]
            if len(copies) != 1:
                raise EventValidationError("解除源依赖必须对应唯一交付拷贝")
            copy = copies[0]
            if delivery.get("status") in (6, 7):
                runs = context.complete_rows("operation_runs", "copy_id", copy["id"])
                if not runs or any(run.get("status") not in _RUN_TERMINAL
                                   or run.get("retry_wait_required") != 0 for run in runs.values()):
                    raise EventValidationError("最终失败或取消后解除源保护要求读取及后续重试已结束")
                for run in runs.values():
                    from .retry_wait_reads import RetryWaitReads
                    reads = context.retry_wait_reads
                    if type(reads) is not RetryWaitReads or not reads.no_unsettled_attempts(run["id"], context.validated_events):
                        raise EventValidationError("读取没有可靠结束结果，不能解除源保护")
                continue
            if copy.get("verification_state") not in (
                int(_VERIFICATION.MATCHED),
                int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
            ):
                raise EventValidationError("副本可靠准备前不能解除源保护")
            if copy.get("committed_bytes") != copy.get("source_size"):
                raise EventValidationError("副本字节未完整保存前不能解除源保护")
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 1:
        for row in event.rows:
            if row.table != "deliveries" or row.before.exists:
                continue
            items = context.state_rows.get("obtain_items", {})
            if not items:
                raise EventValidationError("交付建档必须提供待授予的取回项事实")


def _require_processing_error_document(name: str, document) -> None:
    """协议 error 结构的处理诊断校验；三键完整。"""
    if not isinstance(document, Mapping):
        raise EventValidationError(f"{name} 必须是结构化对象")
    for key in ("code", "stage"):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise EventValidationError(f"{name}.{key} 必须是非空文本")
    if not isinstance(document.get("details"), Mapping):
        raise EventValidationError(f"{name}.details 必须是对象")


def _is_precise_non_negative(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    number = value if isinstance(value, Decimal) else Decimal(value)
    return number.is_finite() and number >= 0


def _validate_check_basis(basis) -> None:
    """检查依据结构校验；未知成员省略，不默认为零。"""
    if not isinstance(basis, Mapping):
        raise EventValidationError("检查依据必须是结构化对象")
    reason = basis.get("reason")
    if isinstance(reason, bool) or not isinstance(reason, int) or reason not in (1, 2, 3):
        raise EventValidationError(f"检查依据来源不在登记范围: {reason!r}")
    target = basis.get("target_duration_ms")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise EventValidationError(f"检查依据必须保存正整数目标时长: {target!r}")
    elapsed = basis.get("control_elapsed_ns")
    if elapsed is not None and (
            isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0):
        raise EventValidationError(f"控制耗时必须是非负整数: {elapsed!r}")
    evidence = basis.get("continuity_evidence")
    if evidence is not None:
        if not isinstance(evidence, list) or not evidence or any(
                not isinstance(item, str) or not item for item in evidence):
            raise EventValidationError(f"连续性证据必须是非空文本列表: {evidence!r}")
    if "threshold_s" in basis:
        threshold = basis["threshold_s"]
        if (reason != 2 or not _is_precise_non_negative(threshold)
                or Decimal(threshold) < Decimal(target) / 1000):
            raise EventValidationError("检查门槛须为不低于目标时长的精确秒数")


def _validate_repair_basis(basis) -> None:
    """修复依据结构校验；门槛秒数随比较理由必填或省略。"""
    if not isinstance(basis, Mapping):
        raise EventValidationError("修复依据必须是结构化对象")
    reason = basis.get("reason")
    if isinstance(reason, bool) or not isinstance(reason, int) or reason not in (1, 2, 3, 4):
        raise EventValidationError(f"修复依据来源不在登记范围: {reason!r}")
    target = basis.get("target_duration_ms")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise EventValidationError(f"修复依据必须保存正整数目标时长: {target!r}")
    threshold = basis.get("threshold_s")
    if reason in (1, 2):
        if not _is_precise_non_negative(threshold):
            raise EventValidationError(f"门槛比较理由必须提供精确门槛秒数: {threshold!r}")
    elif threshold is not None:
        raise EventValidationError("该修复理由不适用门槛秒数，应省略")
    for key in ("actual_duration_s",):
        value = basis.get(key)
        if value is not None and not _is_precise_non_negative(value):
            raise EventValidationError(f"{key} 必须是非负精确数值: {value!r}")
    elapsed = basis.get("control_elapsed_ns")
    if elapsed is not None and (
            isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0):
        raise EventValidationError(f"控制耗时必须是非负整数: {elapsed!r}")


_MEDIA_STATUS_BY_STATE = {2: "running", 3: "completed", 4: "failed", 5: "unconfirmed"}


def _validate_media_document(document, check_state: int) -> None:
    """公共 media 结构与保存阶段的一致性校验。"""
    if not isinstance(document, Mapping):
        raise EventValidationError("媒体观察必须是结构化对象")
    if document.get("check_status") != _MEDIA_STATUS_BY_STATE.get(check_state):
        raise EventValidationError(
            f"媒体观察阶段与保存状态不一致: {document.get('check_status')!r}"
            f" / {check_state!r}")
    duration = document.get("duration")
    if not isinstance(duration, Mapping):
        raise EventValidationError("媒体时长必须是结构化对象")
    duration_status = duration.get("status")
    if duration_status == "available":
        if not _is_precise_non_negative(duration.get("seconds")):
            raise EventValidationError("可用时长必须保存非负精确秒数")
    elif duration_status != "unknown":
        raise EventValidationError(f"媒体时长状态不在允许范围: {duration_status!r}")
    if check_state == 2:
        if duration_status == "available" or "error" in document or "issues" in document:
            raise EventValidationError("检查进行中不能携带时长、错误或问题结论")
    if check_state == 3:
        if duration_status != "available":
            raise EventValidationError("检查完成必须保存可靠视频时长")
        if "error" in document:
            raise EventValidationError("检查完成不能同时携带错误")
    if check_state in (4, 5):
        error = document.get("error")
        if error is None:
            raise EventValidationError("检查失败或未确认必须保存实际错误")
        _require_processing_error_document("媒体错误", error)
    issues = document.get("issues")
    if issues is not None:
        if not isinstance(issues, list):
            raise EventValidationError(f"媒体问题必须是列表: {issues!r}")
        for issue in issues:
            _require_processing_error_document("媒体问题", issue)


def _single_processing_row(event, context):
    """取本事件唯一的处理行及其当前事实。"""
    rows = [row for row in event.rows if row.table == "recording_processing"]
    if len(rows) != 1:
        raise EventValidationError("处理事件必须恰好更新一条处理记录")
    row = rows[0]
    processing = context.state_rows.get("recording_processing", {}).get(row.row_id)
    if processing is None:
        raise EventValidationError("处理事件缺少当前处理事实")
    return row, processing


def _recording_source_guard(event, context) -> None:
    """RECORDING_DECIDED.SOURCE：首次关联的原片必须可靠且属于本动作。"""
    row, processing = _single_processing_row(event, context)
    file_id = row.after.values.get("source_device_file_id")
    if row.before.values.get("source_device_file_id") is not None:
        raise EventValidationError("可靠原片只能首次关联")
    if not isinstance(file_id, int) or isinstance(file_id, bool):
        raise EventValidationError(f"原片身份必须是整数: {file_id!r}")
    file = context.state_rows.get("device_files", {}).get(file_id)
    if file is None:
        raise EventValidationError("关联原片缺少当前文件事实")
    if file.get("role") != int(_DEVICE_FILE_ROLE.ORIGINAL):
        raise EventValidationError(f"关联文件不是原片角色: {file.get('role')!r}")
    if file.get("completion_state") != int(_DEVICE_FILE_COMPLETION.COMPLETE):
        raise EventValidationError("关联原片尚未确认写完")
    if file.get("source_action_id") != processing.get("action_id"):
        raise EventValidationError("关联原片属于其他动作")


def _require_unread_check_input(action, processing, copies, action_id, old_basis):
    """按独立读取的原始事实判断旧检查是否仍可补门槛。"""
    if (action["status"] != 2 or action["cancel_requested"] != 0
            or processing is None or processing["action_id"] != action_id
            or processing["check_decision"] != 3 or processing["check_state"] != 1
            or processing["repair_state"] != 1
            or not json_equal(processing["check_basis_json"], old_basis)):
        raise ConsistencyError("原动作或检查处理已不具备门槛补写资格")
    if len(copies) > 1:
        raise ConsistencyError("原处理存在多个输入副本")
    if not copies:
        return
    copy, target, runs = copies[0]
    if (copy["delivery_id"] is not None or copy["inspection_id"] is not None
            or copy["source_device_file_id"] != processing["source_device_file_id"]
            or copy["source_intermediate_file_id"] is not None
            or copy["round"] != 1 or copy["recopies_used"] != 0
            or copy["committed_bytes"] != 0 or copy["reset_state"] != 1
            or copy["read_end_event_id"] is not None or copy["verification_state"] != 1
            or copy["target_sha256"] is not None or copy["verification_error_json"] is not None
            or target is None or target["owner_action_id"] != action_id
            or target["owner_delivery_id"] is not None or target["purpose"] != 2
            or target["size_bytes"] is not None or target["sha256"] is not None):
        raise ConsistencyError("输入副本已有读取、校验或重拷事实")
    if (len(runs) != 1 or runs[0][1] != action_id or runs[0][2] is not None
            or runs[0][3] != 1 or runs[0][4] != 0 or runs[0][5]):
        raise ConsistencyError("输入副本的读取流程已有尝试或状态不符")


def _decided_processing_guard(event, context) -> None:
    """RECORDING_DECIDED 检查与修复决定：一次固定、依据完整。"""
    if event.reason == _DECIDED_CHECK_REASON:
        row, processing = _single_processing_row(event, context)
        before, after = row.before.values, row.after.values
        _validate_check_basis(after.get("check_basis_json"))
        if set(after) == {"check_decision", "check_basis_json"}:
            if before.get("check_decision") != 1 or after["check_decision"] not in (2, 3):
                raise EventValidationError("检查决定必须从未判定一次固定")
        elif set(after) == {"check_basis_json"}:
            old, new = before.get("check_basis_json"), after["check_basis_json"]
            if (not isinstance(old, Mapping) or "threshold_s" in old
                    or "threshold_s" not in new or new.get("reason") != 2
                    or not json_equal(old, {k: v for k, v in new.items() if k != "threshold_s"})):
                raise EventValidationError("只能为原检查依据补写门槛")
            if (context.transaction.first_event_id != event.event_id
                    or context.transaction.last_event_id != event.event_id):
                raise EventValidationError("检查门槛补写必须独立提交")
            if context.current_reads is None:
                raise EventValidationError("检查门槛补写缺少独立读取事实")
            try:
                action, actual, copies = context.current_reads.check_input_facts(processing["action_id"], row.row_id)
                _require_unread_check_input(action, actual, copies, processing["action_id"], old)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
        else:
            raise EventValidationError("检查决定事件字段不完整")
    elif event.reason == _DECIDED_REPAIR_REASON:
        row, _ = _single_processing_row(event, context)
        state = row.after.values.get("repair_state")
        if row.before.values.get("repair_state") != 1 or state not in (2, 3, 7):
            raise EventValidationError("修复决定必须从未判定一次固定")
        _validate_repair_basis(row.after.values.get("repair_basis_json"))


def _processed_processing_guard(event, context) -> None:
    """RECORDING_PROCESSED 三分支：阶段、媒体与成品事实共同一致。"""
    if event.reason == _PROCESSED_CHECK_REASON:
        row, processing = _single_processing_row(event, context)
        if processing.get("check_decision") != 3:
            raise EventValidationError(
                f"检查执行要求检查决定固定为需要检查: {processing.get('check_decision')!r}")
        state = row.after.values.get("check_state")
        if state not in (2, 3, 4, 5):
            raise EventValidationError(f"检查阶段不在允许范围: {state!r}")
        _validate_media_document(row.after.values.get("media_json"), state)
    elif event.reason == _PROCESSED_REPAIR_REASON:
        row, processing = _single_processing_row(event, context)
        if row.before.values.get("repair_state") not in (3, 4):
            raise EventValidationError(
                "修复结果只能在待执行或执行中阶段保存")
        state = row.after.values.get("repair_state")
        if state not in (4, 5, 6, 7):
            raise EventValidationError(f"修复阶段不在允许范围: {state!r}")
        if state == 5:
            file_id = row.after.values.get("repair_output_file_id")
            if not isinstance(file_id, int) or isinstance(file_id, bool):
                raise EventValidationError(f"修复成功必须指向修复输出: {file_id!r}")
            output = context.state_rows.get("intermediate_files", {}).get(file_id)
            if output is None:
                raise EventValidationError("修复成功缺少输出文件事实")
            if output.get("purpose") != int(_INTERMEDIATE_PURPOSE.REPAIR_OUTPUT):
                raise EventValidationError(
                    f"修复成功必须指向修复输出用途: {output.get('purpose')!r}")
            if output.get("owner_action_id") != processing.get("action_id"):
                raise EventValidationError("修复输出属于其他动作")
            if output.get("size_bytes") is None or output.get("sha256") is None:
                raise EventValidationError("修复成功要求输出已有完整字节事实")
            if context.current_reads is None:
                raise EventValidationError("修复成功缺少独立媒体读取端口")
            from camctl.capture.media_facts import read_probe
            try:
                raw = event.evidence.get("output_probe")
                context.current_reads.repair_media_proof(processing["action_id"], processing["id"], file_id,
                    output_probe=None if raw is None else read_probe(raw))
            except (ConsistencyError, ValueError) as error:
                raise EventValidationError(str(error)) from error
        elif state == 6:
            _require_processing_error_document(
                "修复错误", row.after.values.get("repair_error_json"))
    elif event.reason == _PROCESSED_DISCARD_REASON:
        row, _ = _single_processing_row(event, context)
        state = row.after.values.get("discard_state")
        if state not in (2, 3, 4, 5, 6):
            raise EventValidationError(f"收场进度不在允许范围: {state!r}")
        if state in (5, 6):
            _require_processing_error_document(
                "收场错误", row.after.values.get("discard_error_json"))


def _processing_guard(event, context) -> None:
    """录像处理守卫：字节事实与处理状态分支共同约束。

    字节分支要求处理输入副本保存完整字节事实前完成唯一拷贝
    校验；状态分支校验 RECORDING_DECIDED/RECORDING_PROCESSED 的
    决定、依据、媒体结构与成品事实。
    """
    if event.event_type == _RECORDING_DECIDED_EVENT:
        _decided_processing_guard(event, context)
        return
    if event.event_type == _RECORDING_PROCESSED_EVENT:
        _processed_processing_guard(event, context)
        return
    if event.event_type != _INTERMEDIATE_FILE_EVENT or event.reason != _LIFECYCLE_REASON:
        return
    for row in event.rows:
        if row.table != "intermediate_files":
            continue
        if row.after.values.get("sha256") is None:
            continue
        copies = [
            facts for facts in context.state_rows.get("file_copies", {}).values()
            if facts.get("target_file_id") == row.row_id
        ]
        if not copies:
            continue
        if len(copies) != 1:
            raise EventValidationError("处理输入的完整字节事实必须对应唯一拷贝")
        copy = copies[0]
        if copy.get("verification_state") not in (
            int(_VERIFICATION.MATCHED),
            int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
        ):
            raise EventValidationError("处理输入副本未完成字节校验不能保存完整事实")
        if copy.get("committed_bytes") != copy.get("source_size"):
            raise EventValidationError("处理输入副本字节未完整保存不能保存完整事实")


def _requested_cleanup_ids(action):
    """精确清理的原请求 ID 列表；范围清理返回 None。"""
    params = action.get("input_fields_json")
    if not isinstance(params, Mapping):
        raise EventValidationError("清理动作缺少原请求参数")
    values = params.get("params")
    if not isinstance(values, Mapping) or "output_ids" not in values:
        return None
    ids = values["output_ids"]
    if not isinstance(ids, list) or not ids:
        raise EventValidationError("精确清理缺少非空原目标列表")
    return tuple(int(identity) for identity in ids)


def _target_set_guard(event, context) -> None:
    """TARGETS_FIXED 清理/取消/失败：类型、目标状态与成员集合一致。"""
    if event.event_type != _TARGETS_FIXED_EVENT:
        return
    if event.reason not in (_TARGETS_CLEANUP_REASON, _TARGETS_CANCEL_REASON,
                            _TARGETS_FAIL_REASON):
        return
    action_rows = [row for row in event.rows if row.table == "actions"]
    if len(action_rows) != 1 or not action_rows[0].before.exists:
        raise EventValidationError("目标固定必须恰好更新一条动作行")
    action_id = action_rows[0].row_id
    action = context.state_rows.get("actions", {}).get(action_id)
    if action is None:
        raise EventValidationError("目标固定缺少动作当前事实")
    if event.reason == _TARGETS_FAIL_REASON:
        if action.get("type") not in (_DELETE_ACTION_TYPE, _CANCEL_ACTION_TYPE):
            raise EventValidationError("目标固定失败要求清理或取消动作")
    else:
        expected = (_CANCEL_ACTION_TYPE
                    if event.reason == _TARGETS_CANCEL_REASON
                    else _DELETE_ACTION_TYPE)
        if action.get("type") != expected:
            raise EventValidationError(
                f"目标固定分支与动作类型不符: {action.get('type')!r}")
    created = [row for row in event.rows
               if row.table == "cleanup_items" and not row.before.exists]
    if event.reason == _TARGETS_FAIL_REASON:
        if created:
            raise EventValidationError("目标固定失败不创建清理成员")
        return
    if event.reason != _TARGETS_CLEANUP_REASON:
        return
    explicit = _requested_cleanup_ids(action)
    if not created:
        if explicit is None:
            # 范围清理固定空集合：来源已可靠确认没有正式产物，动
            # 作成功收场由同事务的完成事件表达。
            return
        raise EventValidationError("清理目标固定必须创建清理成员")
    requested = []
    for row in created:
        values = row.after.values
        if values.get("action_id") != action_id:
            raise EventValidationError("清理成员必须属于目标固定动作")
        if (values.get("status") != int(_CLEANUP_ITEM_STATUS.UNRESOLVED)
                or values.get("restriction_state")
                != int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED)
                or values.get("output_id") is not None
                or values.get("outcome") is not None
                or values.get("final_event_id") is not None
                or values.get("error_code") is not None):
            raise EventValidationError("清理成员初始值必须是未解析且无限制")
        requested.append(values.get("requested_output_id"))
    if len(set(requested)) != len(requested):
        raise EventValidationError("清理成员的原请求目标不能重复")
    if explicit is not None:
        from camctl.outputs.cleanup_group import cleanup_group
        reads = _CurrentProductReads(context)
        expected = list(explicit)
        for identity in explicit:
            for member in cleanup_group(reads, identity):
                if member not in expected:
                    expected.append(member)
        actual = [row.after.values.get("requested_output_id")
                  for row in sorted(created, key=lambda row: row.row_id)]
        if actual != expected:
            raise EventValidationError("精确清理成员与原请求及机内照片配对不一致")
    if explicit is None:
        dependencies = context.state_rows.get("action_dependencies", {})
        sources = {
            values.get("depends_on_action_id")
            for values in dependencies.values()
            if values.get("action_id") == action_id
        }
        outputs = context.state_rows.get("outputs", {})
        for identity in requested:
            output = outputs.get(identity)
            if output is None or output.get("source_action_id") not in sources:
                raise EventValidationError(
                    f"范围清理成员不属于本动作固定来源: {identity!r}")


def _cleanup_member_guard(event, context) -> None:
    """CLEANUP_CHANGED：成员归属、身份保持与直接终态事务边界。"""
    if event.event_type != _CLEANUP_CHANGED_EVENT:
        return
    for row in event.rows:
        if row.table != "cleanup_items":
            continue
        if not row.before.exists:
            values = row.after.values
            action = context.state_rows.get("actions", {}).get(
                values.get("action_id"))
            if (action is None
                    or action.get("target_selection_state") != _TARGET_FIXED):
                raise EventValidationError(
                    "直接终态清理成员只能属于已固定的目标集合事务")
            if values.get("final_event_id") != event.event_id:
                raise EventValidationError("清理成员的最终事件必须是本事件")
            continue
        before_facts = context.state_rows.get("cleanup_items", {}).get(row.row_id)
        if before_facts is None:
            raise EventValidationError("清理成员更新缺少当前事实")
        action = context.state_rows.get("actions", {}).get(
            before_facts.get("action_id"))
        if (action is None
                or action.get("target_selection_state") != _TARGET_FIXED):
            raise EventValidationError("清理成员推进要求目标集合已固定")
        if (row.after.values.get("requested_output_id")
                not in (None, before_facts.get("requested_output_id"))
                or row.after.values.get("action_id")
                not in (None, before_facts.get("action_id"))):
            raise EventValidationError("清理成员身份与原请求保持不变")
        if event.reason == 1:
            confirmed = row.after.values.get("output_id")
            if (confirmed is not None
                    and before_facts.get("output_id") is not None):
                raise EventValidationError("产物身份只能从空值一次确认")
            if (confirmed is not None
                    and confirmed != before_facts.get("requested_output_id")):
                raise EventValidationError("确认的产物必须是原请求目标")
            output = context.state_rows.get("outputs", {}).get(confirmed)
            if output is not None:
                from camctl.outputs.cleanup_group import cleanup_group
                reads = _CurrentProductReads(context)
                if any(reads.required("outputs", group_id).get("availability") == int(_AVAILABILITY.AVAILABLE)
                       and has_product_predecessor(reads, before_facts["action_id"], group_id, event.occurred_at)
                       for group_id in cleanup_group(reads, confirmed)):
                    raise EventValidationError("较早的取回或清理尚未取得资格")


def _cleanup_guard(event, context) -> None:
    """CLEANUP_CHANGED：终态依据、最终事件与唯一删除处理者。"""
    if event.event_type != _CLEANUP_CHANGED_EVENT:
        return
    for row in event.rows:
        if row.table != "cleanup_items":
            continue
        values = row.after.values
        terminal = values.get("status") in (
            int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
            int(_CLEANUP_ITEM_STATUS.FAILED),
            int(_CLEANUP_ITEM_STATUS.CANCELED),
        )
        if terminal:
            if values.get("final_event_id") != event.event_id:
                raise EventValidationError("清理成员的最终事件必须是本事件")
            if values.get("status") == int(_CLEANUP_ITEM_STATUS.SUCCEEDED):
                _require_cleanup_success_basis(event, row, context)
            elif values.get("status") == int(_CLEANUP_ITEM_STATUS.FAILED):
                item = context.state_rows.get("cleanup_items", {}).get(row.row_id)
                if (item is not None and item.get("output_id") is not None
                        and context.read_coverage.covers("cleanup_items", "action_id", item["action_id"])):
                    owner = _cleanup_group_call_owner(_CurrentProductReads(context), item)
                    if owner is not None and owner["id"] != row.row_id:
                        details = dict(owner.get("error_details_json") or {})
                        if "output_id" in details:
                            details["output_id"] = str(item["output_id"])
                        if (owner["status"] != int(_CLEANUP_ITEM_STATUS.FAILED)
                                or values.get("error_code") != owner.get("error_code")
                                or not json_equal(values.get("error_details_json"), details)):
                            raise EventValidationError("共同照片后续成员必须沿原处理者失败事实结束")
            continue
        if values.get("status") == int(_CLEANUP_ITEM_STATUS.DELETING):
            before = context.state_rows.get("cleanup_items", {}).get(
                row.row_id, {})
            target = before.get("output_id") or values.get("output_id")
            if target is None:
                continue
            if not _cleanup_entry_ready(_CurrentProductReads(context), before):
                raise EventValidationError("共同来源仍有读取依赖或其他删除处理者")
            owner = _cleanup_group_call_owner(_CurrentProductReads(context), before, exclude_self=True)
            if owner is not None and owner["id"] != row.row_id:
                raise EventValidationError("同一请求的共同照片已有处理者")
            for identity, facts in context.state_rows.get(
                    "cleanup_items", {}).items():
                if identity == row.row_id:
                    continue
                if (facts.get("output_id") == target
                        and facts.get("status")
                        == int(_CLEANUP_ITEM_STATUS.DELETING)):
                    raise EventValidationError("同一产物只能有一个删除中成员")


def _require_cleanup_success_basis(event, row, context) -> None:
    """成功依据按结果分类核对：实际删除、已有完成或存在性查询。"""
    outcome = row.after.values.get("outcome")
    if outcome is None:
        raise EventValidationError("清理成功必须携带结果依据")
    target = (row.after.values.get("output_id")
              or row.before.values.get("output_id"))
    if target is None:
        # 成功事件行不携带产物列；待删除成员复用完成事实时从当前
        # 成员事实解析目标。
        target = context.state_rows.get("cleanup_items", {}).get(
            row.row_id, {}).get("output_id")
    if outcome == int(_CLEANUP_OUTCOME.ALREADY_CLEANED):
        output = context.state_rows.get("outputs", {}).get(target)
        if (output is None
                or output.get("availability") != int(_AVAILABILITY.CLEANED)):
            raise EventValidationError("已有完成依据要求产物已可靠清理")
        return
    output = context.state_rows.get("outputs", {}).get(target)
    file_id = None
    if output is not None:
        file_id = (output.get("device_file_id")
                   or output.get("intermediate_file_id"))
    if outcome == int(_CLEANUP_OUTCOME.DELETED):
        if not _completed_call_basis(context, row.row_id, file_id):
            raise EventValidationError(
                "实际删除依据要求已完成的删除调用或文件缺席事实")
    elif outcome == int(_CLEANUP_OUTCOME.ABSENCE_CONFIRMED):
        if file_id is not None and not _file_absent(context, file_id):
            raise EventValidationError("存在性查询依据要求文件缺席事实")
    else:
        raise EventValidationError(f"未登记的清理结果依据: {outcome!r}")


def _completed_call_basis(context, item_id, file_id) -> bool:
    """本事件之前已有完成的删除调用或文件缺席事实。"""
    if file_id is not None and _file_absent(context, file_id):
        return True
    runs = {
        values.get("id"): values
        for values in context.state_rows.get("operation_runs", {}).values()
        if values.get("cleanup_item_id") == item_id
    }
    for attempt in context.state_rows.get("operation_attempts", {}).values():
        run = runs.get(attempt.get("run_id"))
        if (run is not None and attempt.get("status") == 2
                and attempt.get("effect_state") == 3):
            return True
    return False


def _file_absent(context, file_id) -> bool:
    device = context.state_rows.get("device_files", {}).get(file_id)
    if device is not None and device.get("presence_state") == 3:
        return True
    local = context.state_rows.get("intermediate_files", {}).get(file_id)
    if local is not None and local.get("cleanup_state") == 4:
        return True
    return False


def _cleanup_entry_ready(reads, item):
    """读取完整共同来源范围后判断能否取得物理删除责任。"""
    from camctl.outputs.cleanup_group import cleanup_group

    group = cleanup_group(reads, item["output_id"])
    own = reads.members("cleanup_items", "action_id", item["action_id"])
    for output_id in group:
        if any(cleanup_read_dependency_blocks(row)
               for row in reads.members("obtain_items", "output_id", output_id).values()):
            return False
        for identity, row in reads.members("cleanup_items", "output_id", output_id).items():
            if cleanup_other_delete_blocks(current_id=item["id"], member_id=identity, member=row):
                return False
        # 旧的已固定集合允许没有配对项；新集合的每一项必须先建立限制。
        for row in own.values():
            if cleanup_own_restriction_blocks(output_id=output_id, member=row):
                return False
    return True


def _cleanup_group_call_owner(reads, item, *, exclude_self=False):
    """从同请求中最早可能派发的票据取得共同照片处理者。"""
    from camctl.outputs.cleanup_group import cleanup_group
    from camctl.operations.result_format import read_result_document

    if item["output_id"] is None:
        return None
    group = cleanup_group(reads, item["output_id"])
    if len(group) == 1:
        return None
    attempts = []
    for identity, member in reads.members("cleanup_items", "action_id", item["action_id"]).items():
        if exclude_self and identity == item["id"]:
            continue
        if member["requested_output_id"] not in group:
            continue
        for run_id, run in reads.members("operation_runs", "cleanup_item_id", identity).items():
            if run["kind"] not in (4, 5):
                continue
            for attempt in reads.members("operation_attempts", "run_id", run_id).values():
                attempts.append((attempt["intent_event_id"], attempt["id"], member, attempt))
    for _, _, member, attempt in sorted(attempts, key=lambda value: (value[0], value[1])):
        if attempt["status"] != 1:
            try:
                actual = read_result_document(attempt["status"], attempt["effect_state"],
                    attempt["result_json"], attempt["error_json"])
            except (TypeError, ValueError) as exc:
                raise ConsistencyError("共同照片处理尝试无法解释") from exc
            if actual.settlement.basis is SettlementBasis.NOT_DISPATCHED:
                continue
        return member
    return None


def register_outputs_guards(register) -> None:
    """注册来源、选择与读取资格事件的正式业务守卫（装配期调用）。"""
    register("selection_initialization", _selection_initialization_guard)
    register("source_selection", _source_selection_guard)
    register("intermediate", _intermediate_guard)
    register("cursor", _cursor_guard)
    register("delivery", _delivery_guard)
    register("copy", _copy_guard)
    from .read_end import read_end_guard
    register("read_end", read_end_guard)
    register("copy_links", _copy_links_guard)
    register("read_slot", _read_slot_guard)
    register("read_permission", _read_permission_guard)
    register("processing", _processing_guard)
    register("recording_source", _recording_source_guard)
    register("obtain_member", _obtain_member_guard)
    register("target_set", _target_set_guard)
    register("cleanup_member", _cleanup_member_guard)
    register("cleanup", _cleanup_guard)
    from camctl.persistence.repositories.cleanup_absence import cleanup_result_source_guard
    register("cleanup_result_source", cleanup_result_source_guard)
    from camctl.persistence.repositories.segment_queries import (
        register_segment_query_guards,
    )

    register_segment_query_guards(register)
