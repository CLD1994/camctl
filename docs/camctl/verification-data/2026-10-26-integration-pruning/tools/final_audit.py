import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

repo=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
local=repo/'.local/verification/integration-pruning-20261026'
workers=Path('/tmp/camctl-prune-20261026')
groups=['capture','bootstrap','outputs','storage','control','adapters','reporting-root']
audits=[json.loads((workers/g/'.local/pruning-audit.json').read_text()) for g in groups]
rows=[row for audit in audits for row in audit['files']]
expected={str(p.relative_to(repo)) for base in ['apps/camctl/tests/integration','tests/integration'] for p in (repo/base).rglob('test_*.py')}|{str(p.relative_to(repo)) for p in (repo/'tests/integration').glob('*.test.mjs')}
assert len(rows)==len(expected)==len({x['path'] for x in rows})
assert {x['path'] for x in rows}==expected


def functions(filename, source):
 result={}
 def visit(body, parents=()):
  for node in body:
   if isinstance(node,ast.ClassDef): visit(node.body,parents+(node.name,))
   elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
    if node.name.startswith('test_'):
     key=filename+'::'+'::'.join(parents+(node.name,))
     assert key not in result,key
     result[key]=ast.dump(node,include_attributes=False)
    visit(node.body,parents+(node.name,'<locals>'))
 visit(ast.parse(source).body)
 return result

before_functions={};after_functions={}
changed_files=set(subprocess.check_output(['git','diff','--name-only','771030c9','HEAD'],cwd=repo,text=True).splitlines())
assert all(p.startswith('apps/camctl/tests/integration/') for p in changed_files),changed_files
for name in sorted(expected):
 if not name.endswith('.py'):continue
 source=(repo/name).read_text()
 current=functions(name,source)
 prior=functions(name,subprocess.check_output(['git','show',f'771030c9:{name}'],cwd=repo,text=True)) if name in changed_files else current
 before_functions.update(prior);after_functions.update(current)
removed=sorted(set(before_functions)-set(after_functions))
added=sorted(set(after_functions)-set(before_functions))
modified=sorted(k for k in set(before_functions)&set(after_functions) if before_functions[k]!=after_functions[k])
entries=[entry for row in rows for entry in row.get('removed',[])]
removed_declarations=[x['node'] for x in entries if '[' not in x['node']]
assert set(removed)==set(removed_declarations)
assert not added
assert set(modified)=={
'apps/camctl/tests/integration/capture/test_finish_reuse.py::test_new_key_after_terminal_recovers_without_reregistering',
'apps/camctl/tests/integration/reporting/test_publish.py::test_success_requires_current_publish_intent',
'apps/camctl/tests/integration/reporting/test_result_error_generation.py::test_exhausted_results_generate_schema_valid_public_errors',
}


def collection(phase):
 data={}
 for path in (local/phase).glob('*/nodes.json'):
  for node in json.loads(path.read_text())['nodeids']:
   assert node not in data,node
   data[node]=path.parent.name
 return data
b=collection('before');a=collection('after')
removed_nodes=sorted(set(b)-set(a));added_nodes=sorted(set(a)-set(b))
assert not added_nodes
assert all(node in removed_declarations or node.split('[',1)[0] in removed_declarations or node in [x['node'] for x in entries] for node in removed_nodes)
for entry in entries:
 for coverage in entry['coverage']:
  file,rest=coverage.split('::',1)
  key=file+'::'+rest.split('[',1)[0]
  assert (repo/file).is_file(),coverage
  if file.endswith('.py'):
   assert key in functions(file,(repo/file).read_text()),coverage
  if '[' in coverage and '/integration/' in coverage:
   assert coverage in a,coverage

summary={'baseline':'771030c9','production_baseline':'980f5ff4','integrated_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'reviewed_files':len(rows),'python_files':len([x for x in rows if x['path'].endswith('.py')]),'node_files':len([x for x in rows if x['path'].endswith('.mjs')]),'decisions':dict(Counter(x['decision'] for x in rows)),'changed_test_files':len(changed_files),'removed_test_functions':len(removed),'removed_parameter_instances':sum('[' in x['node'] for x in entries),'before_test_functions':len(before_functions),'after_test_functions':len(after_functions),'before_collected_nodes':len(b),'after_collected_nodes':len(a),'removed_collected_nodes':removed_nodes,'added_collected_nodes':added_nodes,'modified_retained_tests':modified,'new_test_imports_removed':[],'production_unchanged':True,'unit_test_source_unchanged':True,'full_integration_executed':False}
# 由 root 独立复核补充已存在的精确分类见证，不修改测试。
extra={
'apps/camctl/tests/integration/session/test_handoff.py::TestSubmitRacesAdmissionClose::test_acceptor_present_hands_work_over':['apps/camctl/tests/unit/session/test_handoff.py::TestDecideHandoff::test_pending_work_with_acceptor_handed_over'],
'apps/camctl/tests/integration/session/test_handoff.py::TestSubmitRacesAdmissionClose::test_new_work_keeps_admission_for_current_run':['apps/camctl/tests/unit/session/test_work.py::TestWorkDimensions::test_pending_action_counts_even_when_future','apps/camctl/tests/unit/session/test_work.py::TestWorkDimensions::test_future_pending_is_work'],
}
for row in rows:
 for entry in row.get('removed',[]):
  entry['coverage'].extend(extra.get(entry['node'],[]))
result={'summary':summary,'groups':audits}
(local/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
(local/'change-audit.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
