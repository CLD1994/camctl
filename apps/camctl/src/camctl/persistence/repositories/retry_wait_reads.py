"""读取重试守卫在原写事务内核对的事实。"""

from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError


def read_reservation_blocks_control(connection, device_id, *, exclude_attempt_id=None, allow_unstarted=False):
    """读取顺序保留仅在原失败调用可靠收场后允许控制工作通过。"""
    with closing(connection.execute(
        "SELECT c.id,r.id,r.status,r.attempts_used,r.retry_wait_required FROM file_copies c"
        " LEFT JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " WHERE c.slot_device_id=? LIMIT 2",
        (int(enum_for("operation_runs.kind").READ_FILE), device_id),
    )) as cursor:
        rows = cursor.fetchmany(2)
    if not rows:
        return False
    if len(rows) != 1 or rows[0][1] is None:
        raise ConsistencyError("读取机会缺少唯一的原流程")
    copy_id, run_id, status, used, waiting = rows[0]
    runs = enum_for("operation_runs.status")
    if status not in (int(runs.PENDING), int(runs.ACTIVE)) or waiting not in (0, 1):
        return True
    if exclude_attempt_id is not None:
        if waiting != 0:
            raise ConsistencyError("未派发读取不能同时保留重试等待")
        with closing(connection.execute(
            "SELECT t.id,t.attempt_no,t.status,t.result_event_id,t.result_json,t.copy_round,c.round"
            " FROM operation_attempts t JOIN file_copies c ON c.id=?"
            " WHERE t.id=? AND t.run_id=?", (copy_id, exclude_attempt_id, run_id),
        )) as cursor:
            attempt = cursor.fetchone()
        if (attempt is None or attempt[1] != used
                or attempt[2] != int(enum_for("operation_attempts.status").RUNNING)
                or attempt[3] is not None or attempt[4] is not None or attempt[5] != attempt[6]):
            raise ConsistencyError("未派发读取排除与原票据或拷贝轮次不符")
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts WHERE run_id=? AND id<>?"
            " AND (status=? OR result_event_id IS NULL OR result_json IS NULL) LIMIT 1",
            (run_id, exclude_attempt_id, int(enum_for("operation_attempts.status").RUNNING)),
        )) as cursor:
            return cursor.fetchone() is not None
    if not used:
        if not allow_unstarted:
            return True
        if waiting != 0 or not RetryWaitReads(connection).no_attempts(run_id):
            raise ConsistencyError("零次读取责任与已保存尝试或等待矛盾")
        return False
    reads = RetryWaitReads(connection)
    if reads.last_attempt(run_id, used) is None:
        return True
    return waiting != 1 and not reads.completed(run_id, used)


def read_attempt_budget_available(connection, copy_id, max_attempts):
    """按当前采用的上限核对原流程的剩余读取次数。"""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("读取上限必须为正整数")
    with closing(connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE copy_id=? AND kind=? LIMIT 2",
        (copy_id, int(enum_for("operation_runs.kind").READ_FILE)),
    )) as cursor:
        rows = cursor.fetchmany(2)
    if len(rows) != 1:
        raise ConsistencyError("读取拷贝缺少唯一的原流程")
    run_id, status, used, waiting = rows[0]
    runs = enum_for("operation_runs.status")
    return (run_id, status, used, waiting) if (
        status in (int(runs.PENDING), int(runs.ACTIVE)) and used < max_attempts) else None


def held_read_ready_for_availability(connection, copy_id, device_id, occurred_at, max_attempts):
    """持槽读取尚未尝试，或原调用及间隔已结束时可检查设备。"""
    from camctl.outputs.read_discovery import device_holder

    if device_holder(connection, device_id) != copy_id:
        return False
    run = read_attempt_budget_available(connection, copy_id, max_attempts)
    if run is None:
        return False
    run_id, _, used, waiting = run
    if waiting != 0:
        return False
    reads = RetryWaitReads(connection)
    if not used:
        if not reads.no_attempts(run_id):
            raise ConsistencyError("零次读取责任存在原尝试")
        return reads.read_eligible(run_id, occurred_at)
    return reads.completed(run_id, used) and reads.read_eligible(run_id, occurred_at)


class RetryWaitReads:
    def __init__(self, connection):
        self.connection = connection

    def _require_index(self, name):
        from camctl.persistence.retry_wait_structure import verify_retry_wait_index
        try:
            verify_retry_wait_index(self.connection, name)
        except ValueError as error:
            raise ConsistencyError(str(error)) from error

    def no_attempts(self, run_id):
        with closing(self.connection.execute(
            "SELECT 1 FROM operation_attempts WHERE run_id=? LIMIT 1", (run_id,),
        )) as cursor:
            return cursor.fetchone() is None

    def no_unsettled_attempts(self, run_id, validated=()):
        self._require_index("operation_attempts_run_status")
        self._require_index("operation_attempts_without_result")
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

    def recovered_unknown(self, run_id, attempts_used):
        """只接受已可靠保存的原本地收场后结果缺失事实。"""
        last = self.last_attempt(run_id, attempts_used)
        if last is None or last[1] != int(enum_for("operation_attempts.status").UNKNOWN):
            return None
        from camctl.persistence.transaction import row_facts
        from camctl.operations.result_format import read_result_document
        from camctl.operations.models import AttemptStatus, EffectState, SettlementBasis
        attempt = row_facts(self.connection, "operation_attempts", last[0])
        if attempt is None:
            raise ConsistencyError("未知读取的原尝试不存在")
        try:
            outcome = read_result_document(attempt["status"], attempt["effect_state"],
                                           attempt["result_json"], attempt["error_json"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("未知读取的原结果不可解释") from error
        evidence = outcome.settlement.evidence
        if (outcome.status is not AttemptStatus.UNKNOWN or outcome.effect is not EffectState.UNKNOWN
                or outcome.error.code != "result_not_saved" or outcome.error.stage != "recovery"
                or dict(outcome.error.details) or outcome.settlement.basis is not SettlementBasis.ASSUMED
                or evidence.type not in ("local_read_recovery", "adb_foreground_recovery")
                or evidence.version != 1 or dict(evidence.data) or outcome.observations
                or outcome.call_info is not None or outcome.diagnostics is not None):
            return None
        return last

    def unknown_exhaustion_eligible(self, run_id, occurred_at):
        """按原副本归属核对无需新读取机会的未知结果耗尽。"""
        from .operations import _load_row
        from .outputs import _internal_input_requirement, _read_is_due, _read_owner_eligible
        from camctl.outputs.read_discovery import read_copy_dispatch_eligible

        run = _load_row(self.connection, "operation_runs", run_id)
        copy = None if run is None else _load_row(self.connection, "file_copies", run["copy_id"])
        if run is None or copy is None:
            raise ConsistencyError("读取耗尽缺少原流程或副本")
        parents = [("deliveries", copy.get("delivery_id")),
                   ("recording_processing", copy.get("processing_id")),
                   ("segment_inspections", copy.get("inspection_id"))]
        parents = [(table, identity) for table, identity in parents if identity is not None]
        if len(parents) != 1:
            raise ConsistencyError("读取耗尽缺少唯一所属责任")
        table, identity = parents[0]
        parent = _load_row(self.connection, table, identity)
        if parent is None or parent.get("action_id") != run.get("action_id"):
            raise ConsistencyError("读取耗尽的所属责任不一致")
        action = _load_row(self.connection, "actions", run["action_id"])
        if not _read_owner_eligible(action) or not _read_is_due(action, occurred_at):
            return False
        if not read_copy_dispatch_eligible(self.connection, copy["id"]):
            return False
        if table == "deliveries":
            return self.read_eligible(run_id, occurred_at, allow_unheld=True)
        if table == "recording_processing" and _internal_input_requirement(parent) is not None:
            return False
        if table == "segment_inspections" and parent.get("source_device_file_id") != copy.get("source_device_file_id"):
            raise ConsistencyError("读取耗尽的分段检查来源不一致")
        source_id = copy.get("source_device_file_id")
        source = None if source_id is None else _load_row(self.connection, "device_files", source_id)
        if source is None:
            raise ConsistencyError("内部读取耗尽缺少原设备来源")
        observer = _load_row(self.connection, "actions", source["observer_action_id"])
        origin = _load_row(self.connection, "actions", source["source_action_id"])
        if observer is None or origin is None:
            raise ConsistencyError("内部读取耗尽缺少原来源动作")
        binding = (observer.get("device_id"), observer.get("driver_id"))
        if (not all(isinstance(value, str) and value for value in binding)
                or binding != (origin.get("device_id"), origin.get("driver_id"))
                or (copy.get("slot_device_id") is not None and copy["slot_device_id"] != binding[0])):
            raise ConsistencyError("内部读取耗尽的原来源绑定不一致")
        return source.get("completion_state") == int(enum_for("device_files.completion_state").COMPLETE)

    def completed(self, run_id, attempts_used):
        from camctl.history.events import reason_for_branch
        from .operations import _RETRY_WAIT_EVENT

        last = self.last_attempt(run_id, attempts_used)
        if last is None:
            return False
        self._require_index("history_events_row")
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

    def source_fallback(self, run_id):
        """只承认已保存的原本地失败及同一拷贝换源事实。"""
        from .operations import _load_row
        from .outputs import _has_source_fallback_retry_proof

        run = _load_row(self.connection, "operation_runs", run_id)
        if run is None or run.get("copy_id") is None:
            return False
        copy = _load_row(self.connection, "file_copies", run["copy_id"])
        last = self.last_attempt(run_id, run["attempts_used"])
        if (copy is None or copy.get("slot_device_id") is not None
                or copy.get("source_device_file_id") is None
                or copy.get("source_intermediate_file_id") is not None or last is None
                or last[1] != int(enum_for("operation_attempts.status").FAILED)):
            return False
        attempt = _load_row(self.connection, "operation_attempts", last[0])
        if attempt is None or attempt.get("intent_event_id") is None:
            return False
        error = attempt.get("error_json")
        if not isinstance(error, dict) or error.get("code") != "local_read_failed" or error.get("stage") != "source_read":
            return False
        details = error.get("details")
        return (isinstance(details, dict)
                and (details.get("source_failed") is True
                     or details.get("photo_cache_checksum_mismatch") is True)
                and _has_source_fallback_retry_proof(self.connection, copy, attempt))

    def read_eligible(self, run_id, occurred_at, *, allow_unheld=False):
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
                and (slot is not None or allow_unheld or self.source_fallback(run_id))
                and completion == int(enum_for("device_files.completion_state").COMPLETE)
                and delivery_status in (int(delivery.PENDING), int(delivery.PREPARING))
                and source_dependency == 1 and read_copy_dispatch_eligible(self.connection, copy_id))
