"""来源固定和取回开始：原输入、有限成员页与同组规范事件。

生成方、内核封口和原键核实共用这一业务协议。资格只从原连接的
F 前事实产生；申请头、上下文字典和未来事件均不能提供资格。
"""
from contextlib import closing
from dataclasses import dataclass, replace
from itertools import zip_longest

from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId, UtcMicros
from camctl.history.events import load_event_registry
from camctl.history.reads import SourceResolutionReadScope, ObtainInitializationReadScope, ReadCoverage
from camctl.history.validators import EventContext
from camctl.outputs.sources import (
    ResolveFailure, ResolutionState, SourceResolution, read_source_spec,
    _CAPTURE_TYPES, _spec_details, validate_source_transaction_header,
)
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    BoundedCommandPlan, CommandPlan, TransactionError, row_facts, next_row_id,
    row_change, update_change, event_envelope, encode_json_value,
    read_transaction_range, read_saved_transaction_page,
)

SOURCE_HEADER = 'source_resolution_request'
START_HEADER = 'obtain_initialization_request'
_SOURCE = load_event_registry()['events']['SOURCE_RESOLVED']['id']
_START = load_event_registry()['events']['ACTION_STARTED']['id']
_PLAN = load_event_registry()['events']['PLAN_STATUS_CHANGED']['id']
_OBTAIN = int(enum_for('actions.type').OBTAIN_ACTION_OUTPUTS)
_DELETE = int(enum_for('actions.type').DELETE_ACTION_OUTPUTS)
_TERMINAL = frozenset(int(v) for v in enum_for('actions.status') if v.name in {'SUCCEEDED', 'FAILED', 'CANCELED', 'EXPIRED'})


def _one(db, sql, args=()):
    with closing(db.execute(sql, args)) as cursor:
        return cursor.fetchone()


def _pages(db, sql, args, batch):
    """稳定 ID 游标；交出一页以前关闭 SQL cursor。"""
    after = 0
    while True:
        with closing(db.execute(sql, (*args, after, batch))) as cursor:
            rows = tuple(cursor.fetchmany(batch))
        if not rows:
            return
        yield rows
        after = rows[-1][0]
        if len(rows) < batch:
            return


def _boundary(db):
    row = _one(db, 'SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1')
    return HistoryBoundary(*row) if row else HistoryBoundary(0, 0)


def _at(db, table, identity, transaction=None, columns=None):
    """只恢复一个主对象的业务列；新写直接采用 F 前 SQL。"""
    current = row_facts(db, table, identity)
    if current is None or transaction is None:
        return current
    if current['created_event_id'] >= transaction.first_event_id:
        return None
    from camctl.history.events import business_columns
    h = HistoryBoundary(transaction.txn_id - 1, transaction.first_event_id - 1)
    columns = business_columns(table) if columns is None else columns
    return {**current, **read_row_values_at_boundary(db,
        owner=('action' if table == 'actions' else 'plan', identity), table=table,
        row_id=identity, columns=frozenset(columns), current_values=current,
        boundary=h, current_boundary=_boundary(db), validated_transaction=transaction)}


def _spec(action):
    return read_source_spec(encode_json_value(action['input_fields_json']))


@dataclass(frozen=True)
class _Members:
    plan_id: int | None
    predicate: str
    parameters: tuple
    count: int
    failure: ResolveFailure | None = None

    def pages(self, db, batch, upper):
        if self.failure is not None:
            return
        sql = ('SELECT id FROM actions WHERE ' + self.predicate +
               ' AND created_event_id<? AND id>? ORDER BY id LIMIT ?')
        yield from _pages(db, sql, (*self.parameters, upper), batch)


def _members(db, spec, owner, upper):
    """六种原寻址的等值查询；计数和成员枚举使用同一谓词。"""
    captures = ','.join(str(v) for v in sorted(_CAPTURE_TYPES))
    if spec.action_instance_id is not None:
        row = _one(db, 'SELECT plan_id,type FROM actions WHERE id=? AND created_event_id<?',
                   (spec.action_instance_id, upper))
        if row is None:
            return _Members(None, '', (), 0, ResolveFailure.ACTION_NOT_FOUND)
        if row[1] not in _CAPTURE_TYPES:
            return _Members(None, '', (), 0, ResolveFailure.NOT_OUTPUT_SOURCE)
        return _Members(row[0], 'id=?', (spec.action_instance_id,), 1)
    plan = spec.plan_instance_id or owner['plan_id']
    if _one(db, 'SELECT 1 FROM plans WHERE id=? AND created_event_id<?', (plan, upper)) is None:
        return _Members(None, '', (), 0, ResolveFailure.PLAN_NOT_FOUND)
    predicate, args = 'plan_id=?', (plan,)
    if spec.action_name is not None:
        predicate += ' AND name=?'
        args += (spec.action_name,)
        found = _one(db, 'SELECT COUNT(*),MIN(type) FROM actions WHERE ' + predicate +
                     ' AND created_event_id<?', (*args, upper))
        if found[0] != 1:
            return _Members(None, '', (), 0, ResolveFailure.ACTION_NOT_FOUND)
        if found[1] not in _CAPTURE_TYPES:
            return _Members(None, '', (), 0, ResolveFailure.NOT_OUTPUT_SOURCE)
    if spec.group is not None:
        predicate += ' AND group_name=?'
        args += (spec.group,)
        if _one(db, 'SELECT 1 FROM actions WHERE ' + predicate + ' AND created_event_id<? LIMIT 1', (*args, upper)) is None:
            return _Members(None, '', (), 0, ResolveFailure.GROUP_NOT_FOUND)
    predicate += f' AND type IN ({captures})'
    count = _one(db, 'SELECT COUNT(*) FROM actions WHERE ' + predicate + ' AND created_event_id<?', (*args, upper))[0]
    if spec.group is not None and count == 0:
        return _Members(None, '', (), 0, ResolveFailure.NOT_OUTPUT_SOURCE)
    return _Members(plan, predicate, args, count)


def _created_before(table, alias, upper):
    """不可变从行沿所属动作目录定位创建事件；不加载完整正文组。"""
    action_type = load_enum_registry()['history_objects']['action']['id']
    owner = f'{alias}.action_id' if table == 'action_dependencies' else f'(SELECT d.action_id FROM action_dependencies d WHERE d.id={alias}.dependency_id)'
    return (f"EXISTS(SELECT 1 FROM entity_event_links l JOIN history_events e ON e.id=l.event_id "
        f"JOIN json_each(e.body_json,'$.rows') r WHERE l.entity_type={action_type} "
        f"AND l.entity_id={owner} AND l.event_id<{int(upper)} "
        f"AND json_extract(r.value,'$.table')='{table}' AND json_extract(r.value,'$.id')={alias}.id "
        "AND json_extract(r.value,'$.before.exists')=0 AND json_extract(r.value,'$.after.exists')=1)")


def _next_at(db, table, upper, saved):
    if not saved:
        return next_row_id(db, table)
    found = _one(db, f'SELECT t.id FROM {table} t WHERE ' + _created_before(table,'t',upper) + ' ORDER BY t.id DESC LIMIT 1')
    return 1 if found is None else found[0] + 1


def _capacity(first, count):
    ObjectId(first)
    if count and first + count - 1 > MAX_OBJECT_ID:
        raise ConsistencyError('来源事务行编号超出整数范围')


def _dependencies(db, action, upper, batch, saved=False):
    before = ' AND ' + _created_before('action_dependencies','d',upper) if saved else ''
    yield from _pages(db, 'SELECT d.id,d.depends_on_action_id FROM action_dependencies d '
        'WHERE d.action_id=?' + before + ' AND d.id>? ORDER BY d.id LIMIT ?', (action,), batch)


def _dependency_count(db, action, upper, saved=False):
    before = ' AND ' + _created_before('action_dependencies','d',upper) if saved else ''
    return _one(db, 'SELECT COUNT(*) FROM action_dependencies d WHERE d.action_id=?' + before, (action,))[0]


def _selection_count(db, action, upper, saved=False):
    before = ' AND ' + _created_before('obtain_source_selections','s',upper) if saved else ''
    return _one(db, 'SELECT COUNT(*) FROM obtain_source_selections s JOIN action_dependencies d '
                'ON d.id=s.dependency_id WHERE d.action_id=?' + before, (action,))[0]


def _complete_plan(db, action, upper, saved):
    """独立存在性查询；原键只恢复可能影响原计划完成的一行状态。"""
    terminal = ','.join(str(value) for value in sorted(_TERMINAL))
    if saved is None:
        return _one(db, f'SELECT 1 FROM actions WHERE plan_id=? AND id!=? AND status NOT IN ({terminal}) LIMIT 1',
                    (action['plan_id'],action['id'])) is None
    entity = load_enum_registry()['history_objects']['action']['id']
    after = 0
    while True:
        found = _one(db, "SELECT a.id FROM actions a WHERE a.plan_id=? AND a.id!=? AND a.id>? AND a.created_event_id<? "
            "AND COALESCE((SELECT json_extract(r.value,'$.after.values.status') FROM entity_event_links l "
            "JOIN history_events e ON e.id=l.event_id JOIN json_each(e.body_json,'$.rows') r "
            "WHERE l.entity_type=? AND l.entity_id=a.id AND l.event_id<? AND json_extract(r.value,'$.table')='actions' "
            "AND json_extract(r.value,'$.id')=a.id AND json_type(r.value,'$.after.values.status')='integer' "
            f"ORDER BY l.event_id DESC LIMIT 1),0) NOT IN ({terminal}) ORDER BY a.id LIMIT 1",
            (action['plan_id'],action['id'],after,upper,entity,upper))
        if found is None:
            return True
        if _at(db,'actions',found[0],saved,('status',))['status'] not in _TERMINAL:
            return False
        after = found[0]


def qualify(db, kind, action_id, timestamp, batch, key, *, saved=None, first_event_id=None):
    ObjectId(action_id)
    UtcMicros(timestamp)
    if type(batch) is not int or not 1 <= batch <= 64:
        raise ConsistencyError('来源事务每批必须为1至64项')
    upper = saved.first_event_id if saved else (first_event_id or _boundary(db).last_event_id + 1)
    action = _at(db, 'actions', action_id, saved)
    if action is None:
        raise ConsistencyError('原来源动作不存在')
    plan = _at(db, 'plans', action['plan_id'], saved)
    if plan is None or action['cancel_requested']:
        raise ConsistencyError('原来源动作没有计划或已经取消')
    q = dict(kind=kind, action=action, plan=plan, upper=upper, batch=batch, key=str(key), time=timestamp, saved=saved is not None)
    if kind == SOURCE_HEADER:
        if action['type'] not in (_OBTAIN, _DELETE) or action['status'] != 2 or action['execution_started'] != 1 or action['source_resolution_state'] != 1:
            raise ConsistencyError('来源固定要求原执行中、未取消且来源PENDING的产物动作')
        if _dependency_count(db, action_id, upper, saved is not None):
            raise ConsistencyError('原待解析来源已经存在依赖')
        spec = _spec(action)
        members = _members(db, spec, action, upper)
        q.update(spec=spec, members=members,
                 dependency_first=(_next_at(db, 'action_dependencies', upper, saved is not None) if members.count else 1),
                 selection_first=(_next_at(db, 'obtain_source_selections', upper, saved is not None) if members.count and action['type'] == _OBTAIN else 1),
                 complete_plan=members.failure is not None and plan['status'] in (1, 2) and _complete_plan(db, action, upper, saved))
        q['count'] = members.count
        q['selection_count'] = members.count if action['type'] == _OBTAIN else 0
        q['event_count'] = (1 + int(q['complete_plan']) if members.failure else (members.count + batch - 1) // batch + 1)
        _capacity(q['dependency_first'], members.count)
    elif kind == START_HEADER:
        if action['type'] != _OBTAIN or action['status'] != 1 or action['execution_started'] != 0 or action['source_resolution_state'] not in (1, 2):
            raise ConsistencyError('取回开始要求原待开始且来源PENDING或FIXED')
        count = _dependency_count(db, action_id, upper, saved is not None)
        if _selection_count(db, action_id, upper, saved is not None) or (action['source_resolution_state'] == 1 and count):
            raise ConsistencyError('取回开始只能初始化可靠空选择，PENDING来源还须可靠零依赖')
        q.update(count=count, selection_count=count,
                 selection_first=(_next_at(db, 'obtain_source_selections', upper, saved is not None) if count else 1), start_plan=plan['status'] == 1)
        q['event_count'] = (count + batch - 1) // batch + 1 + int(q['start_plan'])
    else:
        raise ConsistencyError('来源事务申请类型不可解释')
    _capacity(q['selection_first'], q['selection_count'])
    return q


def header(q, transaction):
    value = dict(version=1, action_id=q['action']['id'], occurred_at=q['time'],
                 operation_key=q['key'], batch_size=q['batch'], member_count=q['count'],
                 first_selection_id=q['selection_first'],
                 first_event_id=transaction.first_event_id, last_event_id=transaction.last_event_id)
    if q['kind'] == SOURCE_HEADER:
        value.update(source=_spec_details(q['spec']), source_plan_id=q['members'].plan_id,
                     first_dependency_id=q['dependency_first'], complete_plan=q['complete_plan'])
    else:
        value.update(start_plan=q['start_plan'])
    return value


def canonical(db, q, transaction):
    """只读原请求的同一事实页，不从待校验事件行反推预期。"""
    identity = transaction.first_event_id
    action = q['action']
    evidence = {q['kind']: header(q, transaction)}
    def event(kind, reason, rows):
        return event_envelope(identity, transaction.txn_id, kind, reason, rows, q['time'], evidence)
    selection = q['selection_first']
    if q['kind'] == SOURCE_HEADER:
        members = q['members']
        if members.failure is not None:
            error, details = SourceResolution(ResolutionState.FAILED, failure=members.failure).action_error(q['spec'])
            before = {name: action[name] for name in ('source_resolution_state','status','error_code','error_details_json')}
            yield event(_SOURCE, 2, (update_change('actions', action['id'], before,
                dict(source_resolution_state=3, status=4, error_code=error, error_details_json=details)),))
            identity += 1
            if q['complete_plan']:
                yield event(_PLAN, 2, (update_change('plans', action['plan_id'], {'status':q['plan']['status']}, {'status':3}),))
                identity += 1
        else:
            dependency = q['dependency_first']
            count = 0
            for page in members.pages(db, q['batch'], q['upper']):
                rows = []
                for member, in page:
                    rows.append(row_change('action_dependencies', dependency, dict(action_id=action['id'], depends_on_action_id=member)))
                    if action['type'] == _OBTAIN:
                        rows.append(row_change('obtain_source_selections', selection, dict(dependency_id=dependency, status=1, error_code=None, error_details_json=None)))
                        selection += 1
                    dependency += 1
                    count += 1
                yield event(_SOURCE, 1, rows)
                identity += 1
            if count != q['count']:
                raise ConsistencyError('来源成员页没有耗尽可靠原计数')
            before = {name: action[name] for name in ('source_resolution_state','resolved_source_plan_id')}
            yield event(_SOURCE, 1, (update_change('actions', action['id'], before,
                dict(source_resolution_state=2, resolved_source_plan_id=members.plan_id)),))
            identity += 1
    else:
        count = 0
        for page in _dependencies(db, action['id'], q['upper'], q['batch'], q['saved']):
            rows = []
            for dependency, member in page:
                # 既有依赖须指向真实、同来源计划的合格拍摄动作。
                source = _one(db, 'SELECT plan_id,type FROM actions WHERE id=? AND created_event_id<?', (member, q['upper']))
                if source is None or source[0] != action['resolved_source_plan_id'] or source[1] not in _CAPTURE_TYPES:
                    raise ConsistencyError('初始化依赖不属于原固定来源计划的拍摄动作')
                rows.append(row_change('obtain_source_selections', selection, dict(dependency_id=dependency, status=1, error_code=None, error_details_json=None)))
                selection += 1
                count += 1
            yield event(_START, 1, rows)
            identity += 1
        if count != q['count']:
            raise ConsistencyError('开始初始化没有耗尽全部既有依赖')
        yield event(_START, 1, (update_change('actions', action['id'], {'status':1,'execution_started':0}, {'status':2,'execution_started':1}),))
        identity += 1
        if q['start_plan']:
            yield event(_PLAN, 1, (update_change('plans', action['plan_id'], {'status':1}, {'status':2}),))
            identity += 1
    if identity != transaction.last_event_id + 1:
        raise ConsistencyError('规范来源事务与原完整范围不符')


def _result(q):
    from .outputs import ResolveSourcesOutcome, ObtainStartResult
    if q['kind'] == START_HEADER:
        return ObtainStartResult()
    members = q['members']
    if members.failure is None:
        return ResolveSourcesOutcome(True, q['count'], q['selection_count'], members.plan_id)
    error, details = SourceResolution(ResolutionState.FAILED, failure=members.failure).action_error(q['spec'])
    return ResolveSourcesOutcome(False, error_code=error, error_details=details)


def _context(db, event, transaction, q):
    state = {'actions':{}, 'plans':{}, 'action_dependencies':{}, 'obtain_source_selections':{}}
    owners = {}
    action_id = q['action']['id']
    def add(table, identity):
        if identity in state[table]:
            return
        found = row_facts(db, table, identity)
        if found is not None:
            state[table][identity] = found
    add('actions', action_id)
    add('plans', q['action']['plan_id'])
    association = {name:dict(rows) for name,rows in state.items()}
    for row in event.rows:
        add(row.table, row.row_id)
        owners[row.table,row.row_id] = ('plan', row.row_id) if row.table == 'plans' else ('action',action_id)
        if row.table == 'action_dependencies':
            add('actions', row.after.values['depends_on_action_id'])
        if row.table == 'obtain_source_selections':
            dependency = row.after.values['dependency_id']
            add('action_dependencies', dependency)
            # 本事件新dependency用于关联解析，不成为未来资格。
            if dependency not in state['action_dependencies']:
                changed = next(r for r in event.rows if r.table == 'action_dependencies' and r.row_id == dependency)
                association['action_dependencies'][dependency] = {'id':dependency, **changed.after.values}
    scope_type = SourceResolutionReadScope if q['kind'] == SOURCE_HEADER else ObtainInitializationReadScope
    reads = SqlEventReads(db, scope_type(q['action']['plan_id'], q['batch'], action_id))
    return EventContext(transaction, owners, state, {name:{**state[name],**association[name]} for name in state}, read_coverage=ReadCoverage(), current_reads=reads)


def plan_source(scope, command, key, batch, *, start=False):
    from .outputs import ResolveSourcesOutcome, ObtainStartResult, ObtainStartDisposition
    db = scope.connection
    kind = START_HEADER if start else SOURCE_HEADER
    saved = _one(db, 'SELECT id FROM history_transactions WHERE operation_key=?', (str(key),))
    if saved:
        transaction = read_transaction_range(db, saved[0])
        first = read_saved_transaction_page(db, key, transaction, limit=1).items[0]
        raw = first.evidence.get(kind)
        if not isinstance(raw, dict):
            raise TransactionError('原操作键不属于本来源阶段')
        validate_source_transaction_header(raw,kind)
        q = qualify(db, kind, command.action_id, command.occurred_at, raw.get('batch_size'), key, saved=transaction)
        if not start and command.spec != q['spec']:
            raise ConsistencyError('来源解析重送与原保存输入不同')
        expected = canonical(db, q, transaction)
        def actual():
            cursor = None
            while True:
                page = read_saved_transaction_page(db, key, transaction, cursor, 64)
                yield from page.items
                if page.next_cursor is None:
                    return
                cursor = page.next_cursor
        for wanted, stored in zip_longest(expected, actual()):
            if wanted is None or stored is None or replace(stored, change_seq=None) != wanted:
                raise TransactionError('原操作键的完整来源事务与原F前申请不同')
            for row in wanted.rows:
                if row.before.exists:
                    continue
                current = row_facts(db,row.table,row.row_id)
                immutable = load_event_registry()['tables'][row.table]['immutable']
                if current is None or any(not json_equal(current[name],row.after.values[name]) for name in immutable):
                    raise ConsistencyError('原来源事务创建的当前不变身份记录缺失或已改变')
        return CommandPlan((), {}, {}, result=_result(q), read_only=True)
    action = row_facts(db, 'actions', command.action_id)
    if action is None:
        raise ConsistencyError('产物动作不存在')
    if not start and command.spec != _spec(action):
        raise ConsistencyError('来源解析申请与已保存原输入不同')
    if start:
        if action['type'] != _OBTAIN:
            raise ConsistencyError('开始事务只适用于取回动作')
        if action['status'] in _TERMINAL:
            return CommandPlan((), {}, {}, result=ObtainStartResult(ObtainStartDisposition.ALREADY), read_only=True)
        if action['cancel_requested']:
            return CommandPlan((), {}, {}, result=ObtainStartResult(ObtainStartDisposition.REJECTED, 'canceled'), read_only=True)
    elif action['source_resolution_state'] in (2, 3):
        fixed = action['source_resolution_state'] == 2
        count = _one(db, 'SELECT COUNT(*) FROM action_dependencies WHERE action_id=?', (action['id'],))[0]
        selections = _selection_count(db, action['id'], MAX_OBJECT_ID)
        if ((action['type'] == _DELETE and selections)
                or (fixed and action['type'] == _OBTAIN and action['execution_started'] and selections != count)):
            raise ConsistencyError('原固定来源的选择责任不完整或所属类型不适用')
        result = ResolveSourcesOutcome(fixed, count, selections, action['resolved_source_plan_id'], action['error_code'], action['error_details_json'])
        return CommandPlan((), {}, {}, result=result, read_only=True)
    q = qualify(db, kind, command.action_id, command.occurred_at, min(batch,64), key)
    allocation = scope.allocate(q['event_count'])
    transaction = TransactionRange(allocation.txn_id, allocation.first_event_id, allocation.last_event_id)
    return BoundedCommandPlan(canonical(db, q, transaction),
        lambda event, tx: _context(db,event,tx,q), _result(q), lambda db: None)


def required_stream(db, event):
    """按实际分支及原 owner 分类，不用缺头代表无需校验。"""
    if event.event_type == _SOURCE and event.reason in (1, 2):
        return SOURCE_HEADER
    if SOURCE_HEADER in event.evidence:
        return SOURCE_HEADER
    if START_HEADER in event.evidence:
        return START_HEADER
    if event.event_type == _START and event.reason == 1:
        for row in event.rows:
            if row.table in ('obtain_source_selections', 'obtain_items', 'action_dependencies'):
                return START_HEADER
            if row.table == 'actions':
                action = row_facts(db, 'actions', row.row_id)
                if action is not None and action['type'] == _OBTAIN:
                    return START_HEADER
    return None


def _verify_context(db, event, context, reads, q):
    """报告/归属同样使用可靠当前页，不能让伪造字典改变派生结果。"""
    registry = load_event_registry()['tables']
    accepted = reads._kernel_accepted
    if accepted is None and (sum(len(rows) for rows in context.state_rows.values()) > q['batch'] + 2
            or sum(len(rows) for rows in context.association_rows.values()) > 2 * q['batch'] + 2):
        raise ConsistencyError('有界来源上下文只能携带当前页及原 owner/plan')
    def facts(table, identity):
        found = row_facts(db,table,identity)
        if accepted is not None:
            for entry in accepted:
                for row in entry.envelope.rows:
                    if (row.table,row.row_id) == (table,identity):
                        found = ({**(found or {'id':identity}),**row.after.values} if row.after.exists else None)
        return found
    required = {('actions',q['action']['id']),('plans',q['action']['plan_id'])}
    for table, rows in context.state_rows.items():
        if table not in ('actions','plans','action_dependencies','obtain_source_selections'):
            raise ConsistencyError('来源事件上下文超出原业务页')
        columns = frozenset(registry[table]['immutable']) | frozenset(registry[table]['mutable'])
        for identity, supplied in rows.items():
            ObjectId(identity)
            reliable = facts(table,identity)
            if (reliable is None or ('id' in supplied and supplied['id'] != identity)
                    or any(name not in reliable or not json_equal(supplied[name],reliable[name]) for name in columns & supplied.keys())):
                raise ConsistencyError('来源事件上下文与可靠当前事实不同')
            if (table,identity) in required and not columns <= supplied.keys():
                raise ConsistencyError('来源事件缺少可靠完整的原 owner/plan')
    if any(identity not in context.state_rows.get(table,{}) for table,identity in required):
        raise ConsistencyError('来源事件没有原 owner/plan 当前事实')
    references = set(required)
    for row in event.rows:
        if row.table == 'obtain_source_selections':
            references.add(('action_dependencies',row.after.values['dependency_id']))
        elif row.table == 'action_dependencies':
            references.add(('actions',row.after.values['depends_on_action_id']))
    for table,identity in references:
        reliable = facts(table,identity)
        # 当前事件共同新建的固定关系可用于归属，不是未来状态资格。
        for row in event.rows:
            if (row.table,row.row_id) == (table,identity) and row.after.exists:
                reliable = {**(reliable or {'id':identity}),**row.after.values}
        supplied = context.association_rows.get(table,{}).get(identity)
        immutable = registry[table]['immutable']
        if (reliable is None or supplied is None
                or any(name not in supplied or not json_equal(supplied[name],reliable[name]) for name in immutable)):
            raise ConsistencyError('来源事件固定归属关系与可靠原事实不同')


@dataclass(frozen=True)
class _SourceProof:
    scope: object
    transaction: object
    event: object


def bind_stream(db, event, transaction, reads, state, key, context):
    """唯一内核在验证前建立原资格，并在每条实际事件上重绑定。"""
    active = state.get('source_stream')
    needed = required_stream(db,event)
    if active is None and needed is None:
        return
    if active is None:
        raw = event.evidence.get(needed)
        scope_type = SourceResolutionReadScope if needed == SOURCE_HEADER else ObtainInitializationReadScope
        if (event.event_id != transaction.first_event_id or not isinstance(raw, dict)
                or type(reads.scope) is not scope_type):
            raise ConsistencyError('来源事务缺少原F申请或可靠业务读取范围')
        validate_source_transaction_header(raw,needed)
        q = qualify(db, needed, reads.scope.action_id, event.occurred_at, reads.scope.batch_size, key, first_event_id=transaction.first_event_id)
        if reads.scope.plan_id != q['action']['plan_id'] or not json_equal(raw,header(q,transaction)):
            raise ConsistencyError('来源申请头或scope与独立原事实不同')
        if q['event_count'] != transaction.last_event_id-transaction.first_event_id+1:
            raise ConsistencyError('来源事务完整范围与独立原计数不同')
        active = {'q':q, 'scope':reads.scope, 'transaction':transaction, 'iterator':iter(canonical(db,q,transaction)), 'expected':None, 'done':False}
        state['source_stream'] = active
    if active['scope'] != reads.scope or active['transaction'] != transaction or active['done']:
        raise ConsistencyError('实际来源事件改变原scope/事务或在封口后追加')
    expected = next(active['iterator'], None)
    if expected is None or replace(event,change_seq=None) != expected:
        raise ConsistencyError('实际来源事件与原申请的完整规范流不同')
    _verify_context(db,event,context,reads,active['q'])
    active['expected'] = replace(event,change_seq=None)
    reads._source_stream = _SourceProof(reads.scope,transaction,replace(event,change_seq=None))


def seal_stream(reads, validated, state):
    active = state.get('source_stream')
    if active is None:
        return
    event = validated.envelope
    if getattr(reads,'_source_stream',None) != _SourceProof(active['scope'],active['transaction'],replace(event,change_seq=None)) or active['expected'] != replace(event,change_seq=None):
        raise ConsistencyError('来源规范事件未在原scope实际通过全部守卫')
    active['expected'] = None
    if event.event_id == active['transaction'].last_event_id:
        if next(active['iterator'], None) is not None:
            raise ConsistencyError('来源事务末事件没有完整耗尽规范流')
        active['done'] = True


def finish_stream(state):
    active = state.get('source_stream')
    if active is not None and not active['done']:
        raise ConsistencyError('来源事务缺少完整末状态')


def verify_source_guard(event, context):
    """具名守卫只承认内核刚绑定的同一规范事件，不信可伪造bool。"""
    reads = context.current_reads
    proof = getattr(reads, '_source_stream', None)
    if not isinstance(proof,_SourceProof) or proof.event != replace(event,change_seq=None) or proof.transaction != context.transaction or proof.scope != reads.scope:
        raise ConsistencyError('来源/初始化事件缺少内核独立复核的原申请')
