"""在外部替换函数作可行性对照；源码文件保持不变。"""
from pathlib import Path
from dataclasses import asdict, replace
import ast, collections, difflib, gc, hashlib, inspect, json, os, platform
import shutil, sys, time, tracemalloc

RUN=Path(__file__).resolve().parent
META=json.loads((RUN/'metadata.json').read_text())
SOURCE=Path(META['source'])
sys.path.insert(0,str(SOURCE/'apps/camctl/scripts'))
import sync_resources
sync_resources.sync_package_resources(SOURCE/'apps/camctl')
from integration.persistence import test_segment_transaction_batches as test
from camctl.persistence.repositories import result_pages, segment_transactions
from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
from camctl.contracts.values import OperationKey
from camctl.history.decoding import decode_event_row
import camctl

def event(kind,**fields):
    with (RUN/'progress.jsonl').open('a') as f:
        f.write(json.dumps({'utc':time.time(),'kind':kind,**fields},ensure_ascii=False)+'\n')

def replace_once(text,old,new):
    assert text.count(old)==1,old
    return text.replace(old,new,1)

def compile_function(module,source):
    namespace=module.__dict__.copy()
    exec(compile(source,str(RUN/'candidate-functions.py'),'exec'),namespace)
    name=ast.parse(source).body[0].name
    return namespace[name]

original_page=result_pages._verify_page_relations
original_scope=segment_transactions._verify_scope_members
original_canonical=segment_transactions._canonical_events
page_text=inspect.getsource(original_page)
scope_text=inspect.getsource(original_scope)
canonical_text=inspect.getsource(original_canonical)

local_page=replace_once(page_text,'    expected_created = []\n',
    '    file_id_by_identity = dict(file_ids)\n    created_event_by_file = {}\n    expected_created = []\n')
local_page=replace_once(local_page,'        if transaction.first_event_id <= facts["created_event_id"] <= transaction.last_event_id:\n',
    '        created_event_by_file[file_id] = facts["created_event_id"]\n        if transaction.first_event_id <= facts["created_event_id"] <= transaction.last_event_id:\n')
local_page=replace_once(local_page,'        file_id = dict(file_ids).get(identity)\n',
    '        file_id = file_id_by_identity.get(identity)\n')
local_page=replace_once(local_page,
    '        if facts := row_facts(connection, "device_files", file_id):\n            if facts["created_event_id"] != create.event_id:\n                raise ConsistencyError("原首次发现的持久文件创建事件身份不同")\n',
    '        if created_event_by_file[file_id] != create.event_id:\n            raise ConsistencyError("原首次发现的持久文件创建事件身份不同")\n')
local_page=replace_once(local_page,
    '    expected_create_order = [item for item in file_ids if any(\n        identity == item[0] and file_id == item[1] for identity, file_id, _ in expected_created_ids)]\n',
    '    expected_create_order = [(identity, file_id) for identity, file_id, _ in expected_created_ids]\n')

def structure_once(text):
    text=replace_once(text,'        validate_event_structure(create)\n','')
    return replace_once(text,'    validate_event_structure(index)\n','')

local_scope=replace_once(scope_text,'        cursor=None\n',
    '        setrow=storage._set(reader.connection,request.ref,fixed=True)\n        cursor=None\n')
local_scope=replace_once(local_scope,
    '                candidate=storage.segment_output_candidate(request.ref,stored.member_id,reader)\n',
    '                candidate=storage._output_candidate(reader.connection,setrow,stored)\n')

local_canonical=replace_once(canonical_text,'    originals=previews=0\n',
    '    originals=previews=0\n    preview_seen=False\n')
local_canonical=replace_once(local_canonical,
    '    for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):\n        for candidate in _candidates(request,reader,header[\'batch_size\']):\n',
    '    for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):\n        if wanted is OutputKind.PREVIEW and not preview_seen:continue\n        for candidate in _candidates(request,reader,header[\'batch_size\']):\n            if candidate.kind is OutputKind.PREVIEW:preview_seen=True\n')

variants={
    'page_local':(compile_function(result_pages,local_page),original_scope,original_canonical),
    'structure_once':(compile_function(result_pages,structure_once(page_text)),original_scope,original_canonical),
    'member_local':(original_page,compile_function(segment_transactions,local_scope),compile_function(segment_transactions,local_canonical)),
    'combined':(compile_function(result_pages,structure_once(local_page)),compile_function(segment_transactions,local_scope),compile_function(segment_transactions,local_canonical)),
    'original':(original_page,original_scope,original_canonical),
}
candidate_text='\n\n'.join([structure_once(local_page),local_scope,local_canonical])
(RUN/'candidate-functions.py').write_text(candidate_text)
patch=''
for path,before,after in [
    ('apps/camctl/src/camctl/persistence/repositories/result_pages.py',page_text,structure_once(local_page)),
    ('apps/camctl/src/camctl/persistence/repositories/segment_transactions.py',scope_text,local_scope),
    ('apps/camctl/src/camctl/persistence/repositories/segment_transactions.py',canonical_text,local_canonical)]:
    patch+=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=path+'（原函数）',tofile=path+'（实验函数）'))
(RUN/'candidate-preview.patch').write_text(patch)

# 原测试的计数包装直接从其AST取得。
node=next(n for n in ast.parse(Path(test.__file__).read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='test_fixed_batch_write_and_verification_have_bounded_residency')
ns={};exec(compile(ast.Module(body=[n for n in node.body if isinstance(n,ast.ClassDef)],type_ignores=[]),str(test.__file__),'exec'),ns)
MeasuredConnection=ns['MeasuredConnection']

prior=Path(META['prior_experiment']);old=json.loads((prior/'metadata.json').read_text())
path=Path(old['database_path']);frozen=prior/'frozen-state.db'
assert hashlib.sha256(frozen.read_bytes()).hexdigest()==old['frozen_sha256']
owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
row=owned.connection.execute('SELECT operation_key,first_event_id FROM history_transactions ORDER BY id DESC LIMIT 1').fetchone()
key=OperationKey(row[0]);raw=owned.connection.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(row[1],)).fetchone()
request=segment_transactions._header_request(decode_event_row(raw).evidence['segment_finish_request'])
owned.connection.close()
results=[]

def restore():
    assert not Path(str(path)+'-wal').exists() and not Path(str(path)+'-shm').exists()
    shutil.copy2(frozen,path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==old['frozen_sha256']
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    assert hashlib.sha256('\n'.join(owned.connection.iterdump()).encode()).hexdigest()==old['baseline_logical_sha256']
    return owned

def measured_call(label,traced,fn,*args,**kwargs):
    event('operation_start',label=label,tracemalloc=traced)
    gc.collect()
    if traced:tracemalloc.start();tracemalloc.reset_peak()
    start=time.perf_counter()
    try:return fn(*args,**kwargs)
    finally:
        elapsed=time.perf_counter()-start
        peak=tracemalloc.get_traced_memory()[1] if traced else None
        if traced:tracemalloc.stop()
        measured_call.last={'wall_s':elapsed,'peak_bytes':peak}
        event('operation_end',label=label,**measured_call.last)

for name,variant,traced in [
    ('original-before','original',False),('page-local','page_local',False),
    ('structure-once','structure_once',False),('member-local','member_local',False),
    ('combined','combined',False),('original-after','original',False),
    ('original-memory','original',True),('combined-memory','combined',True),
]:
    result_pages._verify_page_relations,segment_transactions._verify_scope_members,segment_transactions._canonical_events=variants[variant]
    owned=restore()
    try:
        stats={'max_fetch':0,'fetched_rows':0,'sql_calls':0,'whole_group_checks':0}
        next_tx=owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
        measured=replace(owned,connection=MeasuredConnection(owned.connection,stats,next_tx))
        saved=measured_call(name+'/write',traced,segment_transactions.finish_segment_recording,request,key,measured,batch_size=META['B'])
        write=measured_call.last.copy();assert saved.kind=='completed',saved.error;write_stats=stats.copy()
        verified=measured_call(name+'/verify',traced,segment_transactions.verify_segment_finish,request,key,measured,execution_finished=True,batch_size=META['B'])
        verify=measured_call.last.copy();assert verified==saved.result and verified.output_count==META['N']
        after=hashlib.sha256('\n'.join(owned.connection.iterdump()).encode()).hexdigest()
        assert after==old['result_logical_sha256'],name
        assert stats['max_fetch']<=128 and stats['whole_group_checks']<=3
        results.append({'mode':name,'variant':variant,'tracemalloc':traced,'write':write,'verify':verify,
            'combined_wall_s':write['wall_s']+verify['wall_s'],'write_stats':write_stats,'total_stats':stats.copy(),
            'result':asdict(saved.result),'after_logical_sha256':after})
        (RUN/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str)+'\n')
    finally:owned.connection.close()

result_pages._verify_page_relations,segment_transactions._verify_scope_members,segment_transactions._canonical_events=variants['original']
META.update(python=platform.python_version(),sqlite=__import__('sqlite3').sqlite_version,camctl_file=camctl.__file__,
    frozen_sha256=old['frozen_sha256'],baseline_logical_sha256=old['baseline_logical_sha256'],result_logical_sha256=old['result_logical_sha256'],
    reused_physical_database_path=str(path),note='外部函数实验；各档一次，原始档前后各一次；trace仅原始与合并档。所有主源码文件未改，原提案/守卫清空及内存上限保持。此全原片样本不验证真实预览、损坏输入或全部容量规模。')
(RUN/'metadata.json').write_text(json.dumps(META,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(results,ensure_ascii=False,default=str))
