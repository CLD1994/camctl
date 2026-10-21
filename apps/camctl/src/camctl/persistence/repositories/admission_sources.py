"""受理来源的 F 绑定、实际前序和完整组成员核验。"""
from contextlib import closing
from dataclasses import dataclass
from heapq import nsmallest
from itertools import zip_longest

from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.reads import AdmissionSourceReadScope
from camctl.outputs.sources import _CAPTURE_TYPES, read_source_spec
from camctl.persistence.transaction import encode_json_value, row_facts

_ACTION = load_event_registry()['events']['ACTION_ADMITTED']['id']
_SOURCE = load_event_registry()['events']['SOURCE_RESOLVED']['id']
_ACTION_OWNER = load_enum_registry()['history_objects']['action']['id']
_OWNER_TYPES = frozenset(int(v) for v in enum_for('actions.type') if v.name in ('OBTAIN_ACTION_OUTPUTS','DELETE_ACTION_OUTPUTS'))


@dataclass(frozen=True)
class _AdmissionBinding:
    connection: object
    scope: object
    transaction: object


def required(event):
    return ((event.event_type == _SOURCE and event.reason == 3)
        or (event.event_type == _ACTION and any(row.table == 'actions'
            and not row.before.exists and row.after.exists
            and row.after.values.get('source_resolution_state') == 2 for row in event.rows)))


def bind(db, event, transaction, reads, state):
    active = state.get('admission')
    scope = reads.scope
    if active is None:
        if type(scope) is not AdmissionSourceReadScope:
            if required(event):
                raise ConsistencyError('受理来源缺少 F 绑定')
            return
        if (event.event_id != transaction.first_event_id
                or row_facts(db, 'plans', scope.plan_id) is not None
                or not 1 <= scope.batch_size <= 128):
            raise ConsistencyError('受理范围必须在 F 绑定真实新计划')
        active = _AdmissionBinding(db, scope, transaction)
        state['admission'] = active
    if (active.connection is not db or type(scope) is not AdmissionSourceReadScope
            or scope != active.scope or transaction != active.transaction):
        raise ConsistencyError('受理事件改变原计划/事务/连接范围')
    for row in event.rows:
        if row.table == 'actions' and not row.before.exists and row.after.exists:
            if event.event_type != _ACTION or row.after.values.get('plan_id') != scope.plan_id or row_facts(db, 'actions', row.row_id) is not None:
                raise ConsistencyError('受理动作不是原计划在 F 前不存在的新对象')
        if row.table == 'plans' and not row.before.exists and row.row_id != scope.plan_id:
            raise ConsistencyError('受理计划改变原 F 身份')
    reads._admission_binding = active


def verify(reads, transaction):
    proof = getattr(reads, '_admission_binding', None)
    if (not isinstance(proof, _AdmissionBinding) or proof.connection is not reads.connection
            or proof.scope != reads.scope or proof.transaction != transaction
            or not reads.connection.in_transaction):
        raise ConsistencyError('受理成员没有内核原 F 绑定')


def _events(db, transaction, accepted, batch, owner_id=None):
    if accepted is not None:
        yield from (entry.envelope for entry in accepted)
        return
    after = transaction.first_event_id - 1
    while after < transaction.last_event_id:
        if owner_id is None:
            sql = 'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE transaction_id=? AND id>? ORDER BY id LIMIT ?'
            args = (transaction.txn_id, after, batch)
        else:
            sql = ('SELECT e.id,e.transaction_id,e.event_type,e.event_version,e.occurred_at,e.clock_status,e.change_seq,e.body_json '
                'FROM entity_event_links l JOIN history_events e ON e.id=l.event_id '
                'WHERE l.entity_type=? AND l.entity_id=? AND e.transaction_id=? AND e.id>? ORDER BY e.id LIMIT ?')
            args = (_ACTION_OWNER, owner_id, transaction.txn_id, after, batch)
        with closing(db.execute(sql, args)) as cursor:
            rows = tuple(cursor.fetchmany(batch))
        if not rows:
            return
        for row in rows:
            yield decode_event_row(row)
        after = rows[-1][0]


def _created(events, table, event_type):
    for event in events:
        if event.event_type == event_type:
            for row in event.rows:
                if row.table == table and not row.before.exists and row.after.exists:
                    yield row.row_id, row.after.values


def finish(db, transaction, accepted, state):
    """有限入口写入前，Bounded 原 SQL 全组写完后；不读未来提案。"""
    proof = state.get('admission')
    if proof is None:
        return
    if proof.connection is not db or proof.transaction != transaction:
        raise ConsistencyError('受理最终核验改变原事务')
    scope = proof.scope
    def events():
        return _events(db, transaction, accepted, scope.batch_size)
    def owners():
        if accepted is not None:
            yield from _created(events(), 'actions', _ACTION)
            return
        after = 0
        while True:
            with closing(db.execute('SELECT id,created_event_id FROM actions WHERE plan_id=? AND created_event_id BETWEEN ? AND ? AND id>? ORDER BY id LIMIT ?',
                    (scope.plan_id, transaction.first_event_id, transaction.last_event_id, after, scope.batch_size))) as cursor:
                rows = tuple(cursor.fetchmany(scope.batch_size))
            if not rows:
                return
            for identity, event_id in rows:
                with closing(db.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=? AND transaction_id=?', (event_id, transaction.txn_id))) as cursor:
                    raw = cursor.fetchone()
                if raw is None:
                    raise ConsistencyError('受理 owner 缺少本组实际创建事件')
                for found, values in _created((decode_event_row(raw),), 'actions', _ACTION):
                    if found == identity:
                        yield found, values
            after = rows[-1][0]
    for owner_id, owner in owners():
        if owner.get('source_resolution_state') != 2:
            continue
        # 同组实际 ACTION_ADMITTED 的原值是唯一输入，当前投影或 proposal 不授予资格。
        if (owner.get('type') not in _OWNER_TYPES or owner.get('plan_id') != scope.plan_id
                or owner.get('resolved_source_plan_id') != scope.plan_id):
            raise ConsistencyError('新 FIXED owner 不属于原受理范围')
        spec = read_source_spec(encode_json_value(owner['input_fields_json']))
        if spec.action_instance_id is not None or spec.plan_instance_id is not None:
            raise ConsistencyError('受理 FIXED owner 必须是本计划来源')
        def matches(source):
            return (source.get('plan_id') == scope.plan_id
                and (spec.action_name is None or source.get('name') == spec.action_name)
                and (spec.group is None or source.get('group_name') == spec.group))
        if accepted is not None and (spec.action_name is not None or spec.group is not None):
            matched = qualified_count = 0
            for _, source in _created(events(), 'actions', _ACTION):
                if matches(source):
                    matched += 1
                    qualified_count += source.get('type') in _CAPTURE_TYPES
            if ((spec.action_name is not None and matched != 1)
                    or matched == 0 or qualified_count == 0):
                raise ConsistencyError('受理命名来源不能把不存在或非产物动作解释为空集合')
        def members():
            # 有限入口复用 accepted；最多 B 个候选，不复制全部新 owner/member。
            if accepted is None:
                from .source_transactions import _members
                qualified = _members(db, spec, owner, transaction.last_event_id + 1)
                if qualified.failure is not None:
                    raise ConsistencyError('受理最终来源没有可靠成员解释')
                for page in qualified.pages(db, scope.batch_size, transaction.last_event_id + 1):
                    yield from (row[0] for row in page)
                return
            after = 0
            while True:
                candidates = nsmallest(scope.batch_size, (identity for identity, source in _created(events(), 'actions', _ACTION)
                    if identity > after and matches(source) and source.get('type') in _CAPTURE_TYPES))
                if not candidates:
                    return
                yield from candidates
                after = candidates[-1]
        def dependencies():
            groups = 0
            actual_events = _events(db, transaction, accepted, scope.batch_size, owner_id)
            for event in actual_events:
                if event.event_type != _SOURCE or event.reason != 3:
                    continue
                rows = (row for row in event.rows if row.table == 'action_dependencies'
                        and row.after.values.get('action_id') == owner_id)
                first = next(rows, None)
                if first is None:
                    continue
                groups += 1
                if groups != 1:
                    raise ConsistencyError('受理 owner 只能用一个完整成员事件')
                yield first.after.values.get('depends_on_action_id')
                for row in rows:
                    yield row.after.values.get('depends_on_action_id')
        for expected, actual in zip_longest(members(), dependencies()):
            if expected is None or actual is None or expected != actual:
                raise ConsistencyError('受理最终成员没有双向完整覆盖实际原来源')
