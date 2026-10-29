"""产物来源存储专项：真实 SQLite 查询观察与游标故障边界。"""

import sqlite3
from unittest.mock import create_autospec

from .outputs import observed_query


class QueryProbe:
    """按实际读取表列与游标投影匹配查询，不匹配 SQL 拼写。"""

    def __init__(self, connection, matches, failure=None, *, fetch='fetchone', error=None,
                 fetchmany=None):
        self.connection, self.matches, self.failure = connection, matches, failure
        self.fetch = fetch
        self.error = sqlite3.OperationalError('原查询读取失败') if error is None else error
        self.fetchmany = fetchmany
        self.cursors, self.queries = [], []

    def execute(self, sql, parameters=()):
        cursor, columns, reads = observed_query(self.connection, sql, parameters)
        if not self.matches(reads, columns, parameters):
            return cursor
        self.queries.append((sql, parameters))
        if self.failure == 'execute':
            cursor.close()
            raise self.error
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.description = cursor.description
        probe.close.side_effect = cursor.close
        probe.fetchone.side_effect = cursor.fetchone
        probe.fetchmany.side_effect = cursor.fetchmany
        probe.fetchall.side_effect = cursor.fetchall
        probe.__iter__.side_effect = lambda: iter(cursor)
        if self.failure == 'fetch':
            getattr(probe, self.fetch).side_effect = self.error
        if self.fetchmany is not None:
            probe.fetchmany.side_effect = lambda size: self.fetchmany(cursor, size)
        self.cursors.append(probe)
        return probe

    def __getattr__(self, name):
        return getattr(self.connection, name)


class OperationFailure:
    """实际 SQLite 授权拒绝写入，并保留原异常对象供消费者核对。"""

    def __init__(self, connection, operation, table, *, after=0):
        self.connection, self.operation, self.table = connection, operation, table
        self.after, self.matches, self.error = after, 0, None

    def execute(self, sql, parameters=()):
        matched=False
        def authorize(operation, table, *_):
            nonlocal matched
            if operation==self.operation and table==self.table:
                if not matched:
                    self.matches+=1
                    matched=True
                if self.matches>self.after:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorize)
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.DatabaseError as error:
            self.error=error
            raise
        finally:
            self.connection.set_authorizer(None)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def reads_table(reads, table):
    return any(actual==table for actual,_column in reads)


def captured_source(owned, request, *, start=False, batch_size=1):
    """只从公开来源仓储的真实保存取得事件及有限返回。"""
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .output_planning import capture_operation
    result=[]
    key=new_operation_key()
    def save(copy):
        repository=OutputsRepository(batch_size)
        outcome=(repository.start_obtain_action(request,key,copy) if start
                 else repository.resolve_sources(request,key,copy))
        result.append(outcome.value)
        return outcome
    from camctl.history.validators import NAMED_GUARDS, register_guard
    registered={name:value for name,value in NAMED_GUARDS.items() if hasattr(value,'source_probe_base')}
    for name,value in registered.items():register_guard(name,value.source_probe_base)
    try:
        proposal=capture_operation(owned,save)
    finally:
        for name,value in registered.items():register_guard(name,value)
    return proposal,key,result[0]


class SourceSequence:
    """正式事件与明确 source_read_scope 分别走有限和有界事务入口。"""

    def __init__(self,proposal,request,result,*,start=False,batch_size=1,finite=False,
                 events=None,change_context=None):
        self.proposal,self.request,self.result=proposal,request,result
        self.start,self.batch_size,self.finite=start,batch_size,finite
        self.events=proposal.events if events is None else tuple(events)
        self.change_context=change_context

    def plan(self,scope):
        from copy import deepcopy
        from dataclasses import replace
        from camctl.history.reads import SourceResolutionReadScope,ObtainInitializationReadScope,ReadCoverage
        from camctl.history.validators import EventContext
        from camctl.persistence.event_reads import SqlEventReads
        from camctl.persistence.transaction import CommandPlan,BoundedCommandPlan,row_facts
        allocation=scope.allocate(len(self.events))
        events=tuple(replace(event,event_id=allocation.first_event_id+index,
            transaction_id=allocation.txn_id,change_seq=None) for index,event in enumerate(self.events))
        action_id=self.request.action_id
        owner=self.proposal.state_rows['actions'][action_id]
        read_scope=(ObtainInitializationReadScope if self.start else SourceResolutionReadScope)(
            owner['plan_id'],self.batch_size,action_id)
        tables=('actions','plans','action_dependencies','obtain_source_selections')
        if self.finite:
            state={table:deepcopy(self.proposal.state_rows[table]) for table in tables}
            return CommandPlan(events,self.proposal.owners,state,result=self.result,
                read_coverage=self.proposal.read_coverage,source_read_scope=read_scope)
        def context(event,transaction):
            state={table:{} for table in tables}
            def current(table,identity):
                value=row_facts(scope.connection,table,identity)
                if value is not None:
                    state[table][identity]=value
            current('actions',action_id)
            current('plans',owner['plan_id'])
            for row in event.rows:
                current(row.table,row.row_id)
                if row.table=='action_dependencies':
                    current('actions',row.after.values['depends_on_action_id'])
                if row.table=='obtain_source_selections':
                    current('action_dependencies',row.after.values['dependency_id'])
            association=deepcopy(state)
            for row in event.rows:
                if row.table=='action_dependencies':
                    association[row.table][row.row_id]={'id':row.row_id,**row.after.values}
            value=EventContext(transaction,self.proposal.owners,state,association,
                ReadCoverage(),current_reads=SqlEventReads(scope.connection,read_scope))
            return self.change_context(value,event,transaction) if self.change_context else value
        return BoundedCommandPlan(iter(events),context,self.result,lambda _connection:None)


def commit_source(owned,request,*,start=False,batch_size=1,finite=False,mutation=None):
    """实际规范事件可独立变造，规划代码始终来自测试拥有的公开事件输入。"""
    from dataclasses import replace
    from camctl.persistence.models import DbOutcome,DbOutcomeKind
    from camctl.persistence.transaction import commit_operation
    from camctl.persistence.event_reads import SqlEventReads
    proposal,key,result=captured_source(owned,request,start=start,batch_size=batch_size)
    events=list(proposal.events)
    change=None
    if mutation=='missing_header':events[0]=replace(events[0],evidence={})
    elif mutation in ('wrong_owner','wrong_member'):
        row=events[0].rows[0]
        field='action_id' if mutation=='wrong_owner' else 'depends_on_action_id'
        changed=replace(row,after=replace(row.after,values={**row.after.values,field:11}))
        events[0]=replace(events[0],rows=(changed,*events[0].rows[1:]))
    elif mutation=='wrong_branch':events[0]=replace(events[0],reason=3,evidence={})
    elif mutation=='missing_final':events.pop()
    elif mutation in ('replace_final','reset_and_replace_final'):
        events[-1]=replace(events[0],event_id=events[-1].event_id)
    elif mutation=='changed_time':events[-1]=replace(events[-1],occurred_at=events[-1].occurred_at+1)
    elif mutation=='fake_header_count':
        name=next(iter(events[0].evidence))
        events[0]=replace(events[0],evidence={name:{**events[0].evidence[name],'member_count':True}})
    elif mutation=='missing_context':
        change=lambda context,*_:replace(context,current_reads=None)
    elif mutation=='fake_reads':
        class ForeignReads(SqlEventReads):pass
        change=lambda context,*_:replace(context,current_reads=ForeignReads(
            owned.connection,context.current_reads.scope))
    elif mutation=='fake_context':
        def change(context,event,transaction):
            from copy import deepcopy
            if event.event_id!=transaction.first_event_id:return context
            state=deepcopy(context.state_rows)
            state['actions'][request.action_id].update(status=3,source_resolution_state=2)
            return replace(context,state_rows=state)
    elif mutation=='whole_source_plan':
        def change(context,event,transaction):
            from copy import deepcopy
            from camctl.persistence.transaction import row_facts
            if event.event_id!=transaction.first_event_id:return context
            state=deepcopy(context.state_rows)
            state['actions'].update({identity:row_facts(owned.connection,'actions',identity)
                for identity, in owned.connection.execute('SELECT id FROM actions WHERE plan_id=2')})
            return replace(context,state_rows=state)
    elif mutation not in (None,'clear_progress'):
        raise ValueError(mutation)
    command=SourceSequence(proposal,request,result,start=start,batch_size=batch_size,
        finite=finite,events=events,change_context=change)
    receipt=commit_operation(command,key,owned)
    return DbOutcome(DbOutcomeKind(receipt.kind),value=receipt.result,error=receipt.error)


def observe_source_reads(monkeypatch,inspect):
    """正式具名守卫消费实际内核绑定的 EventContext；原守卫先完成独立检查。"""
    from camctl.history.validators import NAMED_GUARDS,register_guard
    from camctl.history.events import branch_of
    seen=set()
    def wrap(name,original):
        def observed(event,context):
            original(event,context)
            header=next((value for key,value in event.evidence.items()
                if key in ('source_resolution_request','obtain_initialization_request')),None)
            if header is None or context.current_reads is None:return
            guards=branch_of(event.event_type,event.reason)[1]['guards']
            if name!=guards[-1] or event.event_id in seen:return
            seen.add(event.event_id)
            reads=context.current_reads
            owner=reads.required('actions',header['action_id'])
            details={'action':owner,'dependency_first':header.get('first_dependency_id')}
            inspect(reads.connection,event,context,reads,details)
        observed.source_probe_base=original
        return observed
    for name,original in tuple(NAMED_GUARDS.items()):
        monkeypatch.setitem(NAMED_GUARDS,name,original)
        register_guard(name,wrap(name,original))


async def accept_body(owned, body, *, occurred_at):
    """真实文件读取与正式受理服务准备来源主对象，worker 独立关闭。"""
    from types import SimpleNamespace
    from camctl.acceptance.service import AcceptanceContext,CommandMode
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..acceptance.test_acceptance import Catalog
    from .acceptance import accept_document
    from .database import database_path
    result=await accept_document(body,AcceptanceContext(CommandMode.RUN,Catalog(),
        AcceptanceRepository(),SimpleNamespace(utc_micros=lambda:occurred_at)),database_path(owned))
    assert result.plan_id is not None,result
    return result
