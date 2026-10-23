"""动作自身行的磁盘恢复副本；业务库只进行有界的短读。"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.capture.segment_models import SegmentSetRef
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.history.events import business_columns, foreign_key_targets, load_event_registry
from camctl.history.replay import reverse_row_values
from camctl.history.snapshots import SnapshotRow
from camctl.persistence.transaction import encode_json_value
from camctl.persistence.derived_storage import configure_derived_connection


MAX_BATCH = 128


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_BATCH:
        raise ValueError('批量必须是1至128的整数')
    return value


@dataclass(frozen=True)
class _OwnerBranch:
    tables: tuple
    columns: tuple
    conditions: tuple
    owner_column: str

    def local_conditions(self, index):
        return tuple(condition.replace(f't{index}.','')
                     for slot,condition in self.conditions if slot == index)

    def query(self):
        joins = ' '.join(f'JOIN {self.tables[i+1]} t{i+1} ON t{i}.{column}=t{i+1}.id'
                         for i,column in enumerate(self.columns))
        conditions = [condition for _,condition in self.conditions]
        conditions.append(f't{len(self.tables)-1}.{self.owner_column}=?')
        return f'SELECT t0.id FROM {self.tables[0]} t0 {joins} WHERE '+ ' AND '.join(conditions)


def _ownership_branches(table, entity='action'):
    """解释唯一归属登记；每条链只沿不可变列及权威外键。"""
    registry = load_event_registry()['tables']
    targets = foreign_key_targets()
    results = []
    def fixed(t,c):
        if c != 'id' and c not in registry[t]['immutable']:
            raise ConsistencyError(f'历史候选归属不是不可变列: {t}.{c}')
    def walk(spec,tables,columns,conditions,depth=0):
        if depth > 8:
            raise ConsistencyError('历史归属链超出限制')
        t,index = tables[-1],len(tables)-1
        def follow(column):
            fixed(t,column)
            parent=targets[(t,column)]
            return (*tables,parent),(*columns,column)
        if 'cases' in spec:
            excluded=[]
            for case in spec['cases']:
                clauses=[]
                for column,rule in case['when'].items():
                    fixed(t,column)
                    if rule == 'present':
                        clauses.append(f't{index}.{column} IS NOT NULL')
                    elif isinstance(rule,list) and all(type(v) is int for v in rule):
                        clauses.append(f't{index}.{column} IN ({",".join(map(str,rule))})')
                    else:
                        raise ConsistencyError('无法解释归属分支条件')
                condition='('+' AND '.join(clauses)+')'
                walk(case['owner'],tables,columns,(*conditions,*excluded,(index,condition)),depth+1)
                excluded.append((index,f'NOT COALESCE({condition},0)'))
            walk(spec['otherwise'],tables,columns,(*conditions,*excluded),depth+1)
        elif 'inherit' in spec:
            nested,links=follow(spec['inherit'])
            walk(registry[nested[-1]]['owner'],nested,links,conditions,depth+1)
        elif spec['via']:
            nested,links=follow(spec['via'][0])
            walk({**spec,'via':spec['via'][1:]},nested,links,conditions,depth+1)
        elif spec['entity'] == entity:
            fixed(t,spec['id'])
            results.append(_OwnerBranch(tables,columns,conditions,spec['id']))
    walk(registry[table]['owner'],(table,),(),())
    return tuple(results)


def ownership_queries(table,entity='action'):
    return tuple(branch.query() for branch in _ownership_branches(table,entity))


@dataclass(frozen=True)
class RowCursor:
    instance_id: str
    handle_id: str
    boundary: HistoryBoundary
    action_id: int
    table: str
    ref: SegmentSetRef | SegmentResultRef | None
    position: int | tuple


class SegmentHistoryHandle:
    """恢复全部通过后才返回的查询句柄；close释放可重建磁盘文件。"""
    def __init__(self, repository, seed, batch_size):
        self.repository = repository
        self.boundary = seed.target
        self.action_id = seed.ref[1]
        self.instance_id = repository._instance_id
        self.identity = uuid4().hex
        self._temporary = TemporaryDirectory(prefix='camctl-history-')
        self._db = None
        self._closed = False
        self._path = Path(self._temporary.name)/'rows.db'
        try:
            self._db = sqlite3.connect(str(self._path), isolation_level=None)
            configure_derived_connection(self._db)
            self._db.execute('CREATE TABLE candidates (tab TEXT NOT NULL, id INTEGER NOT NULL, body TEXT NOT NULL, upper_event INTEGER NOT NULL, PRIMARY KEY(tab,id)) WITHOUT ROWID').close()
            self._db.execute('CREATE TABLE set_refs (scan INTEGER, final_event INTEGER, version INTEGER, id INTEGER NOT NULL, PRIMARY KEY(scan,final_event,version)) WITHOUT ROWID').close()
            self._db.execute('CREATE TABLE row_order (tab TEXT,set_id INTEGER,k1 INTEGER,k2 INTEGER,k3 TEXT,id INTEGER,PRIMARY KEY(tab,set_id,k1,k2,k3,id)) WITHOUT ROWID').close()
            self._db.execute('CREATE UNIQUE INDEX row_order_identity ON row_order(tab,id)').close()
            self._db.execute('CREATE TABLE rows (tab TEXT NOT NULL, id INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(tab,id)) WITHOUT ROWID').close()
        except BaseException:
            self.close()
            raise
        self.seed = seed
        self.batch_size = _limit(batch_size)
        self.ready = False
        self.stats = {'projection_rows': 0, 'events': 0, 'candidate_pages': 0}

    def __enter__(self):
        self._require_ready()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._closed = True
        db, self._db = self._db, None
        try:
            if db is not None:
                db.close()
        finally:
            self._temporary.cleanup()

    def detach(self):
        """在当前所属线程关闭连接，只交接可重建文件及有限描述符。"""
        db,self._db=self._db,None
        if db is not None:
            db.close()
        return self

    def _require_ready(self):
        if self._closed or not self.ready:
            raise ConsistencyError('历史句柄尚未完成恢复或已经关闭')
        if self._db is None:
            self._db=sqlite3.connect(self._path.as_uri()+'?mode=rw',uri=True,isolation_level=None)
            try:
                configure_derived_connection(self._db)
            except BaseException:
                self.detach()
                raise

    def _one(self, sql, args=()):
        with closing(self._db.execute(sql,args)) as cursor:
            return cursor.fetchone()

    def _get(self, table, identity, source='rows'):
        row = self._one(f'SELECT body FROM {source} WHERE tab=? AND id=?',(table,identity))
        return None if row is None else parse_exact_json(row[0])

    def _put(self, table, identity, values):
        self._db.execute('INSERT OR REPLACE INTO rows VALUES (?,?,?)',(table,identity,encode_json_value(values))).close()

    def primary(self):
        self._require_ready()
        return self._get('actions',self.action_id)

    def row(self, table, identity):
        """读取同一 H 的一份自身行，不恢复增长关系。"""
        self._require_ready()
        if table not in load_event_registry()["tables"]:
            raise ConsistencyError("未登记的历史行种类")
        return self._get(table, identity)

    def related_rows(self, table, column=None, value=None):
        """在磁盘恢复副本中按主键分批读取自身关系。"""
        self._require_ready()
        if table not in load_event_registry()["tables"] or (
            column is not None and column not in business_columns(table)
        ):
            raise ConsistencyError("未登记的历史关系")
        after = 0
        predicate = (
            "" if column is None else f" AND json_extract(body,'$.{column}') IS ?"
        )
        while True:
            args = (
                (table, after)
                + (() if column is None else (value,))
                + (self.batch_size,)
            )
            with closing(
                self._db.execute(
                    "SELECT id,body FROM rows WHERE tab=? AND id>?"
                    + predicate
                    + " ORDER BY id LIMIT ?",
                    args,
                )
            ) as cursor:
                page = cursor.fetchmany(self.batch_size)
            if not page:
                return
            for identity, body in page:
                yield SnapshotRow(table, identity, parse_exact_json(body))
            after = page[-1][0]

    def read_set(self,ref):
        """精确读取H处原扫描/版本，正式集合须另由active_ref选择。"""
        self._require_ready()
        from camctl.capture.segment_models import SegmentSetRef
        if not isinstance(ref,SegmentSetRef) or ref.action_id != self.action_id:
            raise ConsistencyError('历史集合引用不属于当前动作')
        found=self._one('SELECT id FROM set_refs WHERE scan=? AND final_event=? AND version=?',
            (ref.scan_id,ref.final_page_event_id,ref.version))
        if found is None:
            raise ConsistencyError('原集合在H处不存在')
        return self._get('segment_sets',found[0])

    def read_result(self,ref):
        self._require_ready()
        if not isinstance(ref,SegmentResultRef) or ref.action_id!=self.action_id:
            raise ConsistencyError('历史结果引用不属于当前动作')
        values=self._get('segment_sets',ref.set_id)
        if values is None or values['version']!=ref.version:
            raise ConsistencyError('原结果集合在H处不存在或版本不同')
        return values

    def _published_set(self):
        """只消费H处唯一正式发布标记，不按最大版本推断。"""
        self._require_ready()
        with closing(self._db.execute("SELECT body FROM rows WHERE tab='segment_sets' AND json_extract(body,'$.published_event_id') IS NOT NULL LIMIT 2")) as query:
            rows=query.fetchall()
        if len(rows)>1:
            raise ConsistencyError('H处动作存在多个正式分段集合')
        if not rows:
            return None
        return parse_exact_json(rows[0][0])

    def active_ref(self):
        values=self._published_set()
        if values is None:return None
        if values['current_attempt_id'] is None:
            raise ConsistencyError('正式结果集合没有完整扫描模型引用')
        return SegmentSetRef(self.action_id,values['current_attempt_id'],values['current_final_page_event_id'],values['version'])

    def active_result_ref(self):
        values=self._published_set()
        return None if values is None else SegmentResultRef(self.action_id,values['id'],values['version'])

    def read_assessment(self,ref):
        from camctl.capture.segment_storage import assessment_from_document
        values=self.read_set(ref)
        if values['assessment_json'] is None:
            raise ConsistencyError('H处原集合尚未保存完整判断')
        return assessment_from_document(values['assessment_json'])

    def read_page(
            self, table: str, *, ref: SegmentSetRef | SegmentResultRef | None = None,
            cursor: RowCursor | None = None, limit: int = MAX_BATCH,
    ) -> Page[SnapshotRow, RowCursor]:
        """自身表按ID续读；引用集合按设备/保存序号的完整键续读。"""
        self._require_ready()
        _limit(limit)
        if table not in load_event_registry()['tables']:
            raise ConsistencyError('未登记的历史行种类')
        previous = 0
        if cursor is not None:
            if (not isinstance(cursor,RowCursor) or cursor.instance_id != self.instance_id
                or cursor.handle_id != self.identity or cursor.boundary != self.boundary
                or cursor.action_id != self.action_id or cursor.table != table or cursor.ref != ref):
                raise ConsistencyError('历史游标与固定实例/H/句柄/集合/种类不符')
            previous = cursor.position
        set_id = None
        if ref is not None:
            if not isinstance(ref,(SegmentSetRef,SegmentResultRef)) or ref.action_id != self.action_id:
                raise ConsistencyError('集合不属于历史动作')
            if isinstance(ref,SegmentResultRef):
                self.read_result(ref)
                set_id=ref.set_id
            else:
                selected_set = self._one('SELECT id FROM set_refs WHERE scan=? AND final_event=? AND version=?',
                    (ref.scan_id,ref.final_page_event_id,ref.version))
                set_id = None if selected_set is None else selected_set[0]
                if set_id is None:
                    raise ConsistencyError('集合在H处不存在')
        if ref is not None and foreign_key_targets().get((table,'set_id')) == 'segment_sets':
            return self._ordered_page(table,set_id,ref,cursor,limit)
        if cursor is not None and (type(previous) is not int or previous < 1 or self._get(table,previous) is None):
            raise ConsistencyError('历史物理行游标不是原顺序位置')
        with closing(self._db.execute('SELECT id,body FROM rows WHERE tab=? AND id>? ORDER BY id LIMIT ?', (table,previous,limit))) as query:
            selected = query.fetchall()
        items = []
        for identity, body in selected:
            values = parse_exact_json(body)
            if ref is None or (identity == set_id if table == 'segment_sets' else values.get('set_id') == set_id):
                items.append(SnapshotRow(table,identity,values))
        following = None
        if selected and self._one('SELECT 1 FROM rows WHERE tab=? AND id>? LIMIT 1',(table,selected[-1][0])):
            following = RowCursor(self.instance_id,self.identity,self.boundary,self.action_id,table,ref,selected[-1][0])
        return Page(tuple(items),following)

    def _ordered_page(self,table,set_id,ref,cursor,limit):
        previous=(0,0,'',0)
        if cursor is not None:
            previous=cursor.position
            if (not isinstance(previous,tuple) or len(previous) != 4
                or any(type(previous[i]) is not int for i in (0,1,3))
                or not isinstance(previous[2],str)):
                raise ConsistencyError('历史顺序游标缺少完整排序键')
            fixed=self._one('SELECT k1,k2,k3,id FROM row_order WHERE tab=? AND set_id=? AND id=?',
                (table,set_id,previous[3]))
            if fixed != previous:
                raise ConsistencyError('历史顺序游标不属于原集合完整顺序')
        query=('SELECT o.k1,o.k2,o.k3,o.id,r.body FROM row_order o JOIN rows r ON r.tab=o.tab AND r.id=o.id '
            'WHERE o.tab=? AND o.set_id=? AND (o.k1,o.k2,o.k3,o.id)>(?,?,?,?) '
            'ORDER BY o.k1,o.k2,o.k3,o.id LIMIT ?')
        with closing(self._db.execute(query,(table,set_id,*previous,limit))) as reader:
            selected=reader.fetchall()
        position=None if not selected else selected[-1][:4]
        more=position is not None and self._one('SELECT 1 FROM row_order WHERE tab=? AND set_id=? AND (k1,k2,k3,id)>(?,?,?,?) LIMIT 1',
            (table,set_id,*position)) is not None
        following=RowCursor(self.instance_id,self.identity,self.boundary,self.action_id,table,ref,position) if more else None
        return Page(tuple(SnapshotRow(table,row[3],parse_exact_json(row[4])) for row in selected),following)

    def iter_rows(self):
        self._require_ready()
        yield from self._iter_rows()

    def _copy_candidates(self):
        from camctl.persistence.repositories.history import _table_row
        specs=[]
        bounds={}
        # C0固定完整永久候选目录。count/max与所有分支来自同一视图。
        with self.repository._read_connection() as connection:
            self.repository._boundary(connection)
            from camctl.history.events import business_tables_for_format
            with closing(connection.execute("SELECT format_version FROM database_metadata WHERE id=1")) as metadata:
                database_format = metadata.fetchone()[0]
            for table in business_tables_for_format(database_format):
                branches=_ownership_branches(table)
                if not branches:
                    continue
                sql=' UNION '.join(branch.query() for branch in branches)
                with closing(connection.execute(f'SELECT COUNT(*),MAX(id) FROM ({sql})',
                        (self.action_id,)*len(branches))) as cursor:
                    count,maximum=cursor.fetchone()
                bounds[table]=maximum or 0
                specs.append((table,branches,count))
        for table,branches,count in specs:
            actual=0
            for branch in branches:
                def copy(index,parent=None):
                    nonlocal actual
                    current=branch.tables[index]
                    if index == len(branch.tables)-1:
                        predicate=f'{branch.owner_column}=?'
                        owner=self.action_id
                    else:
                        predicate=f'{branch.columns[index]}=?'
                        owner=parent
                    conditions=' AND '.join((predicate,*branch.local_conditions(index),'id>?','id<=?'))
                    previous=0
                    while previous < bounds[current]:
                        with self.repository._read_connection() as connection:
                            upper=self.repository._boundary(connection)
                            with closing(connection.execute(f'SELECT id FROM {current} WHERE {conditions} ORDER BY id LIMIT ?',
                                    (owner,previous,bounds[current],self.batch_size))) as cursor:
                                ids=cursor.fetchall()
                            selected=[(identity,_table_row(connection,current,identity)) for (identity,) in ids] if index == 0 else ids
                        if not selected:
                            return
                        self.stats['candidate_pages']+=1
                        for item in selected:
                            identity=item[0]
                            if index:
                                copy(index-1,identity)
                            else:
                                self._db.execute('INSERT INTO candidates VALUES (?,?,?,?)',
                                    (table,identity,encode_json_value(item[1]),upper.last_event_id)).close()
                                actual+=1
                        previous=selected[-1][0]
                copy(len(branch.tables)-1)
            if actual != count:
                raise ConsistencyError('C0候选计数与永久身份范围不符')
            self.stats['projection_rows']+=actual
        with self.repository._read_connection() as connection:
            maximum=self.repository._boundary(connection)
            head=self.repository._object_seed(connection,'action',self.action_id,maximum,
                event_batch_size=self.batch_size,include_snapshot=False,primary_only=True)
        return maximum,head.head

    def _events(self, lower, upper, first, end, forward):
        previous = lower.last_event_id if forward else upper.last_event_id+1
        count = first
        while (count <= end if forward else count >= end):
            events = self.repository._object_event_page(self.seed.ref,lower,upper,previous,count,forward=forward,batch_size=self.batch_size,remaining=abs(end-count)+1)
            for event in events:
                self.stats['events'] += 1
                yield event
                previous = event.event_id
                count += 1 if forward else -1

    def _apply(self,event,forward):
        changes = event.rows if forward else reversed(event.rows)
        for change in changes:
            self._apply_change(event.event_id,change,forward)

    def _apply_change(self,event_id,change,forward):
        key = (change.table,change.row_id)
        candidate = self._one('SELECT body,upper_event FROM candidates WHERE tab=? AND id=?',key)
        if candidate is None or (not forward and event_id > candidate[1]):
            return
        current = parse_exact_json(candidate[0])
        target = self._get(*key)
        columns = business_columns(change.table)
        if not change.after.exists:
            raise ConsistencyError('永久自身行不能被历史事件删除')
        if forward:
            if not change.before.exists:
                if target is not None or set(change.after.values) != columns:
                    raise ConsistencyError('历史创建行不完整或重复')
                target = {k:v for k,v in current.items() if k not in columns}
            elif target is None:
                raise ConsistencyError('历史更新行不存在')
            else:
                for column,value in change.before.values.items():
                    if column not in target or not json_equal(target[column],value):
                        raise ConsistencyError('历史更新前值不连续')
            target.update(change.after.values)
            self._put(*key,target)
        elif not change.before.exists:
            if target is None or not json_equal({k:target[k] for k in columns},dict(change.after.values)):
                raise ConsistencyError('逆向创建事实不连续')
            self._db.execute('DELETE FROM rows WHERE tab=? AND id=?',key).close()
        else:
            if target is None:
                raise ConsistencyError('逆向更新行不存在')
            self._put(*key,reverse_row_values(target,change,frozenset(target)))

    def restore(self,strategy):
        upper,head = self._copy_candidates()
        self._upper,self._head=upper,head
        if strategy == 'current':
            self._db.execute('INSERT INTO rows SELECT tab,id,body FROM candidates').close()
            for event in self._events(self.boundary,upper,head[1],self.seed.floor[1]+1,False):
                self._apply(event,False)
        elif strategy == 'initial':
            self._prove_fixed_values(self.boundary,self.seed.floor[1])
            for event in self._events(INITIAL_BOUNDARY,self.boundary,1,self.seed.floor[1],True):
                self._apply(event,True)
        elif strategy == 'snapshot':
            choice = self.seed.snapshot
            if choice is None:
                raise ConsistencyError('请求的快照恢复路径没有可靠S')
            self._prove_fixed_values(choice.boundary,choice.position[1])
            self._load_snapshot()
            self._replay_proved_changes()
        else:
            raise ValueError('未知历史恢复路径')
        if strategy != 'current':
            self._verify_complete_keys()
            self._verify_fixed_values()
        primary = self._get('actions',self.action_id)
        if self.seed.floor[1]:
            if primary is None:
                raise ConsistencyError('H处对象缺少主行')
            primary.update(last_event_id=self.seed.floor[0],change_count=self.seed.floor[1])
            self._put('actions',self.action_id,primary)
        elif self._one('SELECT 1 FROM rows LIMIT 1'):
            raise ConsistencyError('H处对象尚未创建但仍有自身行')
        self._validate()
        self._validate_sets()
        self.ready = True
        return self

    def _prove_fixed_values(self, lower, lower_count):
        """沿每行Ci逆向一次，分别取得S/H的原write_once事实。

        (S,H]的已验证变化落盘后供快照正向复用；源事件只读取一次。
        fixed表也记录存在性，完整身份校验复用同一份创建事实。
        """
        self._db.execute('CREATE TABLE fixed_work (tab TEXT,id INTEGER,body TEXT,PRIMARY KEY(tab,id)) WITHOUT ROWID').close()
        self._db.execute('CREATE TABLE proved_changes (event INTEGER,ordinal INTEGER,tab TEXT,id INTEGER,before_exists INTEGER,before_body TEXT,after_body TEXT,PRIMARY KEY(event,ordinal)) WITHOUT ROWID').close()
        registry=load_event_registry()['tables']
        previous=('',0)
        while True:
            with closing(self._db.execute('SELECT tab,id,body FROM candidates WHERE (tab,id)>(?,?) ORDER BY tab,id LIMIT ?',(*previous,self.batch_size))) as query:
                selected=query.fetchall()
            if not selected:
                break
            for table,identity,body in selected:
                values=parse_exact_json(body)
                fixed={column:values[column] for column in registry[table]['write_once']}
                self._db.execute('INSERT INTO fixed_work VALUES (?,?,?)',
                    (table,identity,encode_json_value(fixed))).close()
            previous=selected[-1][:2]
        copied_h=False
        for event in self._events(lower,self._upper,self._head[1],lower_count+1,False):
            if not copied_h and event.event_id <= self.boundary.last_event_id:
                self._copy_fixed('fixed_h')
                copied_h=True
            for ordinal,change in reversed(tuple(enumerate(event.rows))):
                key=(change.table,change.row_id)
                candidate=self._one('SELECT upper_event FROM candidates WHERE tab=? AND id=?',key)
                if candidate is None or event.event_id > candidate[0]:
                    continue
                if not change.after.exists:
                    raise ConsistencyError('永久自身行不能被历史删除')
                found=self._one('SELECT body FROM fixed_work WHERE tab=? AND id=?',key)
                if found is None:
                    raise ConsistencyError('一次固定证据缺少原行')
                fixed=parse_exact_json(found[0])
                for column in registry[change.table]['write_once']:
                    if column in change.after.values:
                        if not json_equal(fixed[column],change.after.values[column]):
                            raise ConsistencyError('一次固定事实与Ci后值不连续')
                        if change.before.exists:
                            fixed[column]=change.before.values[column]
                if not change.before.exists:
                    self._db.execute('DELETE FROM fixed_work WHERE tab=? AND id=?',key).close()
                else:
                    self._db.execute('UPDATE fixed_work SET body=? WHERE tab=? AND id=?',
                        (encode_json_value(fixed),*key)).close()
                if event.event_id <= self.boundary.last_event_id:
                    self._db.execute('INSERT INTO proved_changes VALUES (?,?,?,?,?,?,?)',
                        (event.event_id,ordinal,*key,int(change.before.exists),
                         encode_json_value(dict(change.before.values)),encode_json_value(dict(change.after.values)))).close()
        if not copied_h:
            self._copy_fixed('fixed_h')
        self._copy_fixed('fixed_s')
        self._db.execute('DROP TABLE fixed_work').close()

    def _copy_fixed(self,name):
        self._db.execute(f'CREATE TABLE {name} (tab TEXT,id INTEGER,body TEXT,PRIMARY KEY(tab,id)) WITHOUT ROWID').close()
        self._db.execute(f'INSERT INTO {name} SELECT tab,id,body FROM fixed_work').close()

    def _replay_proved_changes(self):
        from camctl.history.events import RowChange,RowImage
        previous=(0,-1)
        while True:
            with closing(self._db.execute('SELECT event,ordinal,tab,id,before_exists,before_body,after_body FROM proved_changes WHERE (event,ordinal)>(?,?) ORDER BY event,ordinal LIMIT ?',(*previous,self.batch_size))) as query:
                selected=query.fetchall()
            if not selected:
                return
            for event,ordinal,table,identity,exists,before,after in selected:
                change=RowChange(table,identity,RowImage(bool(exists),parse_exact_json(before)),RowImage(True,parse_exact_json(after)))
                self._apply_change(event,change,True)
            previous=selected[-1][:2]

    def _fixed_values(self,table,identity,at='fixed_s'):
        found=self._one(f'SELECT body FROM {at} WHERE tab=? AND id=?',(table,identity))
        if found is None:
            raise ConsistencyError('快照包含原边界尚未创建的行')
        return parse_exact_json(found[0])

    def _verify_complete_keys(self):
        missing=self._one('SELECT tab,id FROM fixed_h EXCEPT SELECT tab,id FROM rows LIMIT 1')
        extra=self._one('SELECT tab,id FROM rows EXCEPT SELECT tab,id FROM fixed_h LIMIT 1')
        if missing or extra:
            raise ConsistencyError('H处恢复自身成员不完整或包含后创建行')

    def _verify_fixed_values(self):
        for record in self._iter_rows():
            expected=self._fixed_values(record.table,record.row_id,'fixed_h')
            if any(not json_equal(record.values[column],value) for column,value in expected.items()):
                raise ConsistencyError('恢复结果的一次固定事实不属于H')

    def _iter_rows(self):
        previous=('',0)
        while True:
            with closing(self._db.execute('SELECT tab,id,body FROM rows WHERE (tab,id)>(?,?) ORDER BY tab,id LIMIT ?',(*previous,self.batch_size))) as query:
                selected=query.fetchall()
            if not selected:
                return
            for table,identity,body in selected:
                yield SnapshotRow(table,identity,parse_exact_json(body))
            previous=selected[-1][:2]

    def _validate(self):
        from camctl.history.validators import EventContext,_resolve_owner_spec
        from camctl.persistence.row_validation import ProjectionRowValidator
        from camctl.acceptance.definitions import read_action_spec
        registry = load_event_registry()['tables']
        context = EventContext(self.seed.snapshot.transaction if self.seed.snapshot else self._transaction(),{},_DiskState(self))
        with ProjectionRowValidator(storage_path=Path(self._temporary.name)/'validation.db') as validator:
            previous = ('',0)
            while True:
                with closing(self._db.execute('SELECT tab,id,body FROM rows WHERE (tab,id)>(?,?) ORDER BY tab,id LIMIT ?',(*previous,self.batch_size))) as cursor:
                    selected = cursor.fetchall()
                if not selected:
                    break
                for table,identity,body in selected:
                    values = parse_exact_json(body)
                    validator.validate(table,values)
                    if _resolve_owner_spec(registry[table]['owner'],table,values,context,{}) != ('action',self.action_id):
                        raise ConsistencyError('恢复行归属不属于同H动作')
                    if table == 'actions':
                        read_action_spec(values)
                    if table == 'segment_sets' and values['current_attempt_id'] is not None:
                        self._db.execute('INSERT INTO set_refs VALUES (?,?,?,?)',
                            (values['current_attempt_id'],values['current_final_page_event_id'],values['version'],identity)).close()
                    if foreign_key_targets().get((table,'set_id')) == 'segment_sets':
                        if table == 'segment_members':
                            from camctl.persistence.repositories.segments import _position
                            position=_position(values)
                        elif table == 'segment_check_conclusions':
                            position=(identity,0,'',identity)
                        elif table == 'segment_inspections':
                            position=(values['member_id'],0,'',identity)
                        else:
                            position=(values.get('gap_no',values.get('ordinal')),0,'',identity)
                        self._db.execute('INSERT INTO row_order VALUES (?,?,?,?,?,?)',
                            (table,values['set_id'],*position)).close()
                previous = selected[-1][:2]

    def _validate_sets(self):
        from camctl.capture.segment_models import (
            DefaultScopeState, SegmentDecision, SegmentSetRef,
        )
        from camctl.capture.segment_storage import assessment_from_document, facts_from_document
        previous=0
        while True:
            with closing(self._db.execute("SELECT id,body FROM rows WHERE tab='segment_sets' AND id>? ORDER BY id LIMIT ?",
                    (previous,self.batch_size))) as cursor:
                selected=cursor.fetchall()
            if not selected:
                return
            for identity,body in selected:
                values=parse_exact_json(body)
                def count(table):
                    return self._one('SELECT COUNT(*),MIN(k1),MAX(k1) FROM row_order WHERE tab=? AND set_id=?',(table,identity))
                members=count('segment_members')[0]
                if values['member_count'] is not None and members != values['member_count']:
                    raise ConsistencyError('H处完整成员数量与固定集合计数不同')
                gaps=count('segment_gaps')
                scope=count('segment_scope_members')
                for summary in (gaps,scope):
                    if summary[0] and summary[1:] != (1,summary[0]):
                        raise ConsistencyError('H处缺口或范围保存序号不完整')
                if values['facts_json'] is not None:
                    facts_from_document(values['facts_json'])
                if values['assessment_json'] is not None:
                    assessment=assessment_from_document(values['assessment_json'])
                    ref=SegmentSetRef(self.action_id,values['current_attempt_id'],values['current_final_page_event_id'],values['version'])
                    if isinstance(assessment, SegmentDecision):
                        if assessment.set_ref != ref or assessment.default_scope.set_ref != ref:
                            raise ConsistencyError('H处公共判断改变原集合')
                        selected_scope = assessment.default_scope
                        expected = (
                            0 if selected_scope.state in (
                                DefaultScopeState.UNKNOWN, DefaultScopeState.EMPTY
                            ) else selected_scope.member_count
                        )
                        if type(expected) is not int or expected < 0:
                            raise ConsistencyError('H处公共判断的默认范围数量无效')
                        if assessment.checked_count > expected or scope[0] > expected:
                            raise ConsistencyError('H处检查前缀或范围超过公共判断')
                        if selected_scope.state is DefaultScopeState.UNKNOWN and assessment.checked_count:
                            raise ConsistencyError('H处未知公共范围不能包含已检查前缀')
                        if values['published_event_id'] is not None:
                            if scope[0] != expected:
                                raise ConsistencyError('H处正式公共范围缺少成员')
                            missing=self._one("SELECT 1 FROM row_order o JOIN rows r ON r.tab=o.tab AND r.id=o.id WHERE o.tab='segment_scope_members' AND o.set_id=? AND json_extract(r.body,'$.output_id') IS NULL LIMIT 1",(identity,))
                            if missing:
                                raise ConsistencyError('H处正式公共范围尚未关联真实产物')
                        continue
                    if assessment.set_ref != ref or assessment.target_duration_ms != values['target_duration_ms']:
                        raise ConsistencyError('H处判断改变原集合或目标')
                    if gaps[0] != (0 if assessment.gap_count is None else assessment.gap_count):
                        raise ConsistencyError('H处完整缺口与nullable摘要计数不同')
                    expected=(0 if assessment.default_scope.state is DefaultScopeState.UNKNOWN
                              else assessment.default_scope.member_count)
                    if scope[0] > expected:
                        raise ConsistencyError('H处范围包含原判断以外的成员')
                    if values['published_event_id'] is not None:
                        if scope[0] != expected:
                            raise ConsistencyError('H处正式范围缺少成员')
                        missing=self._one("SELECT 1 FROM row_order o JOIN rows r ON r.tab=o.tab AND r.id=o.id WHERE o.tab='segment_scope_members' AND o.set_id=? AND json_extract(r.body,'$.output_id') IS NULL LIMIT 1",(identity,))
                        if missing:
                            raise ConsistencyError('H处正式范围尚未关联真实产物')
            previous=selected[-1][0]

    def _transaction(self):
        from camctl.persistence.repositories.history import _transaction_range
        with self.repository._read_connection() as connection:
            checked = self.repository._current_range
            return _transaction_range(connection,self.seed.current.txn_id,
                checked if checked is not None and checked.txn_id == self.seed.current.txn_id else None)

    def _load_snapshot(self):
        from camctl.history.snapshot_stream import load_snapshot
        load_snapshot(self)


class _DiskState(Mapping):
    def __init__(self,handle): self.handle=handle
    def __getitem__(self,table): return _DiskRows(self.handle,table)
    def __iter__(self): return iter(load_event_registry()['tables'])
    def __len__(self): return len(load_event_registry()['tables'])


class _DiskRows(Mapping):
    def __init__(self,handle,table): self.handle,self.table=handle,table
    def __getitem__(self,identity):
        value=self.handle._get(self.table,identity)
        if value is None: raise KeyError(identity)
        return value
    def __iter__(self): raise ConsistencyError('归属读取只允许按身份定位')
    def __len__(self): raise ConsistencyError('归属读取只允许按身份定位')
