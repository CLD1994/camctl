"""同机批次所需的完整、分页事实读取。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError
from camctl.outputs.batches import (BatchReadinessFact, ObtainDeviceFact, classify_batch_action,
    batch_ready_requirements, obtain_device_requirements, batch_source_selected,
    obtain_batch_member_finished, internal_read_precedes_batch)


class FileBatchReads:
    def __init__(self, connection, *, capture_read_parallel_supported=False):
        self.connection = connection
        self.capture_read_parallel_supported = capture_read_parallel_supported

    def supported(self):
        row = self.connection.execute("SELECT format_version FROM database_metadata WHERE id=1").fetchone()
        if row is None:
            raise ConsistencyError("文件批次读取缺少数据库版本")
        return row[0] >= 9

    def holder(self, device_id):
        if not self.supported():
            return None
        rows = self.connection.execute(
            "SELECT id,action_id FROM device_file_batches WHERE device_id=?"
            " AND released_event_id IS NULL ORDER BY id LIMIT 2", (device_id,),
        ).fetchall()
        if len(rows) > 1:
            raise ConsistencyError("同一相机存在多个文件批次持有者")
        return None if not rows else rows[0]

    def permits(self, action_id, device_id, *, require_owned=False):
        holder = self.holder(device_id)
        return (not require_owned) if holder is None else holder[1] == action_id

    def legacy_read_held(self, copy_id, action_id, device_id):
        """升级前已持槽的原 RUNNING 读取继续原票据，不建立整批归属。"""
        if not self.supported() or self.holder(device_id) is not None:
            return False
        return self.connection.execute(
            "SELECT 1 FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
            " JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=r.attempts_used"
            " WHERE c.id=? AND r.action_id=? AND c.slot_device_id=?"
            " AND t.status=1 AND t.result_event_id IS NULL AND t.result_json IS NULL"
            " AND NOT EXISTS(SELECT 1 FROM device_file_batches b WHERE b.action_id=r.action_id"
            " AND b.device_id=c.slot_device_id) LIMIT 1", (copy_id, action_id, device_id),
        ).fetchone() is not None

    def operation_permitted(self, action, run):
        from camctl.contracts.enums import enum_for
        kinds = enum_for("operation_runs.kind")
        if run["kind"] in (kinds.STOP, kinds.STOP_RESIDUAL, kinds.EMERGENCY_STOP):
            return True
        device_id = self.operation_device(action, run)
        if (device_id is not None and run.get("id") is not None and run.get("copy_id") is not None
                and self.legacy_read_held(run["copy_id"], action["id"], device_id)):
            return True
        if device_id is not None and run.get("availability_check_id") is not None:
            check = self.connection.execute("SELECT trigger_copy_id FROM device_availability_checks WHERE id=?",
                (run["availability_check_id"],)).fetchone()
            if check is not None and check[0] is not None and self.legacy_read_held(check[0], action["id"], device_id):
                return True
        return device_id is None or self.permits(action["id"], device_id, require_owned=action["type"] in (4, 5))

    def operation_device(self, action, run):
        row = None
        if run.get("copy_id") is not None:
            row = self.connection.execute(
                "SELECT a.device_id FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id"
                " JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (run["copy_id"],),
            ).fetchone()
        elif run.get("cleanup_item_id") is not None:
            row = self.connection.execute(
                "SELECT a.device_id FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
                " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id"
                " WHERE c.id=?", (run["cleanup_item_id"],),
            ).fetchone()
        elif run.get("obtain_item_id") is not None:
            row = self.connection.execute(
                "SELECT a.device_id FROM obtain_items i JOIN outputs o ON o.id=COALESCE(i.output_id,i.requested_output_id)"
                " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id"
                " WHERE i.id=?", (run["obtain_item_id"],),
            ).fetchone()
        elif run.get("availability_check_id") is not None:
            row = self.connection.execute("SELECT device_id FROM device_availability_checks WHERE id=?",
                (run["availability_check_id"],)).fetchone()
        elif action.get("device_id") is not None:
            row = (action["device_id"],)
        return None if row is None else row[0]

    def calls_pending(self, action_id, device_id):
        from camctl.persistence.repositories.transactions import row_facts
        action = self.action(action_id)
        with closing(self.connection.execute(
            "SELECT r.id FROM operation_runs r WHERE r.action_id=? AND r.kind IN (3,4,5,12)"
            " AND (r.retry_wait_required=1 OR (r.kind IN (4,5,12) AND r.status IN (1,2))"
            " OR EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
            " AND (t.status=1 OR t.result_event_id IS NULL))) ORDER BY r.id", (action_id,),
        )) as cursor:
            while rows := cursor.fetchmany(64):
                for (run_id,) in rows:
                    run = row_facts(self.connection, "operation_runs", run_id)
                    if run is None:
                        raise ConsistencyError("批次原设备调用缺少流程")
                    if self.operation_device(action, run) == device_id:
                        return True
        return False

    def action(self, action_id):
        from camctl.persistence.repositories.transactions import row_facts
        row = row_facts(self.connection, "actions", action_id)
        if row is None:
            raise ConsistencyError("文件批次所属动作缺失")
        return row

    def source_ready(self, action_id, device_id):
        from camctl.persistence.repositories.outputs import _processing_completed
        with closing(self.connection.execute(
            "SELECT s.id,s.status,x.status FROM action_dependencies d"
            " JOIN actions s ON s.id=d.depends_on_action_id"
            " LEFT JOIN obtain_source_selections x ON x.dependency_id=d.id"
            " WHERE d.action_id=? AND s.device_id=? ORDER BY d.id", (action_id, device_id),
        )) as cursor:
            while rows := cursor.fetchmany(64):
                for source_id, status, selection in rows:
                    if not batch_source_selected(status, selection):
                        return False
                    if not _processing_completed(self.connection, source_id):
                        return False
        return True

    def _obtain_members(self, action_id, device_id):
        # 使用产物原机身，而非当前 copy 的源；同步副本仍可能回退到相机。
        sql = (
            "SELECT i.status,i.source_dependency,c.id,c.verification_state,c.read_end_event_id,"
            "r.retry_wait_required,EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
            " AND (t.status=1 OR t.result_event_id IS NULL)),d.status"
            " FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
            " JOIN action_dependencies x ON x.id=s.dependency_id"
            " JOIN actions src ON src.id=x.depends_on_action_id"
            " LEFT JOIN outputs o ON o.id=COALESCE(i.output_id,i.requested_output_id)"
            " LEFT JOIN device_files f ON f.id=o.device_file_id"
            " LEFT JOIN actions ob ON ob.id=f.observer_action_id"
            " LEFT JOIN deliveries d ON d.id=i.delivery_id"
            " LEFT JOIN file_copies c ON c.delivery_id=d.id"
            " LEFT JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
            " WHERE x.action_id=? AND (ob.device_id=? OR (o.id IS NULL AND src.device_id=?))"
            " ORDER BY i.id"
        )
        with closing(self.connection.execute(sql, (action_id, device_id, device_id))) as cursor:
            while rows := cursor.fetchmany(64):
                yield from rows

    def obtain_finished(self, action_id, device_id):
        if not self.source_ready(action_id, device_id):
            return False
        for status, dependency, copy_id, verification, ended, retry, unsettled, delivery in self._obtain_members(action_id, device_id):
            if not obtain_batch_member_finished(status, dependency, copy_id, verification, ended,
                                                retry, unsettled, delivery):
                return False
        return True

    def cleanup_blocked(self, action_id, device_id):
        return self.connection.execute(
            "SELECT 1 FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " JOIN obtain_items i ON i.output_id=o.id AND i.source_dependency=1"
            " WHERE c.action_id=? AND ob.device_id=? LIMIT 1", (action_id, device_id),
        ).fetchone() is not None

    def obtain_device_ready(self, action_id, device_id):
        base = (
            " FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
            " JOIN action_dependencies x ON x.id=s.dependency_id"
            " JOIN outputs o ON o.id=COALESCE(i.output_id,i.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " LEFT JOIN file_copies c ON c.delivery_id=i.delivery_id"
            " WHERE x.action_id=? AND ob.device_id=?"
        )
        for fact, required in obtain_device_requirements():
            if fact is ObtainDeviceFact.LOCAL_WAIT_EXISTS:
                found = self.connection.execute(
                    "SELECT 1" + base + " AND (i.status=2 OR (i.source_dependency=1"
                    " AND c.source_intermediate_file_id IS NOT NULL AND c.verification_state NOT IN (3,5))) LIMIT 1",
                    (action_id, device_id),
                ).fetchone()
            elif fact is ObtainDeviceFact.DEVICE_CANDIDATE_EXISTS:
                found = self.connection.execute(
                    "SELECT 1" + base + " AND (i.status=1 OR (i.source_dependency=1"
                    " AND c.source_device_file_id IS NOT NULL AND c.verification_state NOT IN (3,5))) LIMIT 1",
                    (action_id, device_id),
                ).fetchone()
            else:
                raise ValueError(f"未知取回设备事实: {fact!r}")
            if (found is not None) != required:
                return False
        return True

    def cleanup_finished(self, action_id, device_id):
        return self.connection.execute(
            "SELECT 1 FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " WHERE c.action_id=? AND ob.device_id=? AND (c.status NOT IN (4,5,6) OR EXISTS("
            " SELECT 1 FROM operation_runs r WHERE r.cleanup_item_id=c.id AND r.kind IN (4,5)"
            " AND (r.status IN (1,2) OR EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
            " AND (t.status=1 OR t.result_event_id IS NULL))))) LIMIT 1", (action_id, device_id),
        ).fetchone() is None

    def cleanup_residual(self, action_id, device_id):
        from camctl.outputs.cleanup_results import cleanup_member_delete_state
        with closing(self.connection.execute(
            "SELECT c.id FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " WHERE c.action_id=? AND ob.device_id=? ORDER BY c.id", (action_id, device_id),
        )) as cursor:
            while rows := cursor.fetchmany(64):
                for (item_id,) in rows:
                    dispatched, unsettled = cleanup_member_delete_state(self.connection, item_id)
                    if dispatched or unsettled:
                        return True
        return False

    def finished(self, action_id, device_id):
        action = self.action(action_id)
        if self.calls_pending(action_id, device_id):
            return False
        if action["type"] == 4:
            return self.obtain_finished(action_id, device_id)
        if action["type"] == 5:
            return self.cleanup_finished(action_id, device_id)
        raise ConsistencyError("文件批次只能属于取回或清理")

    def ready(self, action_id, device_id, now):
        action = self.action(action_id)
        requirements = batch_ready_requirements(classify_batch_action(action, now))
        if requirements is None:
            return False
        for fact, required in requirements:
            if fact is BatchReadinessFact.SOURCE_CHECKS_PASSED:
                actual = self.source_ready(action_id, device_id)
            elif fact is BatchReadinessFact.DEVICE_OBTAIN_CHECKS_PASSED:
                actual = self.obtain_device_ready(action_id, device_id)
            elif fact is BatchReadinessFact.OBTAIN_MEMBERS_FINISHED:
                actual = self.obtain_finished(action_id, device_id)
            elif fact is BatchReadinessFact.CLEANUP_NO_PENDING_TARGETS:
                actual = self.cleanup_device_ready(action_id, device_id)
            elif fact is BatchReadinessFact.CLEANUP_DEPENDENCY_EXISTS:
                actual = self.cleanup_blocked(action_id, device_id)
            elif fact is BatchReadinessFact.CLEANUP_MEMBERS_FINISHED:
                actual = self.cleanup_finished(action_id, device_id)
            elif fact is BatchReadinessFact.CLEANUP_RESIDUAL_EXISTS:
                actual = self.cleanup_residual(action_id, device_id)
            else:
                raise ValueError(f"未知批次资格事实: {fact!r}")
            if bool(actual) != required:
                return False
        return True

    def cleanup_device_ready(self, action_id, device_id):
        return self.connection.execute(
            "SELECT 1 FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " WHERE c.action_id=? AND ob.device_id=? AND c.status=1 LIMIT 1", (action_id, device_id),
        ).fetchone() is None

    def first(self, device_id, now):
        sql = (
            "SELECT a.id,a.scheduled_at,a.type,a.plan_id,a.input_index FROM actions a"
            " WHERE a.scheduled_at<=? AND ((a.status=2 AND a.cancel_requested=0) OR"
            " (a.type=5 AND EXISTS(SELECT 1 FROM operation_runs r WHERE r.action_id=a.id"
            " AND r.cleanup_item_id IS NOT NULL AND r.kind IN (4,5) AND r.status IN (1,2)))) AND ("
            "(a.type=4 AND EXISTS(SELECT 1 FROM action_dependencies d JOIN actions s"
            " ON s.id=d.depends_on_action_id WHERE d.action_id=a.id AND s.device_id=?)) OR"
            "(a.type=5 AND EXISTS(SELECT 1 FROM cleanup_items c JOIN outputs o ON o.id=COALESCE(c.output_id,c.requested_output_id)"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions ob ON ob.id=f.observer_action_id"
            " WHERE c.action_id=a.id AND ob.device_id=?)))"
            " ORDER BY a.scheduled_at,CASE a.type WHEN 4 THEN 0 ELSE 1 END,a.plan_id,a.input_index"
        )
        with closing(self.connection.execute(sql, (now, device_id, device_id))) as cursor:
            while rows := cursor.fetchmany(64):
                for row in rows:
                    if self.ready(row[0], device_id, now):
                        return row[0]
        return None

    def busy(self, action_id, device_id, now):
        if self.connection.execute(
                "SELECT 1 FROM actions WHERE device_id=? AND type IN (1,2,3)"
                " AND status=1 AND cancel_requested=0 AND scheduled_at<=?"
                " AND scheduled_at+max_delay_ms*1000>=? LIMIT 1", (device_id, now, now),
        ).fetchone() is not None:
            return True
        from camctl.outputs.read_discovery import first_eligible_read
        internal = first_eligible_read(self.connection, device_id, now, internal_only=True)
        if internal is not None:
            other = self.connection.execute(
                "SELECT a.scheduled_at,a.plan_id,a.input_index FROM file_copies c"
                " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
                " JOIN actions a ON a.id=r.action_id WHERE c.id=?", (internal,),
            ).fetchone()
            action = self.action(action_id)
            if other is None:
                raise ConsistencyError("内部读取缺少批次排序依据")
            if internal_read_precedes_batch(other, action):
                return True
        if self.connection.execute(
                "SELECT 1 FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
                " WHERE c.slot_device_id=? AND r.action_id<>? LIMIT 1", (device_id, action_id),
        ).fetchone() is not None:
            return True
        from camctl.persistence.repositories.preparation import PreparationReads
        from camctl.scheduling.resources import current_preparation_holder, device_start_in_flight
        reads = PreparationReads(self.connection)
        if (current_preparation_holder(self.connection, device_id) is not None
                or device_start_in_flight(self.connection, device_id)
                or reads.availability_in_flight(device_id, None)):
            return True
        activity_sql = ("SELECT 1 FROM device_activities d JOIN actions a ON a.id=d.action_id"
            " WHERE a.device_id=? AND a.status=2 AND d.occupancy_state=1")
        activity_params = (device_id,)
        if self.capture_read_parallel_supported:
            from camctl.contracts.enums import enum_for
            action_types = enum_for("actions.type")
            if self.action(action_id)["type"] == action_types.OBTAIN_ACTION_OUTPUTS:
                activity_sql += " AND NOT (a.type=? AND d.started_at IS NOT NULL AND d.activity_state=?)"
                activity_params += (action_types.CAMERA_RECORD, enum_for("device_activities.activity_state").ACTIVE)
        return self.connection.execute(activity_sql + " LIMIT 1", activity_params).fetchone() is not None
