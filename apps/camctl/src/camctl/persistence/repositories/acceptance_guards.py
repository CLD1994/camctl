"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.acceptance.storage_rules import AcceptanceStorageRuleError, validate_admitted_action, validate_initial_plan_status, validate_fixed_source, validate_admitted_source_owners, validate_admitted_source_owner_type, validate_admitted_source_member, validate_diagnostic_key, validate_diagnostic_errors, validate_acknowledged_sync_status, validate_acknowledged_sync, validate_acknowledged_watermark
from camctl.contracts.enums import enum_for
from camctl.history.validators import EventValidationError
from camctl.persistence.atomic import TransactionError


def _action_type_code(literal: str) -> int:
    members = enum_for("actions.type")
    for member in members:
        if member.name.lower() == literal:
            return int(member.value)
    raise TransactionError(f"未知动作类型: {literal!r}")


def _admission_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "actions" or row.before.exists:
            continue
        try:
            validate_admitted_action(row.after.values)
        except ValueError as error:
            raise EventValidationError(str(error)) from error


def _plan_aggregate_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "plans" or row.before.exists:
            continue
        plan_id = row.row_id
        statuses = tuple(
            values.get("status")
            for values in context.state_rows.get("actions", {}).values()
            if values.get("plan_id") == plan_id
        )
        try:
            validate_initial_plan_status(row.after.values.get("status"), statuses)
        except ValueError as error:
            raise EventValidationError(str(error)) from error


def _acceptance_rule(rule, *args):
    try:
        return rule(*args)
    except AcceptanceStorageRuleError as error:
        message, cause = str(error), error.__cause__
    raise EventValidationError(message) from cause


def _source_members_guard(event, context) -> None:
    if event.event_type == 3 and event.reason in (1, 2):
        from .source_transactions import verify_source_guard
        verify_source_guard(event, context)
        return
    for row in event.rows:
        if row.table == "actions" and not row.before.exists:
            _acceptance_rule(validate_fixed_source, row.after.values)
    dependencies = [row for row in event.rows if row.table == "action_dependencies" and not row.before.exists]
    reads = context.current_reads
    owner_id = _acceptance_rule(validate_admitted_source_owners,
        tuple(row.after.values.get("action_id") for row in dependencies),
        event.event_type, event.reason, reads is not None)
    if not dependencies:
        return
    owner = reads.admitted_source_owner(owner_id, context.transaction)
    _acceptance_rule(validate_admitted_source_owner_type, owner["type"], frozenset({
        _action_type_code("obtain_action_outputs"), _action_type_code("delete_action_outputs")}))
    from itertools import zip_longest
    actual = (row.after.values.get("depends_on_action_id") for row in dependencies)
    for member, expected in zip_longest(actual, reads.admitted_source_members(owner,context.transaction)):
        _acceptance_rule(validate_admitted_source_member, member, expected)


def _diagnostic_guard(event, context) -> None:
    _acceptance_rule(validate_diagnostic_key, "input_key" in event.evidence)
    for row in event.rows:
        if row.table != "plan_file_diagnostics" or row.before.exists:
            continue
        _acceptance_rule(validate_diagnostic_errors, row.after.values.get("errors_json"))


def _ack_guard(event, context) -> None:
    for row in event.rows:
        if row.table == "state_syncs":
            _acceptance_rule(validate_acknowledged_sync_status, row.after.values,
                int(enum_for("state_syncs.status").ACKNOWLEDGED))
            values = context.state_rows.get("state_syncs", {}).get(row.row_id)
            if values is None:
                raise EventValidationError("ACK 同步结束缺少原责任依据")
            report_id = row.after.values.get("ack_report_id")
            report = context.state_rows.get("reports", {}).get(report_id)
            if report is None:
                raise EventValidationError("同步确认必须引用实际报告")
            _acceptance_rule(validate_acknowledged_sync, row.row_id, event.event_id,
                row.after.values, values, report)
            continue
        if row.table != "runtime_state" or not row.before.exists:
            continue
        report_id = row.after.values.get("acknowledged_report_id")
        report = context.state_rows.get("reports", {}).get(report_id)
        _acceptance_rule(validate_acknowledged_watermark, row.before.values, row.after.values, report)


def register_acceptance_guards(register) -> None:
    """注册受理事务的正式业务守卫（装配期调用，替换测试替身）。"""
    register("admission", _admission_guard)
    register("plan_aggregate", _plan_aggregate_guard)
    register("source_members", _source_members_guard)
    register("diagnostic", _diagnostic_guard)
    register("ack", _ack_guard)
    from camctl.reporting.policy import register_sync_guard

    register_sync_guard(register)
