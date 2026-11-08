"""从正式仓储取得历史事件，分别验证静态事实和真实事务。"""

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
import sqlite3

from camctl.contracts.values import new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import business_tables_for_format, foreign_key_targets, load_event_registry
from camctl.history.reads import ReadCoverage
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import CommandPlan, commit_operation, row_facts


@dataclass(frozen=True)
class StoredProposal:
    """测试拥有的完整事务输入；不持有运行时或生产校验上下文。"""
    events: tuple
    owners: dict
    state_rows: dict
    read_coverage: ReadCoverage
    transaction_rows: dict | None = None
    product_read_scope: object = None
    plan_read_scope: object = None


def read_state(connection):
    """按权威业务表登记读取独立行事实，只供小型存储专项夹具。"""
    state = {}
    version, = connection.execute("SELECT format_version FROM database_metadata").fetchone()
    for table in business_tables_for_format(version):
        with closing(connection.execute(f'SELECT id FROM "{table}"')) as cursor:
            ids = tuple(row[0] for row in cursor)
        state[table] = {identity: row_facts(connection, table, identity) for identity in ids}
    return state


def complete_reads(connection, state, probe_ids=()):
    """为已完整扫描的小型夹具声明等值范围及明确引用的空身份。"""
    def identities(value):
        if type(value) is int and value > 0:
            yield value
        elif isinstance(value, str) and value.isdecimal() and int(value) > 0:
            yield int(value)
        elif isinstance(value, dict):
            for member in value.values():
                yield from identities(member)
        elif isinstance(value, (tuple, list)):
            for member in value:
                yield from identities(member)
    probes = frozenset(identities(state)) | frozenset(probe_ids)
    ranges = {}
    for table in state:
        columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                        if 'INT' in row[2].upper())
        for column in columns:
            ranges[table, column] = probes
    return ReadCoverage(ranges)


def proposal_state(owned, *, owners=None):
    state = read_state(owned.connection)
    return StoredProposal((), owners or {}, state, complete_reads(owned.connection, state))


def capture_operation(owned, operation):
    """在独立 SQLite 副本经公开仓储执行，原库保持事务前状态。"""
    assert not owned.connection.in_transaction
    proposal = proposal_state(owned)
    with closing(sqlite3.connect(':memory:')) as connection:
        owned.connection.backup(connection)
        foreign_keys, = owned.connection.execute('PRAGMA foreign_keys').fetchone()
        connection.execute(f'PRAGMA foreign_keys={foreign_keys}')
        previous = connection.execute('SELECT COALESCE(MAX(id),0) FROM history_events').fetchone()[0]
        outcome = operation(OwnedConnection(connection, owned.metadata))
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        with closing(connection.execute(
                'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json '
                'FROM history_events WHERE id>? ORDER BY id', (previous,))) as cursor:
            events = tuple(decode_event_row(row) for row in cursor)
        assert events, outcome.value
        final = read_state(connection)
        owners = {}
        targets = foreign_key_targets()
        tables = load_event_registry()['tables']

        def matches(facts, column, condition):
            if condition == "present":
                return facts.get(column) is not None
            if isinstance(condition, list):
                return facts.get(column) in condition
            raise AssertionError(f"未提供归属登记样本: {condition!r}")

        def owner(spec, table, facts):
            if 'cases' in spec:
                for case in spec['cases']:
                    if all(matches(facts, column, condition)
                           for column, condition in case['when'].items()):
                        return owner(case['owner'], table, facts)
                return owner(spec['otherwise'], table, facts)
            if 'inherit' in spec:
                column = spec['inherit']
                target = targets[table, column]
                return owner(tables[target]['owner'], target, final[target][facts[column]])
            for column in spec.get('via', ()):
                table = targets[table, column]
                facts = final[table][facts[column]]
            return spec['entity'], facts[spec['id']]

        for event in events:
            for row in event.rows:
                owners[row.table, row.row_id] = owner(
                    tables[row.table]['owner'], row.table, final[row.table][row.row_id])
        return replace(proposal, events=events, owners=owners)


class EventSequence:
    """公开事务命令：事件序号由本次真实写事务分配。"""
    def __init__(self, events, proposal, *, plan_read_scope=None):
        self.events, self.proposal = tuple(events), proposal
        self.plan_read_scope=plan_read_scope

    def plan(self, scope):
        allocation=scope.allocate(len(self.events))
        events=tuple(replace(event,event_id=allocation.first_event_id+index,
                             transaction_id=allocation.txn_id,change_seq=None)
                     for index,event in enumerate(self.events))
        return CommandPlan(events,self.proposal.owners,self.proposal.state_rows,
                           read_coverage=self.proposal.read_coverage,
                           product_read_scope=self.proposal.product_read_scope,
                           plan_read_scope=self.plan_read_scope or self.proposal.plan_read_scope)


def validate_static_event(event, proposal):
    """验证 H1 的静态当前/完整提案契约，不宣称该输入组可以提交。

    正式依据为 history 模块计划 H1 和 module-contracts 的当前事件事实读取。
    事件由公开仓储实际保存取得；真实登记及业务守卫验证静态独立事实。
    真实 SQL 原子性、顺序和回滚另由 EventSequence/commit_operation 验证。
    """
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, validate_event

    events = proposal.events or (event,)
    context = EventContext(
        TransactionRange(event.transaction_id,
                         min(item.event_id for item in events),
                         max(item.event_id for item in events)),
        proposal.owners, proposal.state_rows, proposal.transaction_rows,
        proposal.read_coverage)
    return validate_event(event, context)


def storage_fault(connection, target):
    """故障目标指定动作与表，由 SQLite 授权边界匹配实际操作。"""
    from .outputs import SqliteOperationFault
    words=target.split()
    operation={'INSERT':sqlite3.SQLITE_INSERT,'UPDATE':sqlite3.SQLITE_UPDATE,'DELETE':sqlite3.SQLITE_DELETE}[words[0]]
    table=words[2] if words[0] in ('INSERT','DELETE') else words[1]
    return SqliteOperationFault(connection,operation,table)


def apply_values(state, event):
    for row in event.rows:
        if not row.after.exists:
            state.get(row.table,{}).pop(row.row_id,None)
        else:
            state.setdefault(row.table,{}).setdefault(row.row_id,{}).update(row.after.values)
            state[row.table][row.row_id]['id']=row.row_id


def event_case(proposal, event_type):
    index=next(index for index,event in enumerate(proposal.events) if event.event_type==event_type)
    state=deepcopy(proposal.state_rows)
    for event in proposal.events[:index]:
        apply_values(state,event)
    final=deepcopy(state)
    for event in proposal.events[index:]:
        apply_values(final,event)
    return proposal.events[index],replace(proposal,state_rows=state,transaction_rows=final)


def observe_ranges(proposal, connection, *queries):
    """按指定等值范围实际读取并证明可靠空集，供后续新成员事件使用。"""
    ranges = dict(proposal.read_coverage.ranges)
    for table, column, identity in queries:
        with closing(connection.execute(f'SELECT id FROM "{table}" WHERE "{column}"=?', (identity,))) as cursor:
            rows = tuple(cursor)
        for row_id, in rows:
            proposal.state_rows.setdefault(table, {})[row_id] = row_facts(connection, table, row_id)
        ranges[table, column] = ranges.get((table, column), frozenset()) | {identity}
    return replace(proposal, read_coverage=ReadCoverage(ranges))


class ScopedEventSequence(EventSequence):
    """有界事务按每条事件的已写当前状态提供所属窄读取范围。"""

    def __init__(self, events, proposal, scope_for_event, *, state_transform=None,
                 caller_obtain_check_reads=None):
        super().__init__(events, proposal)
        self.scope_for_event = scope_for_event
        self.state_transform = state_transform
        self.caller_obtain_check_reads = caller_obtain_check_reads

    def plan(self, scope):
        from camctl.history.validators import EventContext
        from camctl.persistence.event_reads import SqlEventReads
        from camctl.persistence.transaction import BoundedCommandPlan

        allocation = scope.allocate(len(self.events))
        events = tuple(replace(event, event_id=allocation.first_event_id + index,
                               transaction_id=allocation.txn_id, change_seq=None)
                       for index, event in enumerate(self.events))

        relations = deepcopy(self.proposal.state_rows)
        for event in events:
            apply_values(relations, event)

        def current(event, transaction):
            state = read_state(scope.connection)
            if self.state_transform is not None:
                state = self.state_transform(deepcopy(state), event)
            read_scope = self.scope_for_event(event)
            return EventContext(
                transaction, self.proposal.owners, state, relations,
                read_coverage=complete_reads(scope.connection, state,
                    (row.row_id for item in events for row in item.rows)),
                current_reads=(None if read_scope is None else SqlEventReads(scope.connection, read_scope)),
                obtain_check_reads=self.caller_obtain_check_reads)

        return BoundedCommandPlan(events, current, None, lambda connection: None)
