"""读取重试守卫在原写事务内核对的事实。"""

from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError


class RetryWaitReads:
    def __init__(self, connection):
        self.connection = connection

    def _require_index(self, name, expected):
        with closing(self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        )) as cursor:
            index = cursor.fetchone()
        if index is None or not isinstance(index[0], str) or " ".join(index[0].split()) != expected:
            raise ConsistencyError(f"读取等待所需索引缺失或定义不符: {name}")

    def no_unsettled_attempts(self, run_id, validated=()):
        self._require_index("operation_attempts_run_status",
                            "CREATE INDEX operation_attempts_run_status ON operation_attempts(run_id, status)")
        self._require_index("operation_attempts_without_result",
                            "CREATE INDEX operation_attempts_without_result ON operation_attempts(run_id) WHERE result_json IS NULL")
        running = int(enum_for("operation_attempts.status").RUNNING)
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND status=? LIMIT 2", (run_id, running),
        )) as cursor:
            running_rows = cursor.fetchall()
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND result_json IS NULL LIMIT 2", (run_id,),
        )) as cursor:
            missing_rows = cursor.fetchall()
        pending = {row[0] for row in (*running_rows, *missing_rows)}
        if not pending:
            return True
        if len(running_rows) == 2 or len(missing_rows) == 2:
            return False
        for accepted in validated:
            event = accepted.envelope
            for row in event.rows:
                if (row.table == "operation_attempts" and row.row_id in pending
                        and row.after.values.get("status") != running
                        and row.after.values.get("result_json") is not None
                        and row.after.values.get("result_event_id") is not None):
                    pending.remove(row.row_id)
            if not pending:
                return True
        return not pending

    def last_attempt(self, run_id, attempts_used):
        with closing(self.connection.execute(
            "SELECT id,status,result_json,result_event_id FROM operation_attempts"
            " WHERE run_id=? AND attempt_no=?", (run_id, attempts_used),
        )) as cursor:
            last = cursor.fetchone()
        with closing(self.connection.execute(
            "SELECT MAX(attempt_no) FROM operation_attempts WHERE run_id=?", (run_id,),
        )) as cursor:
            maximum = cursor.fetchone()
        if maximum is None or maximum[0] != attempts_used:
            raise ConsistencyError("读取流程的累计次数与最近尝试不一致")
        attempts = enum_for("operation_attempts.status")
        if (last is None or last[1] not in (int(attempts.FAILED), int(attempts.UNKNOWN))
                or last[2] is None or last[3] is None):
            return None
        return last if self.no_unsettled_attempts(run_id) else None

    def completed(self, run_id, attempts_used):
        from camctl.history.events import reason_for_branch
        from .operations import _RETRY_WAIT_EVENT

        last = self.last_attempt(run_id, attempts_used)
        if last is None:
            return False
        self._require_index("history_events_row", "CREATE INDEX history_events_row ON history_events(event_type, "
                            "json_extract(body_json, '$.rows[0].table'), json_extract(body_json, '$.rows[0].id'), id)")
        with closing(self.connection.execute(
            "SELECT body_json FROM history_events WHERE event_type=?"
            " AND json_extract(body_json,'$.rows[0].table')=?"
            " AND json_extract(body_json,'$.rows[0].id')=? AND id>?"
            " ORDER BY id DESC LIMIT 1", (_RETRY_WAIT_EVENT, "operation_runs", run_id, last[3]),
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            return False
        body = parse_exact_json(row[0])
        rows = body.get("rows")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ConsistencyError("重试等待事件的行事实无效")
        change = rows[0]
        reason = body.get("reason")
        if reason != reason_for_branch("RETRY_WAIT_CHANGED", "COMPLETE"):
            if reason not in (reason_for_branch("RETRY_WAIT_CHANGED", "REQUIRE"),
                              reason_for_branch("RETRY_WAIT_CHANGED", "ABANDON")):
                raise ConsistencyError("重试等待事件的分支无效")
            return False
        if (body.get("evidence") != {"attempt_id": last[0]}
                or change.get("table") != "operation_runs" or change.get("id") != run_id
                or change.get("before") != {"exists": True, "values": {"retry_wait_required": 1}}
                or change.get("after") != {"exists": True, "values": {"retry_wait_required": 0}}):
            raise ConsistencyError("等待完成事件与原尝试不符")
        return True

    def read_eligible(self, run_id, occurred_at):
        from camctl.outputs.read_discovery import read_copy_dispatch_eligible
        from camctl.persistence.repositories.outputs import _read_is_due, _read_owner_eligible

        with closing(self.connection.execute(
            "SELECT a.status,a.cancel_requested,a.scheduled_at,c.id,c.slot_device_id,"
            " d.status,i.source_dependency,df.completion_state,ob.device_id,ob.driver_id,"
            " origin.device_id,origin.driver_id FROM operation_runs r"
            " JOIN actions a ON a.id=r.action_id JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id AND d.id=r.delivery_id AND d.action_id=a.id"
            " JOIN obtain_items i ON i.delivery_id=d.id"
            " JOIN device_files df ON df.id=c.source_device_file_id"
            " JOIN actions ob ON ob.id=df.observer_action_id"
            " JOIN actions origin ON origin.id=df.source_action_id"
            " WHERE r.id=? AND r.kind=?", (run_id, int(enum_for("operation_runs.kind").READ_FILE)),
        )) as cursor:
            rows = cursor.fetchmany(2)
        if len(rows) != 1:
            raise ConsistencyError("读取重试缺少所属动作、拷贝或交付")
        (status, canceled, scheduled, copy_id, slot, delivery_status, source_dependency,
         completion, device, driver, source_device, source_driver) = rows[0]
        if (not isinstance(device, str) or not device or not isinstance(driver, str) or not driver
                or (device, driver) != (source_device, source_driver) or (slot is not None and slot != device)):
            raise ConsistencyError("读取重试的原来源绑定或机会不一致")
        action = {"status": status, "cancel_requested": canceled, "scheduled_at": scheduled}
        delivery = enum_for("deliveries.status")
        try:
            if type(delivery_status) is not int:
                raise ValueError
            delivery(delivery_status)
        except ValueError as error:
            raise ConsistencyError("读取交付状态无法解释") from error
        if type(source_dependency) is not int or source_dependency not in (0, 1):
            raise ConsistencyError("读取交付状态或来源保护无法解释")
        return (_read_owner_eligible(action) and _read_is_due(action, occurred_at)
                and slot is not None and completion == int(enum_for("device_files.completion_state").COMPLETE)
                and delivery_status in (int(delivery.PENDING), int(delivery.PREPARING))
                and source_dependency == 1 and read_copy_dispatch_eligible(self.connection, copy_id))
