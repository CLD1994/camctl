"""终态清理成员的可靠缺席交接事务。"""

from camctl.contracts.values import ConsistencyError
from camctl.contracts.json_values import json_equal
from camctl.history.validators import EventValidationError
from camctl.operations.result_format import read_result_document
from camctl.outputs.cleanup_absence import HandoffCleanupAbsence
from camctl.outputs.cleanup_results import (
    _read_exact_cleanup_attempt,
    saved_query_presence,
)
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    row_facts,
    saved_transaction_events,
    update_change,
    event_envelope,
)


def _presence(actual, operation, item_id):
    if operation == "query":
        return saved_query_presence(actual, item_id)
    from camctl.outputs.cleanup_flow import _delete_presence

    return _delete_presence(actual, item_id)


class HandoffCleanupAbsenceCommand:
    def __init__(self, request, key):
        if not isinstance(request, HandoffCleanupAbsence):
            raise TypeError("缺席交接要求完整原申请")
        self.request, self.key = request, key

    def plan(self, scope):
        from camctl.outputs.cleanup_flow import CleanupItemSaved, CleanupItemDisposition

        c = scope.connection
        r = self.request
        original = _read_exact_cleanup_attempt(c, r.ticket, r.item_id)
        if (
            original.result_event_id != r.result_event_id
            or original.key != r.source_key
            or _presence(original.actual, r.ticket.operation, r.item_id) is not False
        ):
            raise ConsistencyError("缺席交接来源与原完整结果不符")
        item = row_facts(c, "cleanup_items", r.item_id)
        output = row_facts(c, "outputs", r.output_id)
        if (
            item is None
            or item["status"] not in (4, 5, 6)
            or item["output_id"] != r.output_id
            or output is None
            or output["device_file_id"] != r.device_file_id
            or output["intermediate_file_id"] != r.intermediate_file_id
        ):
            raise ConsistencyError("缺席交接的原成员、产物或文件身份不同")
        if c.execute(
            "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND (a.status=1 OR a.result_event_id IS NULL) LIMIT 1",
            (r.item_id,),
        ).fetchone():
            raise ConsistencyError("终态清理成员仍有未收场物理尝试")
        table, column, done = (
            ("device_files", "presence_state", 3)
            if r.device_file_id is not None
            else ("intermediate_files", "cleanup_state", 4)
        )
        file_id = r.device_file_id or r.intermediate_file_id
        file = row_facts(c, table, file_id)
        if file is None:
            raise ConsistencyError("缺席交接的承载文件不存在")
        attempt_id = c.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (r.ticket.run_id, r.ticket.attempt_id),
        ).fetchone()[0]
        state = {
            "cleanup_items": {r.item_id: item},
            "outputs": {r.output_id: output},
            table: {file_id: file},
            "operation_runs": {
                r.ticket.run_id: row_facts(c, "operation_runs", r.ticket.run_id)
            },
            "operation_attempts": {
                attempt_id: row_facts(c, "operation_attempts", attempt_id)
            },
        }
        owners = {
            ("outputs", r.output_id): ("output", r.output_id),
            (table, file_id): (
                "device_file" if table == "device_files" else "intermediate_file",
                file_id,
            ),
        }
        result = lambda disposition: CommandPlan(
            events=(),
            owners=owners,
            state_rows=state,
            read_only=True,
            result=CleanupItemSaved(disposition, r.item_id),
        )
        saved = saved_transaction_events(c, self.key)
        if saved is not None:
            self._reuse(saved, table, column, done, file_id)
            if (
                output["availability"] == 3
                and output["cleanup_status"] == 4
                and file[column] != done
            ):
                raise ConsistencyError("产物清理完成与承载文件事实矛盾")
            return result(CleanupItemDisposition.ALREADY)
        if (
            output["availability"] == 3
            and output["cleanup_status"] == 4
            and file[column] != done
        ):
            raise ConsistencyError("产物清理完成与承载文件事实矛盾")
        complete = (
            output["availability"] == 3
            and output["cleanup_status"] == 4
            and output["cleanup_error_json"] is None
        )
        if complete:
            if file[column] != done:
                raise ConsistencyError("产物清理完成与承载文件事实矛盾")
            return result(CleanupItemDisposition.ALREADY)
        specs = []
        if file[column] != done:
            specs.append(
                (
                    17 if table == "device_files" else 26,
                    5 if table == "device_files" else 4,
                    update_change(
                        table, file_id, {column: file[column]}, {column: done}
                    ),
                    {}
                    if table == "device_files"
                    else {"cleanup_request": {"advance_cursor": False}},
                )
            )
        desired = {"availability": 3, "cleanup_status": 4, "cleanup_error_json": None}
        before = {
            name: output[name]
            for name in desired
            if not json_equal(output[name], desired[name])
        }
        after = {name: desired[name] for name in before}
        # 必须有实际产物变化；无空事件或只更新文件的交接。
        if not before:
            raise ConsistencyError("缺席交接缺少实际产物变化")
        specs.append(
            (
                20,
                4,
                update_change("outputs", r.output_id, before, after),
                {
                    "cleanup_result_source": {
                        "item_id": r.item_id,
                        "result_event_id": r.result_event_id,
                    }
                },
            )
        )
        allocation = scope.allocate(len(specs))
        events = tuple(
            event_envelope(
                allocation.first_event_id + i,
                allocation.txn_id,
                t,
                reason,
                (row,),
                r.occurred_at,
                evidence=evidence,
            )
            for i, (t, reason, row, evidence) in enumerate(specs)
        )
        return CommandPlan(
            events=events,
            owners=owners,
            state_rows=state,
            result=CleanupItemSaved(CleanupItemDisposition.SAVED, r.item_id),
        )

    def _reuse(self, saved, table, column, done, file_id):
        r = self.request
        if len(saved) not in (1, 2) or any(
            e["occurred_at"] != r.occurred_at for e in saved
        ):
            raise TransactionError("原缺席交接的完整事件组或时刻不同")
        output = saved[-1]
        if (output["type"], output["reason"]) != (20, 4) or output["body"][
            "evidence"
        ] != {
            "cleanup_result_source": {
                "item_id": r.item_id,
                "result_event_id": r.result_event_id,
            }
        }:
            raise TransactionError("原缺席交接来源不同")
        rows = output["body"]["rows"]
        desired = {"availability": 3, "cleanup_status": 4, "cleanup_error_json": None}
        if (
            len(rows) != 1
            or rows[0]["table"] != "outputs"
            or rows[0]["id"] != r.output_id
            or not rows[0]["before"]["exists"]
            or not rows[0]["after"]["exists"]
            or not rows[0]["after"]["values"]
            or set(rows[0]["after"]["values"]) != set(rows[0]["before"]["values"])
            or any(
                name not in desired
                or not json_equal(value, desired[name])
                or json_equal(rows[0]["before"]["values"][name], value)
                for name, value in rows[0]["after"]["values"].items()
            )
        ):
            raise TransactionError("原缺席交接的产物实际变化不同")
        if len(saved) == 2:
            event = saved[0]
            row = event["body"]["rows"]
            evidence = (
                {}
                if table == "device_files"
                else {"cleanup_request": {"advance_cursor": False}}
            )
            if (
                (event["type"], event["reason"])
                != (
                    17 if table == "device_files" else 26,
                    5 if table == "device_files" else 4,
                )
                or event["body"]["evidence"] != evidence
                or len(row) != 1
                or row[0]["table"] != table
                or row[0]["id"] != file_id
                or not row[0]["before"]["exists"]
                or not row[0]["after"]["exists"]
                or row[0]["after"]["values"] != {column: done}
                or set(row[0]["before"]["values"]) != {column}
                or row[0]["before"]["values"][column] == done
            ):
                raise TransactionError("原缺席交接的承载文件实际变化不同")


def cleanup_result_source_guard(event, context):
    """来源观察只接受原可靠缺席和本事件前已保存的文件完成事实。"""
    source = event.evidence.get("cleanup_result_source")
    if source is None:
        return
    item_id, result_id = source["item_id"], source["result_event_id"]
    item = context.state_rows.get("cleanup_items", {}).get(item_id)
    if item is None or item.get("status") not in (4, 5, 6) or len(event.rows) != 1:
        raise EventValidationError("缺席来源观察必须属于原终态成员")
    row = event.rows[0]
    output = context.state_rows.get("outputs", {}).get(item.get("output_id"))
    if row.table != "outputs" or row.row_id != item.get("output_id") or output is None:
        raise EventValidationError("缺席来源观察与原产物不同")
    if set(row.after.values) - {"availability", "cleanup_status", "cleanup_error_json"}:
        raise EventValidationError("缺席来源观察只能改变清理完成投影")
    if result_id >= event.event_id:
        raise EventValidationError("缺席来源必须是本事件之前的可靠结果")
    values = dict(output)
    values.update(row.after.values)
    if (
        values.get("availability"),
        values.get("cleanup_status"),
        values.get("cleanup_error_json"),
    ) != (3, 4, None):
        raise EventValidationError("缺席来源观察必须保存产物清理完成")
    file = context.state_rows.get("device_files", {}).get(output.get("device_file_id"))
    if file is not None:
        complete = file.get("presence_state") == 3
    else:
        file = context.state_rows.get("intermediate_files", {}).get(
            output.get("intermediate_file_id")
        )
        complete = file is not None and file.get("cleanup_state") == 4
    if not complete:
        raise EventValidationError("缺席来源观察要求先行文件完成事实")
    matches = []
    for attempt in context.state_rows.get("operation_attempts", {}).values():
        if attempt.get("result_event_id") != result_id:
            continue
        run = context.state_rows.get("operation_runs", {}).get(attempt.get("run_id"))
        if (
            run is None
            or run.get("cleanup_item_id") != item_id
            or run.get("action_id") != item.get("action_id")
            or run.get("kind") not in (4, 5)
            or attempt.get("status") == 1
        ):
            raise EventValidationError("缺席来源结果与原成员或操作不符")
        operation = "delete" if run["kind"] == 4 else "query"
        if (
            run.get("responsibility_key")
            != f"{'delete' if operation == 'delete' else 'exists'}/{item_id}"
        ):
            raise EventValidationError("缺席来源责任键不同")
        try:
            actual = read_result_document(
                attempt["status"],
                attempt["effect_state"],
                attempt["result_json"],
                attempt["error_json"],
            )
            matches.append(_presence(actual, operation, item_id))
        except (TypeError, ValueError, ConsistencyError) as error:
            raise EventValidationError("缺席来源结果不可解释") from error
    if matches != [False]:
        raise EventValidationError("缺席来源缺少唯一可靠原结果")
