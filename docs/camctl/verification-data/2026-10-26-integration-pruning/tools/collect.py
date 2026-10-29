import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

repo=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
control=Path(__file__).parent
phase=sys.argv[1]
out=repo/'.local/verification/integration-pruning-20261026'/phase
out.mkdir(parents=True,exist_ok=False)
python='/workspaces/camctl/apps/camctl/.venv311/bin/python'
base=repo/'apps/camctl/tests/integration'
targets=[p.relative_to(repo) for p in sorted(base.iterdir()) if p.is_dir() and list(p.glob('test_*.py'))]+[Path('tests/integration')]
summary={'phase':phase,'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'python':subprocess.check_output([python,'--version'],text=True).strip(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':[]}
start=time.monotonic()
for target in targets:
    label=target.name if str(target)!='tests/integration' else 'root'
    item=out/label; item.mkdir()
    command=[python,'-m','pytest',str(target),'--collect-only','--continue-on-collection-errors','-q','--rootdir',str(repo),'-p','pruning_collection']
    env=dict(os.environ,PYTHONPATH=f'{control}:{repo}/apps/camctl/src:{repo}/apps/camctl/tests',PYTHONDONTWRITEBYTECODE='1',CAMCTL_PRUNING_COLLECTION_RECEIPT=str(item/'nodes.json'))
    before=time.monotonic()
    with (item/'collection.log').open('w') as stream:
        result=subprocess.run(command,cwd=repo,env=env,stdout=stream,stderr=subprocess.STDOUT)
    nodes=json.loads((item/'nodes.json').read_text()) if (item/'nodes.json').exists() else {}
    record={'target':str(target),'command':command,'exit_code':result.returncode,'wall_seconds':time.monotonic()-before,'node_count':len(nodes.get('nodeids',[])),'collection_failures':nodes.get('collection_failures'),'nodes_sha256':hashlib.sha256((item/'nodes.json').read_bytes()).hexdigest() if (item/'nodes.json').exists() else None}
    summary['groups'].append(record)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:record[k] for k in ['target','exit_code','node_count','collection_failures']},ensure_ascii=False),flush=True)
summary.update(wall_seconds=time.monotonic()-start,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'total_collected':sum(x['node_count'] for x in summary['groups']),'groups_with_errors':sum(x['exit_code']!=0 for x in summary['groups']),'wall_seconds':summary['wall_seconds']},ensure_ascii=False),flush=True)
