"""请求复用、注册、独立 ACK 及诊断登记的唯一事务。

经 P3 事务内核组织：事务内权威查请求，已存在立即复用并跳过本
文校验；首次受理按完整规则注册或拒绝，ACK 独立判定并单调吸收。
正式业务守卫（admission、plan_aggregate、source_members、
diagnostic、ack）在本模块注册，替换各测试替身。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from camctl.acceptance.input import InputDiagnostic, InputStage, ParsedInput
from camctl.acceptance.links import PlanIdentities, prepare_plan
from camctl.acceptance.definitions import read_action_spec
from camctl.acceptance.rules import extract_request_identity, validate_new_body
from camctl.acceptance.service import (
    AcceptanceResult,
    AckDisposition,
    CommandMode,
    PlanDisposition,
    ProcessInput,
)
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.json_values import MISSING, json_field
from camctl.contracts.values import OperationKey, parse_object_id, ValueTypeError, ValueFormatError, ValueRangeError
from camctl.contracts.workflow_errors import action_error_id, action_error_ids, registered_error_spec, validate_error_details
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.repositories.reporting import (
    read_ack_report, read_ack_state, read_outstanding_syncs,
)
from camctl.reporting.ack import (
    AckFacts, AckInput, AckReport, SyncResponsibility,
    AckDisposition as ReportAckDisposition, decide_ack, qualifies_sync,
)
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
)

_OBTAIN_TYPE = "obtain_action_outputs"
_ACK_EVENT = 30
_SYNC_EVENT = 29
_PLAN_EVENT = 1
_ACTION_EVENT = 2
_MEMBERS_EVENT = 3
_DIAGNOSTIC_EVENT = 27

#: 能产生正式产物的拍摄动作类型（成员名来自登记）。
_CAPTURE_TYPES = frozenset(
    int(member.value)
    for member in enum_for("actions.type")
    if member.name in {"CAMERA_TAKE_PHOTO", "CAMERA_RECORD", "CAMERA_TIMELAPSE"}
)


def _action_type_code(literal: str) -> int:
    members = enum_for("actions.type")
    for member in members:
        if member.name.lower() == literal:
            return int(member.value)
    raise TransactionError(f"未知动作类型: {literal!r}")


def _row(table: str, row_id: int, values: dict) -> RowChange:
    return RowChange(
        table=table,
        row_id=row_id,
        before=RowImage(exists=False, values={}),
        after=RowImage(exists=True, values=values),
    )


def _update(table: str, row_id: int, before: dict, after: dict) -> RowChange:
    return RowChange(
        table=table,
        row_id=row_id,
        before=RowImage(exists=True, values=before),
        after=RowImage(exists=True, values=after),
    )


def _envelope(
    event_id: int, txn_id: int, event_type: int, reason: int, rows, occurred_at: int
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        transaction_id=txn_id,
        event_type=event_type,
        event_version=1,
        occurred_at=occurred_at,
        clock_status=2,
        change_seq=None,
        reason=reason,
        evidence={},
        rows=tuple(rows),
    )


@dataclass(frozen=True)
class _AckEvaluation:
    disposition: AckDisposition
    watermark_after: int
    before: tuple[int, Any]
    after: tuple[int, int] | None
    error_entry: dict | None
    report: AckReport | None = None


def _evaluate_ack(connection, document: Mapping[str, Any] | None) -> _AckEvaluation:
    current_wm, current_report, cumulative_report = read_ack_state(connection)
    if document is None or not isinstance(document, Mapping):
        return _AckEvaluation(
            AckDisposition.NOT_PROCESSED, current_wm, (current_wm, current_report), None, None
        )
    raw = json_field(document, "last_report_id")
    if raw is MISSING:
        return _AckEvaluation(
            AckDisposition.NOT_PROVIDED, current_wm, (current_wm, current_report), None, None
        )
    try:
        report_id = parse_object_id(raw)
    except (ValueTypeError, ValueFormatError, ValueRangeError) as error:
        reason = "type" if isinstance(error, ValueTypeError) else "range" if isinstance(error, ValueRangeError) else "format"
        return _AckEvaluation(
            AckDisposition.INVALID, current_wm, (current_wm, current_report), None,
            {"stage": "ack", "code": "invalid_ack", "details": {
                "field": "last_report_id", "reason": reason, "value": raw,
            }},
        )
    # 判定规则由 reporting.ack 单一定义（R6）；本仓储只取事实并落地。
    report = cumulative_report if report_id == current_report else read_ack_report(connection, report_id)
    decision = decide_ack(
        AckInput(report_id=report_id),
        AckFacts(
            acknowledged_wm=current_wm,
            acknowledged_report_id=current_report,
            report=report,
        ),
    )
    if decision.disposition is ReportAckDisposition.INVALID:
        return _AckEvaluation(
            AckDisposition.INVALID,
            current_wm,
            (current_wm, current_report),
            None,
            {"stage": "ack", "code": "invalid_ack", "details": {"field": "last_report_id", "reason": "reference", "value": raw}},
        )
    if decision.disposition is ReportAckDisposition.ABSORBED:
        return _AckEvaluation(
            AckDisposition.ABSORBED,
            decision.new_acknowledged_wm,
            (current_wm, current_report),
            (decision.new_acknowledged_wm, report_id),
            None,
            report,
        )
    return _AckEvaluation(
        AckDisposition.VALID_NOT_ADVANCING,
        current_wm,
        (current_wm, current_report),
        None,
        None,
        report,
    )


class ProcessInputCommand:
    """一次输入处理的完整事务命令。"""

    def __init__(self, command: ProcessInput) -> None:
        self._command = command
        self._catalog = command.catalog
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "plans": {},
            "actions": {},
            "auto_preview_links": {},
            "action_dependencies": {},
            "plan_file_diagnostics": {},
        }
        self._ack: _AckEvaluation | None = None

    def plan(self, scope) -> CommandPlan:
        if self._command.mode is CommandMode.SUBMIT:
            if self._command.submit_handoff is None:
                raise TransactionError("submit 必须在输入事务内提供接管判断")
            return replace(self._plan_input(scope), complete_result=self._complete_handoff)
        if (self._command.mode is not CommandMode.RUN
                or self._command.submit_handoff is not None):
            raise TransactionError("输入命令模式与接管端口不符")
        return self._plan_input(scope)

    def _complete_handoff(self, connection, result: AcceptanceResult) -> AcceptanceResult:
        needs_run = self._command.submit_handoff.needs_run(connection)
        if type(needs_run) is not bool:
            raise TransactionError("接管判断必须返回布尔值")
        return replace(result, needs_run=needs_run)

    def _plan_input(self, scope) -> CommandPlan:
        connection = scope.connection
        occurred_at = self._command.occurred_at
        document = (
            self._command.source.document
            if isinstance(self._command.source, ParsedInput)
            else None
        )

        ack = _evaluate_ack(connection, document if document is not None else None)
        self._ack = ack
        self._state["runtime_state"] = {
            1: dict(zip(("acknowledged_wm", "acknowledged_report_id"), ack.before))
        }
        if ack.report is not None:
            report = ack.report
            self._state["reports"] = {
                report.report_id: {"id": report.report_id, "from_wm": report.from_wm,
                                   "to_wm": report.to_wm, "frozen_event_id": report.frozen_event_id}
            }

        if isinstance(self._command.source, InputDiagnostic):
            return self._diagnostic_only(scope, occurred_at, ack)

        identity = extract_request_identity(document)
        if identity.request_id is None:
            errors = [identity.error]
            if ack.error_entry is not None:
                errors.append(ack.error_entry)
            return self._diagnostic_plan(scope, occurred_at, None, None, errors)
        request_id = identity.request_id
        existing = connection.execute(
            "SELECT id FROM plans WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing is not None:
            return self._reuse(scope, occurred_at, ack, int(existing[0]), request_id)

        body = {key: value for key, value in document.items() if key != "last_report_id"}
        decision = validate_new_body(body, self._catalog)
        if decision.is_whole_rejection:
            return self._rejected(scope, occurred_at, ack, request_id, decision)
        return self._register(scope, occurred_at, ack, request_id, decision)

    # -- 各分区 -------------------------------------------------------

    def _ack_event(self, event_id: int, txn_id: int, occurred_at: int):
        assert self._ack is not None and self._ack.after is not None
        before_wm, before_report = self._ack.before
        after_wm, after_report = self._ack.after
        row = _update(
            "runtime_state",
            1,
            {"acknowledged_wm": before_wm, "acknowledged_report_id": before_report},
            {"acknowledged_wm": after_wm, "acknowledged_report_id": after_report},
        )
        self._owners[("runtime_state", 1)] = ("runtime_state", 1)
        return _envelope(event_id, txn_id, _ACK_EVENT, 1, (row,), occurred_at)

    def _reuse(self, scope, occurred_at: int, ack: _AckEvaluation, plan_id: int, request_id: int) -> CommandPlan:
        result = AcceptanceResult(
            plan_disposition=PlanDisposition.REUSED,
            plan_id=plan_id,
            ack_disposition=ack.disposition,
            ack_watermark=ack.watermark_after,
        )
        templates: list = []
        diagnostic_id: int | None = None
        if ack.error_entry is not None:
            row, diagnostic_id = self._diagnostic_row(
                scope.connection,
                [ack.error_entry],
                request_id,
                plan_id,
            )
            templates.append(
                replace(
                    _envelope(0, 0, _DIAGNOSTIC_EVENT, 1, (row,), occurred_at),
                    evidence={"input_key": uuid.uuid4().hex},
                )
            )
        return self._finalize(scope, templates, replace(result, diagnostic_id=diagnostic_id))

    def _diagnostic_row(
        self, connection, errors: list, request_id: int | None, plan_id: int | None
    ) -> tuple[RowChange, int]:
        """构造一条输入诊断行（分配身份并登记归属）。"""
        diagnostic_id = self._next_id(connection, "plan_file_diagnostics")
        row = _row(
            "plan_file_diagnostics",
            diagnostic_id,
            {
                "input_path": self._command.source.path,
                "request_id": request_id,
                "plan_id": plan_id,
                "errors_json": errors,
            },
        )
        self._owners[("plan_file_diagnostics", diagnostic_id)] = (
            "diagnostic",
            diagnostic_id,
        )
        return row, diagnostic_id

    def _diagnostic_only(self, scope, occurred_at: int, ack: _AckEvaluation) -> CommandPlan:
        source = self._command.source
        assert isinstance(source, InputDiagnostic)
        is_read = source.stage in {InputStage.OPEN, InputStage.READ}
        details = {"path":source.path, "message":source.detail}
        if is_read:
            details["operation"] = source.stage.value
        errors = [{
            "stage": "input_read" if is_read else "input_parse",
            "code": "plan_file_read_failed" if is_read else "invalid_encoding" if source.stage == InputStage.DECODE else "invalid_json",
            "details": details,
        }]
        # 读取或解析失败不吸收 ACK，也不从片段取得请求身份。
        return self._diagnostic_plan(scope, occurred_at, None, None, errors)

    def _rejected(
        self, scope, occurred_at: int, ack: _AckEvaluation, request_id: int, decision
    ) -> CommandPlan:
        errors = [
            {"stage": "admission", "code": "plan_body_rejected", "details": {"reasons": list(decision.rejection_reasons)}}
        ]
        if ack.error_entry is not None:
            errors.append(ack.error_entry)
        return self._diagnostic_plan(scope, occurred_at, request_id, None, errors)

    def _diagnostic_plan(
        self, scope, occurred_at: int, request_id: int | None, plan_id: int | None, errors: list
    ) -> CommandPlan:
        row, diagnostic_id = self._diagnostic_row(
            scope.connection, errors, request_id, plan_id
        )
        templates = [
            replace(
                _envelope(0, 0, _DIAGNOSTIC_EVENT, 1, (row,), occurred_at),
                evidence={"input_key": uuid.uuid4().hex},
            )
        ]
        result = AcceptanceResult(
            plan_disposition=PlanDisposition.REJECTED,
            plan_id=None,
            ack_disposition=self._ack.disposition if self._ack else AckDisposition.NOT_PROVIDED,
            ack_watermark=self._ack.watermark_after if self._ack else 0,
            diagnostic_id=diagnostic_id,
        )
        return self._finalize(scope, templates, result)

    def _register(self, scope, occurred_at: int, ack: _AckEvaluation, request_id: int, decision) -> CommandPlan:
        connection = scope.connection
        plan_id = self._next_id(connection, "plans")
        action_id_base = self._next_id(connection, "actions")
        prepared = prepare_plan(
            decision,
            PlanIdentities(
                plan_id=plan_id,
                action_ids={
                    action.name: action_id_base + index
                    for index, action in enumerate(decision.actions)
                },
            ),
        )

        templates: list[EventEnvelope] = []
        dep_id = self._next_id(connection, "action_dependencies")
        link_id = self._next_id(connection, "auto_preview_links")

        for action in prepared.actions:
            rows = [self._actions_row(action, plan_id)]
            if action.auto_preview_source is not None:
                link_values = {
                    "obtain_action_id": action.action_id,
                    "source_action_id": action.auto_preview_source_id,
                    "preview_support": int(action.preview_support) if action.preview_support is not None else None,
                    "parameter_type": action.source_parameter_type,
                    "is_valid": int(action.ok),
                }
                rows.append(_row("auto_preview_links", link_id, link_values))
                self._owners[("auto_preview_links", link_id)] = ("action", action.action_id)
                link_id += 1
            templates.append(
                _envelope(
                    0,
                    0,
                    _ACTION_EVENT,
                    1 if action.ok else 2,
                    rows,
                    occurred_at,
                )
            )

        for action in prepared.actions:
            if not action.in_plan_dependencies or not action.ok:
                continue
            member_rows = []
            for source_name in action.in_plan_dependencies:
                member_rows.append(
                    _row(
                        "action_dependencies",
                        dep_id,
                        {
                            "action_id": action.action_id,
                            "depends_on_action_id": prepared_action_id(prepared, source_name),
                        },
                    )
                )
                self._owners[("action_dependencies", dep_id)] = ("action", action.action_id)
                dep_id += 1
            templates.append(
                _envelope(0, 0, _MEMBERS_EVENT, 3, member_rows, occurred_at)
            )

        all_failed = bool(prepared.actions) and all(not action.ok for action in prepared.actions)
        plan_row = _row(
            "plans",
            plan_id,
            {
                "request_id": request_id,
                "name": prepared.name,
                "created_at": prepared.created_at_micros,
                "status": 3 if all_failed else 1,
            },
        )
        self._owners[("plans", plan_id)] = ("plan", plan_id)
        templates.append(
            _envelope(0, 0, _PLAN_EVENT, 1, (plan_row,), occurred_at)
        )

        diagnostic_id: int | None = None
        if ack.error_entry is not None:
            # 计划正常注册，ACK 错误独立保存为诊断。
            row, diagnostic_id = self._diagnostic_row(
                scope.connection, [ack.error_entry], request_id, plan_id
            )
            templates.append(
                replace(
                    _envelope(0, 0, _DIAGNOSTIC_EVENT, 1, (row,), occurred_at),
                    evidence={"input_key": uuid.uuid4().hex},
                )
            )

        result = AcceptanceResult(
            plan_disposition=PlanDisposition.REGISTERED,
            plan_id=plan_id,
            ack_disposition=ack.disposition,
            ack_watermark=ack.watermark_after,
            diagnostic_id=diagnostic_id,
        )
        return self._finalize(scope, templates, result)

    def _finalize(
        self, scope, templates: list, result: AcceptanceResult
    ) -> CommandPlan:
        ack = self._ack
        matched = []
        if ack is not None and ack.report is not None:
            if ack.after is not None:
                templates.append(self._ack_event(0, 0, self._command.occurred_at))
            for sync, values in read_outstanding_syncs(scope.connection):
                if qualifies_sync(ack.report, sync):
                    matched.append((sync, values))
                    self._state.setdefault("state_syncs", {})[sync.sync_id] = values
                    self._owners[("state_syncs", sync.sync_id)] = ("state_sync", sync.sync_id)
        count = len(templates) + len(matched)
        if count == 0:
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                               result=result, read_only=True)
        allocation = scope.allocate(count)
        events = list(self._sequenced(templates, allocation))
        for index, (sync, values) in enumerate(matched, start=len(templates)):
            event_id = allocation.first_event_id + index
            columns = ("status", "ack_report_id", "ended_event_id")
            row = _update("state_syncs", sync.sync_id,
                          {column: values[column] for column in columns},
                          {"status": int(enum_for("state_syncs.status").ACKNOWLEDGED),
                           "ack_report_id": ack.report.report_id, "ended_event_id": event_id})
            events.append(_envelope(event_id, allocation.txn_id, _SYNC_EVENT, 3, (row,),
                                    self._command.occurred_at))
        return CommandPlan(
            events=tuple(events),
            owners=self._owners,
            state_rows=self._state,
            result=result,
        )

    @staticmethod
    def _sequenced(templates: list, allocation) -> tuple:
        return tuple(
            replace(
                template,
                event_id=allocation.first_event_id + index,
                transaction_id=allocation.txn_id,
            )
            for index, template in enumerate(templates)
        )

    def _actions_row(self, action, plan_id: int) -> RowChange:
        raw = action.raw_fields
        literal = action.action_type
        values: dict[str, Any] = {
            "plan_id": plan_id,
            "input_index": action.input_index,
            "name": action.name,
            "type": _action_type_code(literal),
            "device_id": action.device_id,
            "scheduled_at": action.scheduled_at_micros,
            "group_name": action.group_name,
            "input_fields_json": raw,
            "driver_id": None,
            "max_delay_ms": action.max_delay_ms,
            "target_selection_state": int(enum_for("actions.target_selection_state").PENDING)
                if action.ok and literal in {"delete_action_outputs", "cancel_task"} else None,
            "execution_started": 0,
            "cancel_requested": 0,
            "first_window_observed_at": None,
            "expiration_reason": None,
        }
        if action.ok:
            driver = action.driver_id
            effective = action.effective_params
            values.update(
                {
                    "status": 1,
                    "error_code": None,
                    "error_details_json": None,
                    "effective_params_json": effective,
                    "driver_id": driver,
                    "execution_spec_json": action.execution_spec,
                    "source_resolution_state": action.source_resolution_state,
                    "resolved_source_plan_id": action.source_plan_id,
                }
            )
        else:
            code = action_error_id(action.failure_code or "action_validation_failed")
            values.update(
                {
                    "status": 4,
                    "error_code": code,
                    "error_details_json": _failure_details(action),
                    "effective_params_json": None,
                    "execution_spec_json": None,
                    "source_resolution_state": None,
                    "resolved_source_plan_id": None,
                }
            )
        self._owners[("actions", action.action_id)] = ("action", action.action_id)
        return _row("actions", action.action_id, values)

    @staticmethod
    def _next_id(connection, table: str) -> int:
        row = connection.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        return (int(row[0]) if row[0] is not None else 0) + 1


def prepared_action_id(prepared, name: str) -> int:
    for action in prepared.actions:
        if action.name == name:
            return action.action_id
    raise TransactionError(f"计划内不存在动作: {name!r}")


def _failure_details(action) -> dict:
    return action.failure_details


class AcceptanceRepository:
    """输入处理的 SQLite 仓储：唯一事务经 P3 内核提交。"""

    def process_input(
        self,
        command: ProcessInput,
        key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[AcceptanceResult]:
        receipt = commit_operation(ProcessInputCommand(command), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


# -- 正式业务守卫 -----------------------------------------------------


def _admission_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "actions" or row.before.exists:
            continue
        values = row.after.values
        try:
            read_action_spec(values)
        except ValueError as error:
            raise EventValidationError(str(error)) from error
        status = values.get("status")
        if status == 4:
            if values.get("execution_started") != 0 or values.get("error_code") is None:
                raise EventValidationError(
                    "受理失败动作必须 execution_started=0 且携带 admission 错误"
                )
            if values.get("execution_spec_json") is not None:
                raise EventValidationError("受理失败动作不携带执行定义")
            try:
                code, spec = registered_error_spec("action_error_id", values["error_code"])
                if spec["stage"] != "admission":
                    raise ValueError("首次受理失败必须使用受理阶段错误")
                validate_error_details(code, values["error_details_json"])
            except (TypeError, ValueError, KeyError) as error:
                raise EventValidationError(str(error)) from error
        elif status == 1:
            if values.get("execution_spec_json") is None:
                raise EventValidationError("可受理动作必须携带执行定义")


def _plan_aggregate_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "plans" or row.before.exists:
            continue
        plan_id = row.row_id
        actions = [
            values
            for values in context.state_rows.get("actions", {}).values()
            if values.get("plan_id") == plan_id
        ]
        statuses = {values.get("status") for values in actions}
        expected = 3 if (actions and statuses == {4}) else 1
        if row.after.values.get("status") != expected:
            raise EventValidationError(
                f"计划状态与动作聚合不符: 期望 {expected}"
            )


def _source_members_guard(event, context) -> None:
    fixed_now: dict[int, Any] = {}
    for row in event.rows:
        if row.table == "actions" and row.before.exists:
            after = row.after.values
            if after.get("source_resolution_state") == 2:
                fixed_now[row.row_id] = after.get("resolved_source_plan_id")
    for row in event.rows:
        if row.table == "actions" and not row.before.exists:
            values = row.after.values
            if values.get("source_resolution_state") == 2:
                if values.get("resolved_source_plan_id") is None:
                    raise EventValidationError("FIXED 来源必须保存真实来源计划")
        if row.table == "action_dependencies" and not row.before.exists:
            owner_id = row.after.values.get("action_id")
            owner = context.state_rows.get("actions", {}).get(owner_id)
            # 执行期固定在同一事件把所属动作更新为 FIXED；受理成员
            # 则要求先前事件已创建 FIXED 动作。
            if owner_id not in fixed_now and (
                owner is None or owner.get("source_resolution_state") != 2
            ):
                raise EventValidationError("来源成员必须属于 FIXED 来源的取回或范围清理动作")
            if owner is None or owner.get("type") not in {_action_type_code("obtain_action_outputs"), _action_type_code("delete_action_outputs")} :
                raise EventValidationError("来源成员所属动作类型不适用")
            plan_id = fixed_now.get(owner_id) or (owner or {}).get(
                "resolved_source_plan_id"
            )
            member = context.state_rows.get("actions", {}).get(
                row.after.values.get("depends_on_action_id")
            )
            if (
                plan_id is None
                or member is None
                or member.get("plan_id") != plan_id
                or member.get("type") not in _CAPTURE_TYPES
            ):
                raise EventValidationError(
                    "来源成员必须是指向来源计划且能产生产物的拍摄动作:"
                    f" {row.after.values.get('depends_on_action_id')!r}"
                )


def _diagnostic_guard(event, context) -> None:
    if "input_key" not in event.evidence:
        raise EventValidationError("输入诊断必须携带 input_key 依据")
    for row in event.rows:
        if row.table != "plan_file_diagnostics" or row.before.exists:
            continue
        errors = row.after.values.get("errors_json")
        if not isinstance(errors, list) or not errors:
            raise EventValidationError("输入诊断必须保存非空错误列表")


def _ack_guard(event, context) -> None:
    for row in event.rows:
        if row.table == "state_syncs":
            if row.after.values.get("status") != int(enum_for("state_syncs.status").ACKNOWLEDGED):
                raise EventValidationError("ACK 同步结束必须保存确认状态")
            values = context.state_rows.get("state_syncs", {}).get(row.row_id)
            if values is None:
                raise EventValidationError("ACK 同步结束缺少原责任依据")
            report_id = row.after.values.get("ack_report_id")
            report = context.state_rows.get("reports", {}).get(report_id)
            if report is None:
                raise EventValidationError("同步确认必须引用实际报告")
            try:
                basis = AckReport(report_id, report["from_wm"], report["to_wm"], report["frozen_event_id"])
                sync = SyncResponsibility(row.row_id, values["action_id"], values["from_wm"],
                                          values["started_boundary_event_id"])
            except (ValueError, KeyError) as error:
                raise EventValidationError("同步确认依据无法可靠解释") from error
            if not qualifies_sync(basis, sync):
                raise EventValidationError("ACK 报告未满足同步起点与开始历史边界")
            if row.after.values.get("ended_event_id") != event.event_id:
                raise EventValidationError("同步结束依据必须是本事件")
            continue
        if row.table != "runtime_state" or not row.before.exists:
            continue
        after = row.after.values
        report_id = after.get("acknowledged_report_id")
        new_wm = after.get("acknowledged_wm")
        report = context.state_rows.get("reports", {}).get(report_id)
        if report is None:
            raise EventValidationError(f"吸收的 ACK 引用不存在的报告: {report_id!r}")
        if report.get("to_wm") != new_wm:
            raise EventValidationError("累计水位必须等于所吸收报告的覆盖终点")
        if not row.before.values.get("acknowledged_wm", 0) < new_wm:
            raise EventValidationError("累计确认水位只严格提高")


def register_acceptance_guards() -> None:
    """注册受理事务的正式业务守卫（装配期调用，替换测试替身）。"""
    register_guard("admission", _admission_guard)
    register_guard("plan_aggregate", _plan_aggregate_guard)
    register_guard("source_members", _source_members_guard)
    register_guard("diagnostic", _diagnostic_guard)
    register_guard("ack", _ack_guard)
    from camctl.reporting.policy import register_sync_guard

    register_sync_guard()
