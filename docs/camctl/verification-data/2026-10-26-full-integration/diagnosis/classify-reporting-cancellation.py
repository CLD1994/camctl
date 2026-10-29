from pathlib import Path
import ast, collections, hashlib, json, subprocess
ROOT=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
OUT=Path('/tmp/camctl-full-272f-diagnosis')
REVS=['a7755d81','272fda66','9fbb734c']
cache={}
def get(rev,path):
 k=(rev,path)
 if k not in cache:cache[k]=subprocess.check_output(['git','show',f'{rev}:{path}'],cwd=ROOT,text=True)
 return cache[k]
def extract(source,names):
 tree=ast.parse(source);node=tree
 for name in names:
  matches=[x for x in node.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and x.name==name]
  if len(matches)!=1:raise ValueError((names,name,len(matches)))
  node=matches[0]
 return node

def comparison(path,names):
 d={}
 for rev in REVS:
  n=extract(get(rev,path),names);d[rev]={'line':n.lineno,'end_line':n.end_lineno,'kind':type(n).__name__,'ast_sha256':hashlib.sha256(ast.dump(n,include_attributes=False).encode()).hexdigest()}
  if hasattr(n,'args'):d[rev]['signature']=ast.unparse(n.args)
 return {'path':path,'qualified_name':'.'.join(names),'revisions':d,'same_all':len({x['ast_sha256'] for x in d.values()})==1}
for group in ['reporting','cancellation']:
 folder=OUT/group;cases=json.loads((folder/'failure-identities.json').read_text())
 for x in cases:
  e=x['error']
  if group=='reporting':
   c='owned_connection_as_database' if 'has no attribute \'write\'' in e else 'old_runtime_owned_argument' if 'unexpected keyword argument \'owned\'' in e else 'context_manager_as_capture_runtime'
  else:
   if 'coroutine' in e or e=='TimeoutError':c='unawaited_capture_factory'
   elif 'has no attribute \'write\'' in e:c='owned_connection_as_database'
   elif 'unexpected keyword argument \'owned\'' in e:c='old_runtime_owned_argument'
   elif 'motor_permits' in e:c='obsolete_eligibility_keyword'
   elif 'CancellationRepository() takes no arguments' in e:c='obsolete_repository_constructor'
   else:c='executed_capture_fixture_missing_activity'
  x['classification']=c
  x['first_cause_kind']='stale_internal_interface_or_fixture'
  x['business_coverage']='该首因阻断后续业务断言；不能记为功能通过'
 (folder/'classified-cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
 comps=[];seen=set()
 for x in cases:
  bits=x['nodeid'].split('::');p=bits[0];names=[b.split('[',1)[0] for b in bits[1:]];key=(p,tuple(names))
  if key in seen:continue
  seen.add(key);comps.append(comparison(p,names))
 (folder/'failed-test-source-comparison.json').write_text(json.dumps(comps,ensure_ascii=False,indent=2)+'\n')
 stats={'failed':len(cases),'phase':dict(collections.Counter(x['phase'] for x in cases)),'classification':dict(collections.Counter(x['classification'] for x in cases)),'distinct_failed_test_functions':len(comps),'test_functions_identical_across_three_revisions':sum(x['same_all'] for x in comps)}
 (folder/'classification-summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n');print(group,stats)
checks=[
 ('apps/camctl/src/camctl/acceptance/service.py',['accept_input']),
 ('apps/camctl/src/camctl/reporting/publication.py',['DbPublicationSession','record_intent']),
 ('apps/camctl/src/camctl/cancellation/service.py',['CancellationRuntime']),
 ('apps/camctl/src/camctl/cancellation/settlement.py',['TargetSettlement']),
 ('apps/camctl/src/camctl/cancellation/rules.py',['load_eligibility_facts']),
 ('apps/camctl/src/camctl/persistence/repositories/cancellation.py',['CancellationRepository']),
 ('apps/camctl/src/camctl/persistence/repositories/capture_facts.py',['start_facts_from_summary']),
 ('apps/camctl/src/camctl/bootstrap/capture_assembly.py',['session_capture_assembly','factory']),
 ('apps/camctl/tests/integration/capture/test_host_photo_flow.py',['_world']),
 ('apps/camctl/tests/integration/history/test_report_scope.py',['_runtime']),
 ('apps/camctl/src/camctl/capture/dispatch.py',['dispatch_ready']),
]
records=[]
for p,names in checks:records.append(comparison(p,names))
for group in ['reporting','cancellation']:(OUT/group/'interface-source-comparison.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
print('interfaces',[(x['qualified_name'],x['same_all']) for x in records])
