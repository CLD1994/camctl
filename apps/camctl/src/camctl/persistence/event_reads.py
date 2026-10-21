"""原写事务的已验证事件覆盖，以及正式发布身份的单事件查证。"""

from contextlib import closing
from dataclasses import dataclass, replace
from types import MappingProxyType

from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.reads import PlanReadScope, ProductReadScope, CaptureResultReadScope, SegmentReadScope, SourceResolutionReadScope, AdmissionSourceReadScope


@dataclass(frozen=True)
class _ValidatedSegmentFinish:
    scope: object
    transaction: object
    first_event_id: int
    header: str
    qualification: object


class SqlEventReads:
    """供产物、计划及拍摄结果守卫独立复核的有限只读端口。

    查询字段由内部白名单限定。每批 SQL 游标在交付前关闭；覆盖表
    只含本事务已经验证的变化，读错误与失效事务直接传播。
    正式marker另按原提案的一条事件证明固定身份，不提供未来资格。
    """

    _RANGES = {
        "plans": frozenset(("id",)),
        "actions": frozenset(("id", "status", "plan_id")),
        "action_dependencies": frozenset(("id", "action_id")),
        "obtain_source_selections": frozenset(("id", "dependency_id")),
        "obtain_items": frozenset(("id", "selection_id", "output_id", "requested_output_id")),
        "cleanup_items": frozenset(("id", "action_id", "output_id")),
        "recording_processing": frozenset(("id", "action_id")),
        "outputs": frozenset(("id",)),
        "device_files": frozenset(("id",)),
        "intermediate_files": frozenset(("id",)),
        "output_origins": frozenset(("id", "output_id", "original_output_id")),
    }
    SOURCE_COLUMNS = ("id", "type", "status", "plan_id", "name", "group_name")
    ACTION_COLUMNS = ("id", "type", "status", "scheduled_at", "cancel_requested", "plan_id", "input_index")

    def __init__(self, connection, scope, validated=(), planned_events=(), *, proposal_event=None, segment_ref=None, segment_guard_state=None):
        if not isinstance(scope, (ProductReadScope, PlanReadScope, CaptureResultReadScope, SegmentReadScope, SourceResolutionReadScope, AdmissionSourceReadScope)):
            raise ConsistencyError("事实读取必须使用原事务声明的范围")
        self.connection, self.scope = connection, scope
        self._planned_events = planned_events
        self._validated = validated
        self.proposal_event = proposal_event
        self.segment_ref = segment_ref
        self._segment_guard_state = segment_guard_state
        self._segment_guard_transaction = None
        self._result_file_ids = frozenset()
        self._overlay = {}
        for accepted in validated:
            event = accepted.envelope
            for row in event.rows:
                key = row.table, row.row_id
                if not row.after.exists:
                    self._overlay[key] = None
                else:
                    prior = self._overlay.get(key, {})
                    self._overlay[key] = {**(prior or {}), **row.after.values}

    def segment_activation_event(self, event_id, transaction):
        """只定位原提案中的一个发布事件，不向业务提供未来状态资格。"""
        return self.transaction_event(event_id,transaction)

    def transaction_event(self, event_id, transaction):
        """查证原完整提案中的单条实际事件；不提供未来业务状态。"""
        ObjectId(event_id)
        if (not self.connection.in_transaction
                or not isinstance(self.scope,(SegmentReadScope,PlanReadScope))
                or not transaction.first_event_id<=event_id<=transaction.last_event_id):
            raise ConsistencyError('正式marker不属于原完整完成事务')
        offset=event_id-transaction.first_event_id
        if self.proposal_event is not None:
            found = self.proposal_event(event_id)
            if found.event_id != event_id or found.transaction_id != transaction.txn_id:
                raise ConsistencyError('正式marker的实际事件位置或事务不同')
            return found
        if offset>=len(self._planned_events):
            raise ConsistencyError('正式marker没有原事务实际ACTIVATE事件')
        found=self._planned_events[offset]
        if found.event_id!=event_id or found.transaction_id!=transaction.txn_id:
            raise ConsistencyError('正式marker的实际事件位置或事务不同')
        return found

    def photo_entry_apply_event(self, fixed_event, source_action_id, item_id, transaction):
        """只查原实际提案的同组源应用身份，不提供未来业务资格。"""
        ObjectId(source_action_id)
        ObjectId(item_id)
        count = transaction.last_event_id - transaction.first_event_id + 1
        offset = fixed_event.event_id - transaction.first_event_id
        if (not self.connection.in_transaction or not isinstance(self.scope, PlanReadScope)
                or len(self._planned_events) != count or not 0 <= offset < count
                or self._planned_events[offset] is not fixed_event
                or fixed_event.transaction_id != transaction.txn_id):
            raise ConsistencyError('入口取消固定不属于原完整实际事务')
        found = None
        for index, event in enumerate(self._planned_events):
            if (event.event_id != transaction.first_event_id + index
                    or event.transaction_id != transaction.txn_id):
                raise ConsistencyError('入口取消实际提案的事件位置或事务不符')
            if event.event_id <= fixed_event.event_id or (event.event_type, event.reason) != (25, 1):
                continue
            members = [row for row in event.rows if row.table == 'cancel_items' and row.row_id == item_id]
            request = event.evidence.get('cancel_request', {})
            if not members and request.get('item_id') != item_id:
                continue
            sources = [row for row in event.rows if row.table == 'actions']
            if (found is not None or len(members) != 1 or request.get('item_id') != item_id
                    or len(sources) != 1 or sources[0].row_id != source_action_id):
                raise ConsistencyError('入口取消的实际源或成员应用不唯一或不匹配')
            found = event
        if found is None:
            raise ConsistencyError('原入口取消事务缺少对应源成员应用')
        return found

    def verify_segment_event(self, event, context):
        from camctl.persistence.repositories.segments import verify_segment_event
        from camctl.capture.segment_models import SegmentSetRef
        from camctl.persistence.transaction import row_facts
        if not self.connection.in_transaction:
            raise ConsistencyError('分段守卫只能复核原写事务集合')
        if isinstance(self.scope, SegmentReadScope):
            ref = self.scope.ref
        elif isinstance(self.scope, PlanReadScope):
            ref = SegmentSetRef(**event.evidence['segment_request']['ref'])
            action = row_facts(self.connection,'actions',ref.action_id)
            if action is None or action['plan_id'] != self.scope.plan_id:
                raise ConsistencyError('共同发布集合不属于原计划')
        else:
            raise ConsistencyError('分段守卫缺少原集合或原计划范围')
        verify_segment_event(self.connection, ref, event, context,
            prior_events=self._validated)

    def verify_segment_finish_event(self, event, context, action_id):
        from camctl.persistence.repositories.segment_transactions import verify_finish_request_event
        if not self.connection.in_transaction:
            raise ConsistencyError('分段终态守卫只能读取原写事务')
        self.required('actions',action_id)
        verify_finish_request_event(self.connection,event,context,action_id)

    def bind_segment_finish_state(self,state,transaction):
        """由唯一事务内核在每条上下文上绑定原组进度，不接受上下文另定资格。"""
        if self._segment_guard_transaction not in (None,transaction):
            raise ConsistencyError('事件读取端口已经绑定另一完整事务')
        if self._segment_guard_state is not state and self._segment_guard_state:
            raise ConsistencyError('事件上下文携带另一组分段完成进度')
        self._segment_guard_state=state
        self._segment_guard_transaction=transaction

    def prepare_segment_finish(self,event,transaction,header,qualification):
        if self._segment_guard_state is None or self._segment_guard_transaction!=transaction:
            raise ConsistencyError('分段F没有唯一事务内核绑定的原组进度')
        from camctl.persistence.transaction import encode_json_value
        self._segment_guard_state.pop('accepted',None)
        self._segment_guard_state['pending']=(self.scope,transaction,event,
            encode_json_value(header),MappingProxyType(dict(qualification)))

    def seal_segment_finish(self,validated):
        """全部具名守卫通过后，逐条封存实际事件的完整规范身份。"""
        if self._segment_guard_state is None:return
        state=self._segment_guard_state
        pending=state.pop('pending',None)
        event=validated.envelope
        if pending is not None:
            scope,transaction,first,header,qualification=pending
            if scope!=self.scope or event!=first or event.event_id!=transaction.first_event_id:
                raise ConsistencyError('原F资格没有当前scope实际通过的首事件')
            from camctl.persistence.repositories.segment_transactions import _canonical_events,_header_request
            from camctl.contracts.json_values import parse_exact_json
            raw=parse_exact_json(header)
            state['accepted']=_ValidatedSegmentFinish(scope,transaction,event.event_id,header,qualification)
            state['progress']=iter(_canonical_events(_header_request(raw),transaction,raw,self,qualification))
        proof=state.get('accepted')
        if not isinstance(proof,_ValidatedSegmentFinish):return
        if proof.scope!=self.scope:raise ConsistencyError('实际事件改变原完成读范围')
        progress=state.get('progress')
        expected=None if progress is None else next(progress,None)
        if expected is None or replace(event,change_seq=None)!=expected:
            raise ConsistencyError('实际完成事件与原冻结申请的完整规范事件不同')
        if event.event_id==proof.transaction.last_event_id:
            marker=self.transaction_event(event.event_id,proof.transaction)
            if replace(marker,change_seq=None)!=expected:
                raise ConsistencyError('实际L与原唯一ACTIVATE提案的完整事件不同')
            if next(progress,None) is not None:raise ConsistencyError('实际L之前规范阶段尚未耗尽')
            state.pop('progress',None)

    def segment_created_output(self,transaction,action_id,file_id,kind):
        """有限入口借用实际已验证前序；有界入口只读已写入原连接。"""
        if self.proposal_event is not None:
            from camctl.persistence.repositories.segment_transactions import _created_output
            return _created_output(self.connection,transaction,action_id,file_id,kind)
        from camctl.history.events import load_event_registry
        output_type=load_event_registry()['events']['OUTPUT_REGISTERED']['id']
        found=None
        for accepted in self._validated:
            event=accepted.envelope
            if (event.transaction_id!=transaction.txn_id or event.event_type!=output_type
                    or not transaction.first_event_id<event.event_id<transaction.last_event_id):continue
            for row in event.rows:
                if row.table=='outputs' and not row.before.exists:
                    values=row.after.values
                    if (values.get('source_action_id'),values.get('device_file_id'),values.get('kind'))==(action_id,file_id,kind):
                        if found is not None:raise ConsistencyError('原已验证前序为同一候选重复登记产物')
                        found=row.row_id
        if found is None:raise ConsistencyError('规范关联没有实际已验证的原片前序')
        return found

    def validated_segment_finish(self,transaction,header):
        from camctl.persistence.transaction import encode_json_value
        proof=None if self._segment_guard_state is None else self._segment_guard_state.get('accepted')
        if (not isinstance(proof,_ValidatedSegmentFinish) or proof.scope!=self.scope or proof.transaction!=transaction
                or proof.first_event_id!=transaction.first_event_id or proof.header!=encode_json_value(header)):
            raise ConsistencyError('最终守卫没有同scope/完整组/实际原头的已验证F资格')
        return proof.qualification

    def admitted_source_owner(self, owner_id, transaction):
        """原 F 前不存在，且已由内核实际接受的 ACTION_ADMITTED 创建。"""
        from camctl.persistence.transaction import row_facts
        from camctl.history.events import load_event_registry
        from camctl.history.decoding import decode_event_row
        if (type(self.scope) is not AdmissionSourceReadScope or not self.connection.in_transaction
                or getattr(self, '_kernel_transaction', None) != transaction):
            raise ConsistencyError('受理来源成员缺少原事务可靠前序端口')
        from camctl.persistence.repositories.admission_sources import verify
        verify(self, transaction)
        accepted = getattr(self, '_kernel_accepted', None)
        current = row_facts(self.connection, 'actions', owner_id)
        admitted_type = load_event_registry()['events']['ACTION_ADMITTED']['id']
        if accepted is None:
            if current is None or not transaction.first_event_id <= current['created_event_id'] < self._kernel_event_id:
                raise ConsistencyError('受理来源 owner 在原 F 前已存在或没有实际创建前序')
            raw = self.connection.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?', (current['created_event_id'],)).fetchone()
            candidates = () if raw is None else (decode_event_row(raw),)
        else:
            if current is not None:
                raise ConsistencyError('既有 owner 不得借受理成员分支追加来源')
            candidates = (entry.envelope for entry in accepted)
        found = None
        for event in candidates:
            if event.event_type != admitted_type or event.transaction_id != transaction.txn_id:
                continue
            for row in event.rows:
                if row.table == 'actions' and row.row_id == owner_id and not row.before.exists and row.after.exists:
                    if found is not None:
                        raise ConsistencyError('受理来源 owner 的实际创建前序不唯一')
                    found = dict(row.after.values, id=owner_id)
        if (found is None or found['plan_id'] != self.scope.plan_id
                or found['source_resolution_state'] != 2 or found['resolved_source_plan_id'] != self.scope.plan_id):
            raise ConsistencyError('受理成员不属于原新建 FIXED owner 的真实计划')
        return found

    def admitted_source_members(self, owner, transaction):
        """只从实际受理前序按原 source 逐项解释预期成员。"""
        from camctl.outputs.sources import read_source_spec, _CAPTURE_TYPES
        from camctl.persistence.transaction import encode_json_value, row_facts
        spec = read_source_spec(encode_json_value(owner['input_fields_json']))
        if spec.action_instance_id is not None or spec.plan_instance_id is not None:
            raise ConsistencyError('实例来源不属于受理时的本计划成员分区')
        accepted = self._kernel_accepted
        if accepted is None:
            def candidates():
                after = 0
                while True:
                    with closing(self.connection.execute('SELECT id FROM actions WHERE plan_id=? AND created_event_id>=? AND created_event_id<? AND id>? ORDER BY id LIMIT ?',
                            (self.scope.plan_id,transaction.first_event_id,self._kernel_event_id,after,self.scope.batch_size))) as cursor:
                        rows = tuple(cursor.fetchmany(self.scope.batch_size))
                    if not rows:
                        return
                    for identity, in rows:
                        yield identity,row_facts(self.connection,'actions',identity)
                    after = rows[-1][0]
        else:
            def candidates():
                for entry in accepted:
                    for row in entry.envelope.rows:
                        if row.table == 'actions' and not row.before.exists and row.after.exists:
                            yield row.row_id,row.after.values
        for identity, source in candidates():
            if source.get('plan_id') != self.scope.plan_id or source.get('type') not in _CAPTURE_TYPES:
                continue
            if spec.action_name is not None and source.get('name') != spec.action_name:
                continue
            if spec.group is not None and source.get('group_name') != spec.group:
                continue
            yield identity

    def _check(self, table, column="id", identity=None):
        if not self.connection.in_transaction:
            raise ConsistencyError("当前事件事实端口必须在原写事务内使用")
        if isinstance(self.scope, SegmentReadScope):
            raise ConsistencyError('分段端口只提供原集合专属复核')
        if table not in self._RANGES or column not in self._RANGES[table]:
            raise ConsistencyError("当前事件事实查询超出内部允许范围")
        if identity is not None:
            ObjectId(identity)
        if isinstance(self.scope, SourceResolutionReadScope):
            from camctl.persistence.repositories.source_transactions import source_read_role
            source_read_role(self, table, column, identity)
        if isinstance(self.scope, CaptureResultReadScope) and (
                table != "device_files" or column != "id" or identity not in self._result_file_ids):
            raise ConsistencyError("结果文件事实只能读取本批原页的成员")
        if isinstance(self.scope, PlanReadScope) and not isinstance(self.scope, SourceResolutionReadScope) and (
                table != "actions" or column not in ("id", "plan_id")
                or (column == "plan_id" and identity != self.scope.plan_id)):
            raise ConsistencyError("计划聚合读取超出所属计划")

    def optional(self, table, identity, columns=None):
        self._check(table, identity=identity)
        from camctl.persistence.transaction import row_facts

        if columns is None:
            facts = row_facts(self.connection, table, identity)
        else:
            if table != "actions" or tuple(columns) not in (self.SOURCE_COLUMNS, self.ACTION_COLUMNS):
                raise ConsistencyError("事实列投影超出允许范围")
            with closing(self.connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE id=?", (identity,),
            )) as cursor:
                row = cursor.fetchone()
            facts = dict(zip(columns, row)) if row is not None else None
        if (table, identity) in self._overlay:
            changes = self._overlay[table, identity]
            facts = None if changes is None else {**(facts or {"id": identity}), **changes}
        if isinstance(self.scope, PlanReadScope) and not isinstance(self.scope, SourceResolutionReadScope) and facts is not None and facts.get("plan_id") != self.scope.plan_id:
            raise ConsistencyError("计划聚合记录不属于当前计划")
        return facts

    def required(self, table, identity):
        facts = self.optional(table, identity)
        if facts is None:
            raise ConsistencyError(f"当前事件关联记录缺失: {table}#{identity}")
        return facts

    def result_pages(self, ticket, cursor, batch):
        """只读取原单次扫描的已保存页及本批文件，不扩大历史上界。"""
        from camctl.persistence.repositories.result_pages import read_pages
        from camctl.persistence.transaction import row_facts

        scope = self.scope
        if (not isinstance(scope, CaptureResultReadScope) or not self.connection.in_transaction
                or type(batch) is not int or not 1 <= batch <= scope.batch_size
                or ticket.operation != "result" or ticket.run_id != scope.run_id
                or ticket.attempt_id != scope.attempt_no or ticket.target_id != str(scope.activity_id)
                or ticket.responsibility_key != f"results/{scope.activity_id}"):
            raise ConsistencyError("结果页读取不属于原有限活动、尝试或批次")
        run = row_facts(self.connection, "operation_runs", scope.run_id)
        if run is None or run["action_id"] != scope.action_id or run["activity_id"] != scope.activity_id:
            raise ConsistencyError("结果读取范围改变原活动归属")
        result = read_pages(ticket, cursor, batch, self)
        if any(page.ref.event_id > scope.last_page_event_id for page in result.items):
            raise ConsistencyError("结果页读取超过原可靠末页")
        self._result_file_ids = frozenset(file_id for page in result.items for _, file_id in page.file_ids)
        return result

    def iter_members(self, table, column, identity, *, columns=None, limit=None):
        self._check(table, column, identity)
        size = self.scope.batch_size if limit is None else limit
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= self.scope.batch_size:
            raise ConsistencyError("事实读取限额超出可信范围")
        changed = tuple(key[1] for key in self._overlay if key[0] == table)
        plan_owners = {}
        if isinstance(self.scope, PlanReadScope):
            for key in changed:
                changes = self._overlay[table, key]
                if changes is None:
                    plan_owners[key] = None
                    continue
                if "plan_id" in changes:
                    owner = changes["plan_id"]
                else:
                    # 稀疏前序变化不带归属；只查原行，不读未来提案。
                    with closing(self.connection.execute(
                            "SELECT plan_id FROM actions WHERE id=?", (key,))) as cursor:
                        original = cursor.fetchone()
                    if original is None:
                        raise ConsistencyError("计划覆盖行缺少可靠原归属")
                    owner = original[0]
                try:
                    ObjectId(owner)
                except (TypeError, ValueError) as error:
                    raise ConsistencyError("计划覆盖行的归属无效") from error
                plan_owners[key] = owner
            changed = tuple(key for key in changed if plan_owners[key] == self.scope.plan_id)
        after = 0
        while True:
            # 前序事件可能使成员进入或离开范围；它们必须参与稳定 ID 扫描。
            extra = " OR id IN (" + ",".join("?" for _ in changed) + ")" if changed else ""
            with closing(self.connection.execute(
                f"SELECT id FROM {table} WHERE ({column}=?{extra}) AND id>? ORDER BY id LIMIT ?",
                (identity, *changed, after, size),
            )) as cursor:
                candidates = tuple(cursor.fetchmany(size))
            # 未写入 SQL 的新行独立合并；数量仅由本事务实际触及行数限制。
            added = [key for key in changed if key > after and
                     not any(key == row[0] for row in candidates)]
            upper = candidates[-1][0] if len(candidates) == size else None
            ids = sorted({row[0] for row in candidates} | {
                key for key in added if upper is None or key <= upper})
            if not ids:
                return
            batch = []
            if isinstance(self.scope, SourceResolutionReadScope):
                from camctl.persistence.repositories.source_transactions import bind_aggregate_page
                previous_aggregate = self._source_aggregate_page
                bind_aggregate_page(self, ids[:size])
            for key in ids[:size]:
                if key in plan_owners and plan_owners[key] != self.scope.plan_id:
                    continue
                facts = self.optional(table, key, columns)
                if facts is not None and facts.get(column, key if column == "id" else None) == identity:
                    batch.append((key, facts))
            after = ids[min(size, len(ids)) - 1]
            try:
                yield tuple(batch)
            finally:
                if isinstance(self.scope, SourceResolutionReadScope):
                    self._source_aggregate_page = previous_aggregate
            if len(candidates) < size and len(ids) <= size:
                return

    def actions(self):
        for status in (1, 2):
            for batch in self.iter_members("actions", "status", status, columns=self.ACTION_COLUMNS):
                yield from batch

    def recording_input_issues(self, action_id: int) -> tuple:
        """在原计划范围内独立核对唯一录像处理与原保存的 OSV 输入事实。"""
        from camctl.capture.media_facts import saved_osv_input_issues
        from camctl.contracts.json_values import parse_exact_json
        if not isinstance(self.scope, PlanReadScope):
            raise ConsistencyError("录像输入复核要求原计划读取范围")
        action = self.required("actions", action_id)
        if action["plan_id"] != self.scope.plan_id:
            raise ConsistencyError("录像输入不属于原计划")
        with closing(self.connection.execute(
            "SELECT id,check_basis_json FROM recording_processing WHERE action_id=? LIMIT 2",
            (action_id,))) as cursor:
            records = cursor.fetchmany(2)
        if len(records) > 1:
            raise ConsistencyError("录像动作关联多个媒体处理")
        if not records:
            return ()
        basis = parse_exact_json(records[0][1]) if records[0][1] else {}
        target = basis.get("target_duration_ms")
        if type(target) is not int or target <= 0:
            return ()
        return saved_osv_input_issues(self.connection, records[0][0], target)

    def repair_media_proof(self, action_id: int, processing_id: int, output_file_id: int,
            *, output_probe=None, saved: bool = False) -> None:
        """原计划范围内独立核对原处理和本次修复输出的媒体资格。"""
        from camctl.capture.media_facts import require_repair_media_proof
        if not isinstance(self.scope, PlanReadScope):
            raise ConsistencyError("修复媒体复核要求原计划读取范围")
        action = self.required("actions", action_id)
        if action["plan_id"] != self.scope.plan_id:
            raise ConsistencyError("修复媒体不属于原计划")
        require_repair_media_proof(self.connection, action_id, processing_id, output_file_id,
            output_probe=output_probe, saved=saved)
