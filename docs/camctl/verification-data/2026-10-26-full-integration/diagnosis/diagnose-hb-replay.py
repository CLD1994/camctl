from pathlib import Path
import ast,hashlib,json,re,sqlite3,subprocess
ROOT=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
BASE=Path('/tmp/camctl-integration-replay-20261026-u19lkvoe')
OUT=Path('/tmp/camctl-full-272f-diagnosis/root-demos/hasselblad-replay');OUT.mkdir(parents=True,exist_ok=True)
source=json.loads((BASE/'source.json').read_text())
allcases=[];groups=[]
for task in ['003-root-test_hasselblad_photo_roundtrip-shard-2','004-root-test_hasselblad_photo_roundtrip-shard-3','005-root-test_hasselblad_photo_roundtrip-shard-1']:
 a=BASE/'tasks'/task/'artifacts';lines=(a/'output.log').read_text().splitlines();progress=json.loads((a/'progress.json').read_text());groups.append({'task':task,**progress})
 nodes=[x[len('FAILED '):].split(' - ',1)[0] for x in lines if x.startswith('FAILED ')]
 heads=[i for i,l in enumerate(lines) if re.match(r'^_+ .+ _+$',l) and not l.startswith('_ _')]
 assert len(nodes)==len(heads)
 for k,(node,start) in enumerate(zip(nodes,heads)):
  end=heads[k+1] if k+1<len(heads) else next((i for i in range(start+1,len(lines)) if lines[i].startswith('============================== slowest')),len(lines));block=lines[start:end]
  x={'nodeid':node,'phase':'call','task':task,'log':str(a/'output.log'),'trace_start_line':start+1,'trace_end_line':end,'process_errors':[]}
  for offset,line in enumerate(block):
   if 'stdout={' in line:
    e=json.loads(line.split('stdout=',1)[1]);x['process_errors'].append({'line':start+offset+1,'event':e})
  if 'complete_commit_receipt_loss' in node:
   x.update(classification='inherited_production_contract_gap',first_cause='故障注入 verify 的独立只读核实断言失败；正式 HBF-02 保留该要求，实际 UNKNOWN 后使用下一 worker 的普通写连接，未按只读方式打开或启用 query_only',assertion_boundary='原trace只有 conjunction assert，没有打印两个操作数；不能单凭trace断言是哪一半失败。旧连接关闭及后续EXISTING_RW路径由固定源码补证。')
  elif 'report_rebuild' in node:
   x.update(classification='stale_internal_fault_injection',first_cause='同步 intent wrapper 调用 async DbPublicationSession.record_intent 后直接读取 coroutine.kind，实际报告流程先 report_error，90s门等待才最终报超时',unreached='报告发布意图没有被此 wrapper 等待；重建字节、冻结边界和后续发布/ACK业务断言尚未验证')
  else:
   x.update(classification='stale_internal_fault_injection',first_cause='同步 recover wrapper 访问已移除 CaptureRuntime.owned，原真实run因AttributeError退出',unreached='该恢复观察未形成；不重放START及原未知活动/报告断言尚未完整验证')
  allcases.append(x)
  case_name='test_installed_hasselblad_repo0' if 'report_rebuild' in node else 'test_installed_hasselblad_miss0' if 'missing_start' in node else 'test_installed_hasselblad_comp0'
  world=BASE/'tasks'/task/'tmp/pytest'/case_name;x['world']=str(world)
  facts={}
  for marker in ['complete-commit','complete-verifier','report-before-publish','start-recovery-observed','before-start-result']:
   p=world/(marker+'.json');facts[marker]=json.loads(p.read_text()) if p.exists() else None
  db=world/'deployment/state.db';c=sqlite3.connect(f'file:{db}?mode=ro',uri=True);c.row_factory=sqlite3.Row
  queries={
   'actions':'SELECT id,type,status,execution_started,error_code FROM actions',
   'operation_runs':'SELECT id,action_id,kind,status,responsibility_key FROM operation_runs',
   'operation_attempts':'SELECT id,run_id,attempt_no,status,effect_state,intent_event_id,result_event_id FROM operation_attempts',
   'intermediate_files':'SELECT * FROM intermediate_files',
   'outputs':'SELECT * FROM outputs',
   'reports':'SELECT id,status,from_wm,to_wm FROM reports',
  }
  for name,q in queries.items():
   try:facts[name]=[dict(r) for r in c.execute(q)]
   except sqlite3.Error as e:facts[name]={'query_error':str(e),'query':q}
  marker=facts['complete-commit']
  if marker:
   facts['saved_complete_transaction']=[dict(r) for r in c.execute('SELECT id,operation_key,first_event_id,last_event_id FROM history_transactions WHERE operation_key=?',(marker['operation_key'],))]
  c.close();x['facts_file']=f"facts-{task[:3]}-{case_name}.json";(OUT/x['facts_file']).write_text(json.dumps(facts,ensure_ascii=False,indent=2)+'\n')
  if 'report_rebuild' in node:
   x['first_process_diagnosis']=[{'line':i+1,'text':l} for i,l in enumerate((world/'deployment/camctl.log').read_text().splitlines()) if '报告处理失败' in l or 'run 会话失败' in l]
   x['module_evidence']=[{'line':i+1,'text':l} for i,l in enumerate((world/'module.log').read_text().splitlines()) if 'never awaited' in l or 'result=error' in l or 'stdout=' in l]
(OUT/'classified-cases.json').write_text(json.dumps(allcases,ensure_ascii=False,indent=2)+'\n')
summary={'source_commit':source['head'],'source_snapshot_sha256':source['snapshot_sha256'],'collected':sum(x['collected'] for x in groups),'outcomes':{'passed':sum(x['outcomes'].get('passed',0) for x in groups),'failed':sum(x['outcomes'].get('failed',0) for x in groups)},'groups':groups,'classification':{'inherited_production_contract_gap':2,'stale_internal_fault_injection':2},'new_tests_run':False,'source_or_tests_modified':False}
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
# Exact fixed-revision comparisons; no execution of production or tests.
REVS=['a7755d81','272fda66','9fbb734c','354e38f9'];cache={}
def text(rev,path):
 k=(rev,path)
 if k not in cache:cache[k]=subprocess.check_output(['git','show',f'{rev}:{path}'],cwd=ROOT,text=True)
 return cache[k]
def node_at(s,names):
 n=ast.parse(s)
 for name in names:n=next(x for x in n.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and x.name==name)
 return n
checks=[
 ('tests/integration/_hasselblad_photo_entry.py',['_install_fault']),
 ('tests/integration/test_hasselblad_photo_roundtrip.py',['test_installed_hasselblad_complete_commit_receipt_loss_reuses_request']),
 ('tests/integration/test_hasselblad_photo_roundtrip.py',['test_installed_hasselblad_report_rebuild_keeps_frozen_bytes']),
 ('tests/integration/test_hasselblad_photo_roundtrip.py',['test_installed_hasselblad_missing_start_result_never_replays']),
 ('apps/camctl/src/camctl/capture/host_photo.py',['_verify']),
 ('apps/camctl/src/camctl/capture/host_photo.py',['_request_result']),
 ('apps/camctl/src/camctl/persistence/runtime.py',['open_existing']),
 ('apps/camctl/src/camctl/bootstrap/lifecycle.py',['_open_connection']),
 ('apps/camctl/src/camctl/persistence/executor.py',['DbExecutor','_execute']),
 ('apps/camctl/src/camctl/reporting/publication.py',['DbPublicationSession','record_intent']),
 ('apps/camctl/src/camctl/reporting/publication.py',['DbPublicationSession','save_publication']),
 ('apps/camctl/src/camctl/capture/handlers.py',['CaptureRuntime','recover_attempt']),
]
comps=[]
for p,names in checks:
 c={'path':p,'qualified_name':'.'.join(names),'revisions':{}}
 for r in REVS:
  n=node_at(text(r,p),names);d={'line':n.lineno,'end_line':n.end_lineno,'kind':type(n).__name__,'sha256':hashlib.sha256(ast.dump(n,include_attributes=False).encode()).hexdigest()}
  if hasattr(n,'args'):d['signature']=ast.unparse(n.args)
  c['revisions'][r]=d
 c['same_all']=len({d['sha256'] for d in c['revisions'].values()})==1;comps.append(c)
for p,needle in [('docs/camctl/database/consistency-verification.md','**验收 HBF-02**')]:
 c={'path':p,'contract':needle,'revisions':{}}
 for r in REVS:
  s=text(r,p).splitlines();i=next(i for i,x in enumerate(s) if needle in x);c['revisions'][r]={'line':i+1,'text':s[i]}
 c['same_all']=len({d['text'] for d in c['revisions'].values()})==1;comps.append(c)
(OUT/'source-comparison.json').write_text(json.dumps(comps,ensure_ascii=False,indent=2)+'\n')
print('HB',summary['outcomes'],'source comparisons',[(x.get('qualified_name',x.get('contract')),x['same_all']) for x in comps])
