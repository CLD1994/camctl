"""录像完成终态与录像内部处理的事务。

经 P3 事务内核组织：动作成功终态、正式产物登记（含修复成品承载
文件的共同提升）及父计划状态在同一事务共同保存，任一写入失败整
组回滚；登记前经 X1 纯规则校验，事件守卫从当前文件事实复核来源、
原设备绑定、文件角色、修复成功依据、初始可用性组合与提升配对。
录像内部处理提供决定与结果保存，以及修复输出的登记与完成事务：
路径登记先于文件写入，完整字节与修复成功同事务固定。停止、活动
结束与产物、动作结果分别保存，不互相混同。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any, Mapping

from camctl.capture.media import RecordingFailure
from camctl.capture.processing import (
    CheckDecisionSave,
    CheckResultSave,
    DiscardPhase,
    DiscardProgressSave,
    ProcessingDisposition,
    ProcessingOutcome,
    RepairDecisionSave,
    RepairOutcome,
    RepairOutputFile,
    RepairResultSave,
    RepairStart,
    RepairSuccess,
    SourceFileSave,
)
from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord, RecordStatus
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.contracts.workflow_errors import (
    registered_error,
    validate_error_details,
)
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import relative_file_path
from camctl.outputs.catalog import (
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
    validate_output_registration,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id as _next_id,
    row_change as _row,
    row_facts,
    saved_transaction_events,
    update_change as _update,
)

_ACTION_FINISHED_EVENT = 8
_OUTPUT_REGISTERED_EVENT = 20
_PLAN_STATUS_EVENT = 9
_EMERGENCY_RECORDED_EVENT = 33
_RECORDING_DECIDED_EVENT = 18
_RECORDING_PROCESSED_EVENT = 19
_INTERMEDIATE_FILE_EVENT = 26

#: INTERMEDIATE_FILE_CHANGED.LIFECYCLE：保存完整字节及保留用途变化。
_LIFECYCLE_REASON = 2

_RETENTION = enum_for("intermediate_files.retention_state")
_PURPOSE = enum_for("intermediate_files.purpose")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")

#: RECORDING_DECIDED 的三个分支。
_DECIDED_CHECK_REASON = 1
_DECIDED_REPAIR_REASON = 2
_DECIDED_SOURCE_REASON = 3

#: RECORDING_PROCESSED 的三个分支。
_PROCESSED_CHECK_REASON = 1
_PROCESSED_REPAIR_REASON = 2
_PROCESSED_DISCARD_REASON = 3

_ACTION_RUNNING = 2
_ACTION_SUCCEEDED = 3
_ACTION_FAILED = 4
_PLAN_COMPLETE = 3

#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

_OUTPUT_KIND = enum_for("outputs.kind")
_KIND_CODES = {kind: int(_OUTPUT_KIND[kind.name]) for kind in OutputKind}


def _unchecked_media() -> dict[str, Any]:
    """登记时产物文件尚无媒体检查：公共 media 结构表达未检查与未知时长。"""
    return {"check_status": "not_performed", "duration": {"status": "unknown"}}

#: device_files.role 与产物种类的对应；修复产物承载于中间文件。
_FILE_ROLE = enum_for("device_files.role")
_FILE_ROLE_FOR_KIND = {_OUTPUT_KIND.ORIGINAL: _FILE_ROLE.ORIGINAL, _OUTPUT_KIND.PREVIEW: _FILE_ROLE.PREVIEW}
_ACTION_TYPE = enum_for("actions.type")
_CAPTURE_TYPES = frozenset({
    _ACTION_TYPE.CAMERA_TAKE_PHOTO,
    _ACTION_TYPE.CAMERA_RECORD,
    _ACTION_TYPE.CAMERA_TIMELAPSE,
})


@dataclass(frozen=True)
class FinishCapture:
    """一次录像完成登记的完整输入：终态事实与全部适用产物。

    failure 为空保存成功终态；携带失败时按公共动作错误码保存执行
    失败终态，产物登记与失败事实同事务提交。
    """

    action_id: int
    drafts: tuple[OutputDraft, ...]
    catalog_facts: OutputCatalogFacts
    occurred_at: int
    failure: RecordingFailure | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)


@dataclass(frozen=True)
class CaptureResult:
    """完成登记的已保存事实。"""

    action_status: int
    plan_status: int
    output_ids: tuple[int, ...]


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _action_finish_guard(event, context) -> None:
    """动作终态组合与产物同事务校验。"""
    for row in event.rows:
        if row.table == "actions" and row.before.exists:
            after = row.after.values
            if after.get("status") == _ACTION_SUCCEEDED:
                facts = _guard_facts(context, "actions", row.row_id)
                facts.update(row.before.values)
                if facts.get("cancel_requested") != 0:
                    raise EventValidationError("取消请求生效的动作不能保存成功终态")
        elif row.table == "outputs" and not row.before.exists:
            source = row.after.values.get("source_action_id")
            action = _guard_facts(context, "actions", source)
            if action.get("status") not in _ACTION_TERMINAL:
                raise EventValidationError(
                    f"产物登记要求源动作终态同事务成立: 动作 {source} 状态"
                    f" {action.get('status')}"
                )


def _output_guard(event, context) -> None:
    """从事件当前事实核对来源、原设备绑定、文件角色与提升配对。"""
    if event.event_type == _INTERMEDIATE_FILE_EVENT:
        _promotion_pairing(event, context)
        return
    for row in event.rows:
        if row.table == "outputs" and not row.before.exists:
            _output_file(row.after.values, context)
    _output_relationships(event, context)


def _promotion_pairing(event, context) -> None:
    """INTERMEDIATE_FILE_CHANGED.LIFECYCLE：提升只因同事务登记发生。

    推进到 PROMOTED 的中间文件要求先前事件已登记承载它的修复成
    品；释放与交接的保留变化不经本核对。
    """
    if event.reason != _LIFECYCLE_REASON:
        return
    for row in event.rows:
        if row.table != "intermediate_files":
            continue
        if row.after.values.get("retention_state") != int(_RETENTION.PROMOTED):
            continue
        file = context.state_rows.get("intermediate_files", {}).get(row.row_id)
        if file is None:
            raise EventValidationError(
                f"提升缺少当前文件事实: intermediate_files#{row.row_id}")
        owner = file.get("owner_action_id")
        for output in context.state_rows.get("outputs", {}).values():
            if (output.get("kind") == _KIND_CODES[OutputKind.REPAIRED]
                    and output.get("intermediate_file_id") == row.row_id
                    and output.get("source_action_id") == owner):
                break
        else:
            raise EventValidationError(
                "文件提升必须由同事务先行的修复成品登记授权:"
                f" intermediate_files#{row.row_id}")


def _output_file(values, context) -> None:
    """新登记产物及所引用的既有原片共用文件身份与来源校验。"""
    source_id = values["source_action_id"]
    source = _registration_facts(context, "actions", source_id)
    source_binding = _capture_binding(source)
    kind = values.get("kind")
    if not {"device_file_id", "intermediate_file_id"} <= values.keys():
        raise EventValidationError("产物承载文件类型尚未完整读取")
    device_file_id = values.get("device_file_id")
    if (device_file_id is None) == (values.get("intermediate_file_id") is None):
        raise EventValidationError("产物必须恰好由一种文件身份承载")
    if device_file_id is not None:
        facts = _registration_facts(context, "device_files", device_file_id)
        if facts.get("source_action_id") != source_id or facts.get("ownership_evidence_json") is None:
            raise EventValidationError("产物承载文件缺少该来源动作的可靠归属")
        observer = _registration_facts(context, "actions", facts.get("observer_action_id"))
        if _capture_binding(observer) != source_binding:
            raise EventValidationError("产物来源与文件观察者的原设备或驱动绑定不一致")
        expected_role = _FILE_ROLE_FOR_KIND.get(kind)
        if expected_role is None or facts.get("role") != expected_role:
            raise EventValidationError(f"产物种类与文件角色不符: kind={kind} role={facts.get('role')}")
        if facts.get("completion_state") != 3:
            raise EventValidationError(f"产物承载文件未完成: {device_file_id}")
        if kind == _OUTPUT_KIND.ORIGINAL and (
            "original_device_file_id" not in facts or "pairing_evidence_json" not in facts
            or facts["original_device_file_id"] is not None or facts["pairing_evidence_json"] is not None
        ):
            raise EventValidationError("原片文件必须明确没有预览配对")
    else:
        if kind != _OUTPUT_KIND.REPAIRED:
            raise EventValidationError("只有修复成品使用中间文件")
        facts = _registration_facts(context, "intermediate_files", values.get("intermediate_file_id"))
        if (facts.get("owner_action_id") != source_id
                or "owner_delivery_id" not in facts
                or facts["owner_delivery_id"] is not None):
            raise EventValidationError("产物中间文件不属于来源动作的文件责任")
        _require_promotable_repair_output(facts)
        _require_repair_success(context, source_id, values.get("intermediate_file_id"))


def _require_promotable_repair_output(facts) -> None:
    """修复成品的承载文件必须仍是未释放的完整修复输出。"""
    if facts.get("purpose") != int(_PURPOSE.REPAIR_OUTPUT):
        raise EventValidationError(
            f"修复成品承载文件不是修复输出用途: {facts.get('purpose')!r}")
    if facts.get("retention_state") != int(_RETENTION.REQUIRED):
        raise EventValidationError(
            "修复成品承载文件必须尚未释放、提升或交接:"
            f" {facts.get('retention_state')!r}")
    if facts.get("size_bytes") is None or facts.get("sha256") is None:
        raise EventValidationError("修复成品承载文件缺少完整字节事实")


def _require_repair_success(context, source_id: int, file_id) -> None:
    """登记以本动作的修复成功事实为前提；缺失或指向他处均拒绝。"""
    for processing in context.state_rows.get("recording_processing", {}).values():
        if (processing.get("action_id") == source_id
                and processing.get("repair_state") == RepairOutcome.SUCCEEDED.value
                and processing.get("repair_output_file_id") == file_id):
            return
    raise EventValidationError(
        f"产物登记缺少本动作的修复成功事实: intermediate_files#{file_id}")


def _output_relationships(event, context) -> None:
    """登记自己的关系与当前完整反向集合共同决定原片及派生唯一性。"""
    outputs = {row.row_id: row.after.values for row in event.rows
               if row.table == "outputs" and not row.before.exists}
    links: dict[int, list[Mapping[str, Any]]] = {}
    for row in event.rows:
        if row.table != "output_origins":
            continue
        values = row.after.values
        if values["output_id"] not in outputs:
            raise EventValidationError("关联必须属于本事件共同登记的产物")
        links.setdefault(values["output_id"], []).append(values)
    kinds_by_original: dict[int, set[int]] = {}
    for identity, output in outputs.items():
        if context.complete_rows("output_origins", "output_id", identity):
            raise EventValidationError("新产物不能已有原片关联")
        own_links = links.get(identity, ())
        kind = output["kind"]
        if kind == _OUTPUT_KIND.ORIGINAL:
            if own_links:
                raise EventValidationError("原片不能携带派生关联")
            continue
        if len(own_links) != 1:
            raise EventValidationError("派生产物必须共同登记唯一原片关联")
        original_id = own_links[0]["original_output_id"]
        if original_id == identity:
            raise EventValidationError("派生产物不能引用自身")
        original = _registration_facts(context, "outputs", original_id)
        if (original.get("kind") != _OUTPUT_KIND.ORIGINAL
                or original.get("source_action_id") != output["source_action_id"]
                or original.get("device_file_id") is None
                or original.get("intermediate_file_id") is not None):
            raise EventValidationError("派生关联必须指向同源的设备原片")
        if context.complete_rows("output_origins", "output_id", original_id):
            raise EventValidationError("引用的原片不能携带派生关联")
        _output_file(original, context)
        if kind == _OUTPUT_KIND.PREVIEW:
            file = _registration_facts(context, "device_files", output["device_file_id"])
            if (file.get("original_device_file_id") != original["device_file_id"]
                    or file.get("pairing_evidence_json") is None):
                raise EventValidationError("预览的产物关联与设备文件配对不一致")
        if original_id not in kinds_by_original:
            related = context.complete_rows("output_origins", "original_output_id", original_id)
            kinds: set[int] = set()
            for relation in related.values():
                previous = _registration_facts(context, "outputs", relation["output_id"])
                previous_kind = previous.get("kind")
                if (previous_kind not in {_OUTPUT_KIND.PREVIEW, _OUTPUT_KIND.REPAIRED}
                        or previous_kind in kinds
                        or previous.get("source_action_id") != output["source_action_id"]):
                    raise EventValidationError("已有原片派生集合的种类或来源矛盾")
                kinds.add(previous_kind)
            kinds_by_original[original_id] = kinds
        kinds = kinds_by_original[original_id]
        if kind in kinds:
            raise EventValidationError("同一原片至多登记一份预览和一份修复成品")
        kinds.add(kind)


def _registration_facts(context, table: str, identity: int | None) -> Mapping[str, Any]:
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"产物登记缺少当前关联记录: {table}#{identity}")
    return facts


def _capture_binding(action: Mapping[str, Any]) -> tuple[str, str]:
    if action.get("type") not in _CAPTURE_TYPES:
        raise EventValidationError("产物来源和设备文件观察者必须是拍摄动作")
    device, driver = action.get("device_id"), action.get("driver_id")
    if not isinstance(device, str) or not device or not isinstance(driver, str) or not driver:
        raise EventValidationError("拍摄动作缺少已保存的设备或驱动绑定")
    return device, driver


def _cleanup_aggregate_guard(event, context) -> None:
    """产物初始可用性与清理状态组合校验。"""
    for row in event.rows:
        if row.table != "outputs" or row.before.exists:
            continue
        values = row.after.values
        if values.get("cleanup_status") != 1:
            raise EventValidationError("正式登记的初始清理状态必须是未请求")
        availability = values.get("availability")
        if availability not in (1, 5):
            raise EventValidationError(f"登记初始可用性非法: {availability!r}")
        if availability == 5 and values.get("error_json") is None:
            raise EventValidationError("未知可用性必须携带错误依据")


def _plan_aggregate_guard(event, context) -> None:
    """计划状态与动作聚合一致性（受理创建与终态推进共用）。"""
    for row in event.rows:
        if row.table != "plans":
            continue
        if not row.before.exists:
            plan_id = row.row_id
            actions = [
                values
                for values in context.state_rows.get("actions", {}).values()
                if values.get("plan_id") == plan_id
            ]
            statuses = {values.get("status") for values in actions}
            expected = 3 if (actions and statuses == {4}) else 1
            if row.after.values.get("status") != expected:
                raise EventValidationError(f"计划状态与动作聚合不符: 期望 {expected}")
            continue
        if row.after.values.get("status") == _PLAN_COMPLETE:
            plan_id = row.row_id
            actions = [
                values
                for values in context.state_rows.get("actions", {}).values()
                if values.get("plan_id") == plan_id
            ]
            if not actions or any(
                values.get("status") not in _ACTION_TERMINAL for values in actions
            ):
                raise EventValidationError("计划完成要求全部动作终态")


def register_capture_guards() -> None:
    """注册采集终态事务的正式业务守卫（装配期调用）。"""
    register_guard("action_finish", _action_finish_guard)
    register_guard("output", _output_guard)
    register_guard("cleanup_aggregate", _cleanup_aggregate_guard)
    register_guard("plan_aggregate", _plan_aggregate_guard)
    register_guard("emergency", _emergency_guard)
    register_guard("activity", _activity_guard)
    register_guard("release", _release_guard)


class FinishCaptureCommand:
    """一次录像完成登记的完整事务命令。"""

    def __init__(self, command: FinishCapture, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}
        self._ranges: dict[tuple[str, str], set[int]] = {}
        self._origin_members: dict[tuple[str, int], tuple[int, ...]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("完成登记的重送须由调用方按原事务核实")

        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {command.action_id}")
        siblings = self._sibling_actions(connection, action)
        self._state["actions"] = dict(siblings)
        self._state.setdefault("outputs", {})
        self._state.setdefault("device_files", {})
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        if action["status"] != _ACTION_RUNNING or action["cancel_requested"]:
            raise TransactionError(
                f"只有未取消的执行中动作能保存终态: {command.action_id}"
                f" status={action['status']}"
            )
        action_status = _ACTION_SUCCEEDED
        error_id: int | None = None
        error_details: dict[str, Any] | None = None
        if command.failure is not None:
            spec = registered_error(command.failure.code)
            if "action_error_id" not in spec:
                raise TransactionError(
                    f"失败错误码不是动作错误: {command.failure.code!r}")
            validate_error_details(command.failure.code, command.failure.details)
            action_status = _ACTION_FAILED
            error_id = spec["action_error_id"]
            error_details = dict(command.failure.details)

        # 登记规则在同一事务内校验：任一草稿不合法整组拒绝。
        _capture_binding(action)
        if command.catalog_facts.action_id != command.action_id:
            raise TransactionError("目录上下文与完成命令的动作身份不一致")
        changes = validate_output_registration(command.drafts, command.catalog_facts)
        for output in changes.outputs:
            self._load_file(connection, output.device_file_id, output.intermediate_file_id)
        self._verify_repaired_outputs(connection, changes)

        first_output_id = _next_id(connection, "outputs")
        numbered = tuple((first_output_id + index, output) for index, output in enumerate(changes.outputs))
        batch_originals = {output.device_file_id: identity for identity, output in numbered
                           if output.kind is OutputKind.ORIGINAL}
        original_ids = {}
        for identity, output in numbered:
            self._read_origins(connection, "output_id", identity)
            original_id = output.original_output_id
            if original_id is not None:
                original = self._required(connection, "outputs", original_id)
                self._load_file(connection, original["device_file_id"], original["intermediate_file_id"])
            elif output.original_batch_file_id is not None:
                original_id = batch_originals[output.original_batch_file_id]
            if original_id is not None:
                self._read_origins(connection, "output_id", original_id)
                for relation in self._read_origins(connection, "original_output_id", original_id):
                    self._required(connection, "outputs", relation["output_id"])
                original_ids[identity] = original_id

        if command.failure is None:
            action_row = _update(
                "actions",
                command.action_id,
                {"status": action["status"]},
                {"status": _ACTION_SUCCEEDED},
            )
            action_reason = 1
        else:
            action_row = _update(
                "actions",
                command.action_id,
                {
                    "status": action["status"],
                    "error_code": action["error_code"],
                    "error_details_json": action["error_details_json"],
                },
                {
                    "status": _ACTION_FAILED,
                    "error_code": error_id,
                    "error_details_json": error_details,
                },
            )
            action_reason = 2
        templates = [
            _envelope(
                0, 0, _ACTION_FINISHED_EVENT, action_reason,
                (action_row,),
                command.occurred_at,
            )
        ]
        next_origin_id = _next_id(connection, "output_origins")
        # 原片先进入当前事件事实，派生关系按显式引用解析；结果保持输入次序。
        promoted_files: list[int] = []
        for output_id, output in sorted(numbered, key=lambda item: item[1].kind is not OutputKind.ORIGINAL):
            name, media_type = self._readable_metadata(connection, output_id, output, original_ids)
            values = {
                "source_action_id": command.action_id,
                "kind": _KIND_CODES[output.kind],
                "device_file_id": output.device_file_id,
                "intermediate_file_id": output.intermediate_file_id,
                "original_name": name,
                "media_type": media_type,
                "availability": 1,
                "cleanup_status": 1,
                "cleanup_error_json": None,
                "media_json": _unchecked_media(),
                "error_json": None,
            }
            self._owners[("outputs", output_id)] = ("output", output_id)
            rows = (_row("outputs", output_id, values),)
            if output_id in original_ids:
                rows += (_row("output_origins", next_origin_id, {
                    "output_id": output_id, "original_output_id": original_ids[output_id],
                }),)
                self._owners[("output_origins", next_origin_id)] = ("output", output_id)
                next_origin_id += 1
            templates.append(
                _envelope(
                    0, 0, _OUTPUT_REGISTERED_EVENT, _KIND_CODES[output.kind],
                    rows, command.occurred_at,
                )
            )
            if output.kind is OutputKind.REPAIRED:
                promoted_files.append(output.intermediate_file_id)
        # 登记事件先行；承载文件在同事务提升为正式产物保留状态。
        for file_id in promoted_files:
            self._owners[("intermediate_files", file_id)] = ("intermediate_file", file_id)
            templates.append(
                _envelope(
                    0, 0, _INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON,
                    (_update(
                        "intermediate_files", file_id,
                        {"retention_state": int(_RETENTION.REQUIRED)},
                        {"retention_state": int(_RETENTION.PROMOTED)},
                    ),),
                    command.occurred_at,
                )
            )
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id,
        )
        plan_complete = all(
            values.get("status") in _ACTION_TERMINAL
            or values["id"] == command.action_id
            for values in siblings.values()
        )
        plan_status = plan["status"]
        if plan_complete and plan["status"] in (1, 2):
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            templates.append(
                _envelope(
                    0, 0, _PLAN_STATUS_EVENT, 2,
                    (
                        _update(
                            "plans",
                            plan["id"],
                            {"status": plan["status"]},
                            {"status": _PLAN_COMPLETE},
                        ),
                    ),
                    command.occurred_at,
                )
            )
            plan_status = _PLAN_COMPLETE

        allocation = scope.allocate(len(templates))
        events = tuple(
            replace(
                template,
                event_id=allocation.first_event_id + index,
                transaction_id=allocation.txn_id,
            )
            for index, template in enumerate(templates)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            read_coverage=ReadCoverage(self._ranges),
            result=CaptureResult(
                action_status=action_status,
                plan_status=plan_status,
                output_ids=tuple(identity for identity, _ in numbered),
            ),
        )

    def _required(self, connection, table, identity):
        rows = self._state.setdefault(table, {})
        if identity not in rows:
            facts = row_facts(connection, table, identity)
            if facts is None:
                raise TransactionError(f"产物登记关联记录不存在: {table}#{identity}")
            rows[identity] = facts
        return rows[identity]

    def _verify_repaired_outputs(self, connection, changes) -> None:
        """核对修复成品登记资格：未释放的完整修复输出配修复成功事实。

        没有修复成品时不读取处理事实；守卫在事件层用同样事实复核。
        """
        repaired = [output for output in changes.outputs
                    if output.kind is OutputKind.REPAIRED]
        if not repaired:
            return
        with closing(connection.execute(
                "SELECT id FROM recording_processing WHERE action_id = ?",
                (self._command.action_id,))) as cursor:
            found = cursor.fetchone()
        processing = None
        if found is not None:
            processing = self._required(
                connection, "recording_processing", int(found[0]))
        for output in repaired:
            file = self._state["intermediate_files"][output.intermediate_file_id]
            if file.get("purpose") != int(_PURPOSE.REPAIR_OUTPUT):
                raise TransactionError(
                    "修复成品承载文件不是修复输出用途:"
                    f" {output.intermediate_file_id}")
            if file.get("retention_state") != int(_RETENTION.REQUIRED):
                raise TransactionError(
                    "修复成品承载文件必须尚未释放、提升或交接:"
                    f" {file.get('retention_state')!r}")
            if file.get("size_bytes") is None or file.get("sha256") is None:
                raise TransactionError(
                    f"修复成品承载文件缺少完整字节事实: {output.intermediate_file_id}")
            if (processing is None
                    or processing.get("repair_state") != RepairOutcome.SUCCEEDED.value
                    or processing.get("repair_output_file_id")
                    != output.intermediate_file_id):
                raise TransactionError(
                    "产物登记缺少本动作的修复成功事实:"
                    f" {output.intermediate_file_id}")

    def _readable_metadata(self, connection, output_id, output, original_ids):
        """登记时的可读元信息取自承载文件行；观察未保存时保留未知。

        修复成品沿用其关联原片设备文件的可读名称与类型：修复不重
        新编码，媒体类型与原片一致。
        """
        if output.device_file_id is not None:
            file = self._required(connection, "device_files", output.device_file_id)
        else:
            if output.original_batch_file_id is not None:
                original_file_id = output.original_batch_file_id
            else:
                original_file_id = self._state["outputs"][
                    original_ids[output_id]]["device_file_id"]
            file = self._required(connection, "device_files", original_file_id)
        return file.get("original_name"), file.get("media_type")

    def _load_file(self, connection, device_id, intermediate_id):
        table = "device_files" if device_id is not None else "intermediate_files"
        file = self._required(connection, table, device_id if device_id is not None else intermediate_id)
        if device_id is not None:
            self._required(connection, "actions", file["observer_action_id"])

    def _read_origins(self, connection, column, identity):
        covered = self._ranges.setdefault(("output_origins", column), set())
        rows = self._state.setdefault("output_origins", {})
        if identity not in covered:
            limit = 2 if column == "output_id" else 3
            with closing(connection.execute(
                f"SELECT id,output_id,original_output_id FROM output_origins WHERE {column}=? ORDER BY id LIMIT ?",
                (identity, limit),
            )) as cursor:
                found = cursor.fetchall()
            if len(found) == limit:
                raise TransactionError("产物原片关联数量超过允许范围")
            for row_id, output_id, original_id in found:
                rows[row_id] = {"id": row_id, "output_id": output_id, "original_output_id": original_id}
            self._origin_members[column, identity] = tuple(row[0] for row in found)
            covered.add(identity)
        return tuple(rows[row_id] for row_id in self._origin_members[column, identity])

    def _sibling_actions(self, connection, action) -> dict[int, dict[str, Any]]:
        rows = {}
        for row in connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?", (action["plan_id"],)
        ).fetchall():
            facts = row_facts(connection, "actions", int(row[0]))
            if facts is not None:
                rows[int(row[0])] = facts
        return rows


# -- 录像内部处理的决定与结果 ---------------------------------------


#: 取消收场进度的合法转换；与 RECORDING_PROCESSED.DISCARD 登记一致。
_DISCARD_NEXT = {
    1: frozenset({DiscardPhase.PENDING.value}),
    2: frozenset({
        DiscardPhase.RUNNING.value, DiscardPhase.COMPLETED.value,
        DiscardPhase.FAILED.value, DiscardPhase.UNKNOWN.value}),
    3: frozenset({
        DiscardPhase.COMPLETED.value, DiscardPhase.FAILED.value,
        DiscardPhase.UNKNOWN.value}),
}

_FILE_PURPOSE = enum_for("intermediate_files.purpose")
_FILE_COMPLETION = enum_for("device_files.completion_state")


class _ProcessingCommand:
    """录像内部处理事务的共同装载：处理行、核对行与只读决定。"""

    def __init__(self) -> None:
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def _load_processing(self, connection, processing_id: int) -> dict[str, Any]:
        rows = self._state.setdefault("recording_processing", {})
        if processing_id not in rows:
            facts = row_facts(connection, "recording_processing", processing_id)
            if facts is None:
                raise ConsistencyError(f"录像处理记录不存在: {processing_id}")
            rows[processing_id] = facts
        return rows[processing_id]

    def _load_row(self, connection, table: str, row_id: int) -> dict[str, Any]:
        rows = self._state.setdefault(table, {})
        if row_id not in rows:
            facts = row_facts(connection, table, row_id)
            if facts is None:
                raise ConsistencyError(f"处理关联记录不存在: {table}#{row_id}")
            rows[row_id] = facts
        return rows[row_id]

    def _claim(self, processing) -> None:
        self._owners[("recording_processing", processing["id"])] = (
            "action", processing["action_id"])

    def _decision(
        self, disposition: ProcessingDisposition, *, read_only: bool = False,
    ) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(disposition), read_only=read_only)

    def _emit(
        self, scope, event_type: int, reason: int, row, occurred_at: int,
    ) -> CommandPlan:
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            event_type, reason, (row,), occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))

    def _reuse(self, saved, event_type: int, reason: int, occurred_at: int) -> CommandPlan:
        """原键恢复首次响应；承载其他阶段或事实时按身份冲突拒绝。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(event_type, reason)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为本事务重送")
        if saved[0]["occurred_at"] != occurred_at:
            raise TransactionError("处理事实的时刻与原事务不同")
        return self._decision(ProcessingDisposition.ALREADY, read_only=True)


class _CheckDecisionCommand(_ProcessingCommand):
    """固定原片检查决定：UNDETERMINED 一次固定，不因重送重算。"""

    def __init__(self, request: CheckDecisionSave, key: OperationKey) -> None:
        if not isinstance(request, CheckDecisionSave):
            raise TypeError("检查决定申请必须使用 CheckDecisionSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_DECIDED_EVENT, _DECIDED_CHECK_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["check_decision"] != 1:
            raise ConsistencyError(
                f"检查决定已固定，不重新判断: {processing['check_decision']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"check_decision": processing["check_decision"],
             "check_basis_json": processing["check_basis_json"]},
            {"check_decision": request.decision.value,
             "check_basis_json": request.basis.as_json()},
        )
        return self._emit(
            scope, _RECORDING_DECIDED_EVENT, _DECIDED_CHECK_REASON,
            row, request.occurred_at)


class _RepairDecisionCommand(_ProcessingCommand):
    """固定修复决定：UNDETERMINED 一次固定，保存后配置变化不重算。"""

    def __init__(self, request: RepairDecisionSave, key: OperationKey) -> None:
        if not isinstance(request, RepairDecisionSave):
            raise TypeError("修复决定申请必须使用 RepairDecisionSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_DECIDED_EVENT, _DECIDED_REPAIR_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["repair_state"] != 1:
            raise ConsistencyError(
                f"修复决定已固定，不重新判断: {processing['repair_state']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"],
             "repair_basis_json": processing["repair_basis_json"]},
            {"repair_state": request.decision.value,
             "repair_basis_json": request.basis.as_json()},
        )
        return self._emit(
            scope, _RECORDING_DECIDED_EVENT, _DECIDED_REPAIR_REASON,
            row, request.occurred_at)


class _SourceFileCommand(_ProcessingCommand):
    """首次关联可靠原片：属于本次动作、角色为原片且已确认写完。"""

    def __init__(self, request: SourceFileSave, key: OperationKey) -> None:
        if not isinstance(request, SourceFileSave):
            raise TypeError("原片关联申请必须使用 SourceFileSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_DECIDED_EVENT, _DECIDED_SOURCE_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["source_device_file_id"] is not None:
            raise ConsistencyError("可靠原片已关联，不重复关联")
        file = self._load_row(
            connection, "device_files", request.source_device_file_id)
        if file["role"] != int(_FILE_ROLE.ORIGINAL):
            raise ConsistencyError(
                f"关联文件不是原片角色: {file['role']!r}")
        if file["completion_state"] != int(_FILE_COMPLETION.COMPLETE):
            raise ConsistencyError(
                f"关联文件尚未确认写完: {file['completion_state']!r}")
        if file["source_action_id"] != processing["action_id"]:
            raise ConsistencyError(
                f"关联文件属于其他动作: {file['source_action_id']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"source_device_file_id": processing["source_device_file_id"]},
            {"source_device_file_id": request.source_device_file_id},
        )
        return self._emit(
            scope, _RECORDING_DECIDED_EVENT, _DECIDED_SOURCE_REASON,
            row, request.occurred_at)


class _CheckResultCommand(_ProcessingCommand):
    """保存检查执行阶段及公共媒体观察；只有 REQUIRED 决定可执行。"""

    def __init__(self, request: CheckResultSave, key: OperationKey) -> None:
        if not isinstance(request, CheckResultSave):
            raise TypeError("检查结果申请必须使用 CheckResultSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_PROCESSED_EVENT, _PROCESSED_CHECK_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["check_decision"] != 3:
            raise ConsistencyError(
                f"检查决定未固定为需要检查: {processing['check_decision']!r}")
        if processing["check_state"] not in (1, 2):
            raise ConsistencyError(
                f"检查阶段已终结，不再保存结果: {processing['check_state']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"check_state": processing["check_state"],
             "media_json": processing["media_json"]},
            {"check_state": request.media.phase.value,
             "media_json": request.media.as_json()},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RECORDING_PROCESSED_EVENT, _PROCESSED_CHECK_REASON, (row,),
            request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))


class _RepairResultCommand(_ProcessingCommand):
    """保存修复执行阶段及结果；成功必须指向完整修复输出。"""

    def __init__(self, request: RepairResultSave, key: OperationKey) -> None:
        if not isinstance(request, RepairResultSave):
            raise TypeError("修复结果申请必须使用 RepairResultSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["repair_state"] not in (3, 4):
            raise ConsistencyError(
                f"修复尚未取得执行决定或已终结: {processing['repair_state']!r}")
        before: dict[str, Any] = {"repair_state": processing["repair_state"]}
        after: dict[str, Any] = {"repair_state": request.phase.value}
        if request.phase is RepairOutcome.SUCCEEDED:
            output = self._load_row(
                connection, "intermediate_files", request.output_file_id)
            if output["purpose"] != int(_FILE_PURPOSE.REPAIR_OUTPUT):
                raise ConsistencyError(
                    f"修复成功必须指向修复输出文件: {output['purpose']!r}")
            if output["owner_action_id"] != processing["action_id"]:
                raise ConsistencyError("修复输出属于其他动作")
            if output["size_bytes"] is None or output["sha256"] is None:
                raise ConsistencyError("修复输出缺少完整字节事实")
            before["repair_output_file_id"] = processing["repair_output_file_id"]
            after["repair_output_file_id"] = request.output_file_id
        elif request.phase is RepairOutcome.FAILED:
            before["repair_error_json"] = processing["repair_error_json"]
            after["repair_error_json"] = request.error.as_json()
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"], before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON, (row,),
            request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))


class _DiscardProgressCommand(_ProcessingCommand):
    """保存取消后文件处理收场进度；失败或未知保留实际错误。"""

    def __init__(self, request: DiscardProgressSave, key: OperationKey) -> None:
        if not isinstance(request, DiscardProgressSave):
            raise TypeError("收场进度申请必须使用 DiscardProgressSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_PROCESSED_EVENT, _PROCESSED_DISCARD_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        current = processing["discard_state"]
        allowed = _DISCARD_NEXT.get(current, frozenset())
        if request.phase.value not in allowed:
            raise ConsistencyError(
                f"收场进度不能从 {current!r} 推进到 {request.phase.value!r}")
        before: dict[str, Any] = {"discard_state": current}
        after: dict[str, Any] = {"discard_state": request.phase.value}
        if request.error is not None:
            before["discard_error_json"] = processing["discard_error_json"]
            after["discard_error_json"] = request.error.as_json()
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"], before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RECORDING_PROCESSED_EVENT, _PROCESSED_DISCARD_REASON, (row,),
            request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))


class _RepairStartCommand(_ProcessingCommand):
    """登记修复输出文件并进入修复执行；路径登记与运行阶段同事务。

    创建文件前先登记路径和责任；修复输出自创建起使用 derived/
    正式身份，保持同一文件身份和位置直到登记为正式产物。
    """

    def __init__(self, request: RepairStart, key: OperationKey) -> None:
        if not isinstance(request, RepairStart):
            raise TypeError("修复输出登记申请必须使用 RepairStart")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        processing = self._load_processing(connection, request.processing_id)
        if processing["repair_state"] != int(_REPAIR_STATE.PENDING):
            raise ConsistencyError(
                f"修复尚未取得待执行决定: {processing['repair_state']!r}")
        self._claim(processing)
        file_id = _next_id(connection, "intermediate_files")
        relative_path = relative_file_path(
            FilePurpose.REPAIR_OUTPUT, file_id, request.extension)
        file_row = _row(
            "intermediate_files",
            file_id,
            {
                "owner_action_id": processing["action_id"],
                "owner_delivery_id": None,
                "purpose": int(_PURPOSE.REPAIR_OUTPUT),
                "relative_path": relative_path,
                "retention_state": int(_RETENTION.REQUIRED),
                "cleanup_state": int(_FILE_CLEANUP.NOT_NEEDED),
                "size_bytes": None,
                "sha256": None,
                "last_error_json": None,
            },
        )
        processing_row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"]},
            {"repair_state": int(_REPAIR_STATE.RUNNING)},
        )
        self._owners[("intermediate_files", file_id)] = (
            "intermediate_file", file_id)
        allocation = scope.allocate(2)
        events = (
            _envelope(
                allocation.first_event_id, allocation.txn_id,
                _INTERMEDIATE_FILE_EVENT, 1, (file_row,), request.occurred_at),
            _envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON,
                (processing_row,), request.occurred_at),
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=RepairOutputFile(
                ProcessingDisposition.SAVED, file_id, relative_path))

    def _reuse(self, saved) -> CommandPlan:
        """原键恢复首次登记响应：文件身份与路径取自原事务正文。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, 1),
                     (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为修复输出登记重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("修复输出登记的事实时刻与原事务不同")
        created = saved[0]["body"]["rows"][0]
        if created["table"] != "intermediate_files":
            raise ConsistencyError("原事务的修复输出登记行不解释")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=RepairOutputFile(
                ProcessingDisposition.ALREADY, created["id"],
                created["after"]["values"]["relative_path"]))


class _RepairSuccessCommand(_ProcessingCommand):
    """保存修复输出完整字节并固定修复成功；字节与终态同事务。

    事件先保存字节事实再保存成功终态，守卫从同事务先行事件复核
    输出用途、归属与完整字节；重复提交按原键恢复首次响应。
    """

    def __init__(self, request: RepairSuccess, key: OperationKey) -> None:
        if not isinstance(request, RepairSuccess):
            raise TypeError("修复成功申请必须使用 RepairSuccess")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        processing = self._load_processing(connection, request.processing_id)
        if processing["repair_state"] != int(_REPAIR_STATE.RUNNING):
            raise ConsistencyError(
                f"修复不在执行中，不能固定成功: {processing['repair_state']!r}")
        output = self._load_row(
            connection, "intermediate_files", request.output_file_id)
        if output["purpose"] != int(_PURPOSE.REPAIR_OUTPUT):
            raise ConsistencyError(
                f"修复成功必须指向修复输出文件: {output['purpose']!r}")
        if output["owner_action_id"] != processing["action_id"]:
            raise ConsistencyError("修复输出属于其他动作")
        if output["size_bytes"] is not None or output["sha256"] is not None:
            raise ConsistencyError("修复输出已保存完整字节事实")
        self._claim(processing)
        self._owners[("intermediate_files", output["id"])] = (
            "intermediate_file", output["id"])
        file_row = _update(
            "intermediate_files", output["id"],
            {"size_bytes": output["size_bytes"], "sha256": output["sha256"]},
            {"size_bytes": request.size_bytes, "sha256": request.sha256},
        )
        processing_row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"],
             "repair_output_file_id": processing["repair_output_file_id"]},
            {"repair_state": int(_REPAIR_STATE.SUCCEEDED),
             "repair_output_file_id": request.output_file_id},
        )
        allocation = scope.allocate(2)
        events = (
            _envelope(
                allocation.first_event_id, allocation.txn_id,
                _INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON, (file_row,),
                request.occurred_at),
            _envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON,
                (processing_row,), request.occurred_at),
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))

    def _reuse(self, saved) -> CommandPlan:
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON),
                     (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为修复成功重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("修复成功的事实时刻与原事务不同")
        return self._decision(ProcessingDisposition.ALREADY, read_only=True)


class CaptureRepository:
    """采集完成终态事务的 SQLite 仓储。"""

    def save_emergency(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        owned: OwnedConnection,
        timeout_s=None,
        retry_interval_s=None,
    ) -> DbOutcome[EmergencySave]:
        command = SaveEmergencyCommand(
            session_key=session_key,
            action_id=action_id,
            activity_id=activity_id,
            record=record,
            attempts=attempts,
            occurred_at=occurred_at,
            key=key,
            timeout_s=timeout_s,
            retry_interval_s=retry_interval_s,
        )
        receipt = commit_operation(command, key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_capture(
        self, command: FinishCapture, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CaptureResult]:
        receipt = commit_operation(FinishCaptureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_check_decision(
        self, command: CheckDecisionSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_CheckDecisionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_repair_decision(
        self, command: RepairDecisionSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairDecisionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_source_file(
        self, command: SourceFileSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_SourceFileCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_check_result(
        self, command: CheckResultSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_CheckResultCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_repair_result(
        self, command: RepairResultSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairResultCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_discard_progress(
        self, command: DiscardProgressSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_DiscardProgressCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def start_repair_output(
        self, command: RepairStart, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[RepairOutputFile]:
        receipt = commit_operation(_RepairStartCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def complete_repair_output(
        self, command: RepairSuccess, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairSuccessCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


# -- 应急停止最终补记 -------------------------------------------------


@dataclass(frozen=True)
class EmergencySave:
    """应急补记的保存结果：持久化状态与原流程事实。"""

    record_status: RecordStatus
    run_id: int
    attempts_saved: int


class SaveEmergencyCommand:
    """一次应急停止最终补记的完整事务命令。

    一个目标的最终流程（kind=9）与全部实际尝试、适用设备活动变
    化在一个事务共同创建；进行中补记、普通意图引用、超限与缺项
    均拒绝。
    """

    def __init__(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        timeout_s=None,
        retry_interval_s=None,
    ) -> None:
        self._session_key = session_key
        self._timeout_s = timeout_s
        self._retry_interval_s = retry_interval_s
        self._action_id = action_id
        self._activity_id = activity_id
        self._record = record
        self._attempts = attempts
        self._occurred_at = occurred_at
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("应急补记的重送须按原事务核实")
        action = row_facts(connection, "actions", self._action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {self._action_id}")
        activity = row_facts(connection, "device_activities", self._activity_id)
        if activity is None:
            raise TransactionError(f"设备活动不存在: {self._activity_id}")
        self._state["device_activities"] = {self._activity_id: activity}
        self._state["actions"] = {self._action_id: action}
        self._state.setdefault("operation_runs", {})
        self._state.setdefault("operation_attempts", {})

        record = self._record
        if len(self._attempts) != record.attempts_used:
            raise TransactionError(
                f"补记尝试行数与实际次数不符: {len(self._attempts)}"
                f" != {record.attempts_used}"
            )
        if record.attempts_used > record.max_attempts:
            raise TransactionError("实际次数超过本会话固定限额")
        if record.attempts_used > 0 and not self._complete_config():
            raise TransactionError("已有尝试的补记要求完整配置")

        responsibility_key = (
            f"emergency/{self._session_key}/{self._activity_id}"
        )
        existed = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (responsibility_key,),
        ).fetchone()
        if existed is not None:
            raise TransactionError("同一会话对同一活动只有一条应急流程")

        if record.outcome is EmergencyOutcome.STOPPED:
            run_status = 3
            run_error = None
            activity_after = 3
            activity_error = None
        elif record.outcome is EmergencyOutcome.UNCONFIRMED:
            if record.attempts_used == 0:
                raise TransactionError("停止未确认且有尝试的补记要求次数大于 0")
            run_status = 6
            run_error = {"code": "emergency_stop_unconfirmed", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error
        else:
            if record.attempts_used != 0:
                raise TransactionError("未能尝试的补记不创建尝试行")
            run_status = 4
            run_error = {"code": "emergency_not_attempted", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error

        allocation = scope.allocate(1)
        first_id = allocation.first_event_id
        run_id = _next_id(connection, "operation_runs")
        owner = ("action", action["id"])
        self._owners[("operation_runs", run_id)] = owner

        run_values = {
            "action_id": self._action_id,
            "delivery_id": None,
            "kind": 9,
            "query_purpose": None,
            "responsibility_key": responsibility_key,
            "activity_id": self._activity_id,
            "copy_id": None,
            "cleanup_item_id": None,
            "session_key": self._session_key,
            "status": run_status,
            "attempts_used": record.attempts_used,
            "max_attempts_used": record.max_attempts,
            "timeout_s_json": self._timeout_s,
            "retry_interval_s_json": self._retry_interval_s,
            "retry_wait_required": 0,
            "error_json": run_error,
        }
        rows = [_row("operation_runs", run_id, run_values)]
        attempt_ids: list[int] = []
        next_attempt_id = _next_id(connection, "operation_attempts")
        for index, attempt in enumerate(self._attempts):
            attempt_id = next_attempt_id + index
            attempt_ids.append(attempt_id)
            self._owners[("operation_attempts", attempt_id)] = owner
            values = dict(attempt)
            values.update(
                {
                    "run_id": run_id,
                    "attempt_no": index + 1,
                    "copy_round": None,
                    "intent_event_id": None,
                    "result_event_id": first_id,
                    "max_attempts_used": record.max_attempts,
                    "timeout_s_json": self._timeout_s,
                    "retry_interval_s_json": self._retry_interval_s,
                }
            )
            rows.append(_row("operation_attempts", attempt_id, values))
        before = {"activity_state": activity["activity_state"],
                  "last_error_json": activity["last_error_json"]}
        after = {"activity_state": activity_after, "last_error_json": activity_error}
        if not json_equal(before, after):
            self._owners[("device_activities", self._activity_id)] = owner
            rows.append(_update("device_activities", self._activity_id, before, after))
        event = _envelope(
            first_id,
            allocation.txn_id,
            _EMERGENCY_RECORDED_EVENT,
            1,
            tuple(rows),
            self._occurred_at,
            evidence={"session_key": self._session_key},
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=EmergencySave(
                record_status=RecordStatus.RECORDED,
                run_id=run_id,
                attempts_saved=record.attempts_used,
            ),
        )

    def _complete_config(self) -> bool:
        return self._timeout_s is not None and self._retry_interval_s is not None


def _emergency_guard(event, context) -> None:
    """应急补记的组合守卫：责任键、尝试归属、次数与结果组合。"""
    session_key = event.evidence.get("session_key")
    if not isinstance(session_key, str) or len(session_key) != 32:
        raise EventValidationError("应急补记必须携带本会话身份")
    run_values = None
    run_id = None
    attempts: list[dict] = []
    for row in event.rows:
        if row.table == "operation_runs" and not row.before.exists:
            run_values = row.after.values
            run_id = row.row_id
        elif row.table == "operation_attempts" and not row.before.exists:
            attempts.append(row.after.values)
    if run_values is None:
        raise EventValidationError("应急补记缺少最终流程行")
    expected_key = (
        f"emergency/{session_key}/{run_values.get('activity_id')}"
    )
    if run_values.get("responsibility_key") != expected_key:
        raise EventValidationError("应急责任键与会话及活动不符")
    if run_values.get("kind") != 9 or run_values.get("retry_wait_required") != 0:
        raise EventValidationError("应急流程必须是补记终态")
    status = run_values.get("status")
    if status not in (3, 4, 6):
        raise EventValidationError("应急补记不保存进行中状态")
    if status == 3 and run_values.get("error_json") is not None:
        raise EventValidationError("应急成功不携带流程错误")
    if status in (4, 6) and run_values.get("error_json") is None:
        raise EventValidationError("应急失败或未确认必须携带原因")
    if status == 6 and not attempts:
        raise EventValidationError("停止未确认的补记必须有实际尝试")
    if status == 4 and attempts:
        raise EventValidationError("未能尝试的补记不创建尝试行")
    numbers = [a.get("attempt_no") for a in attempts]
    if numbers != list(range(1, len(attempts) + 1)):
        raise EventValidationError("应急尝试必须从 1 连续编号")
    if run_values.get("attempts_used") != len(attempts):
        raise EventValidationError("累计次数与尝试行数不符")
    maximum = run_values.get("max_attempts_used")
    if maximum is not None and len(attempts) > maximum:
        raise EventValidationError("尝试次数超过本会话固定限额")
    for attempt in attempts:
        if attempt.get("run_id") != run_id:
            raise EventValidationError("应急尝试必须归属本次流程")
        if attempt.get("intent_event_id") is not None:
            raise EventValidationError("应急尝试不携带普通意图引用")
        if attempt.get("result_event_id") != event.event_id:
            raise EventValidationError("应急尝试结果必须指向本事件")
        if attempt.get("status") == 1:
            raise EventValidationError("应急补记不保存运行中尝试")
        if attempt.get("copy_round") is not None:
            raise EventValidationError("应急尝试没有拷贝轮次")


def _activity_guard(event, context) -> None:
    """活动状态守卫：结束不由路径、超时或本地退出补造。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        before_state = row.before.values.get("activity_state")
        after_state = row.after.values.get("activity_state")
        if after_state == 3 and before_state != 3:
            # 活动结束必须由本事务的可靠停止事实承载；同事件创建的
            # 最终流程行即为该事实。
            stopped = any(
                row.table == "operation_runs"
                and not row.before.exists
                and row.after.values.get("status") == 3
                for row in event.rows
            )
            runs = context.state_rows.get("operation_runs", {})
            stopped = stopped or any(
                values.get("status") == 3 for values in runs.values()
            )
            if not stopped:
                raise EventValidationError("活动结束缺少可靠停止事实")


def _release_guard(event, context) -> None:
    """占用释放守卫：ENDED 仍可保持 HELD，释放要求活动已结束。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        if (
            row.after.values.get("occupancy_state") == 2
            and row.before.values.get("occupancy_state") == 1
        ):
            facts = dict(
                context.state_rows.get("device_activities", {})
                .get(row.row_id, {})
            )
            facts.update(row.after.values)
            if facts.get("activity_state") != 3:
                raise EventValidationError("占用释放要求活动已经结束")
