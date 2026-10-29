from collections import defaultdict
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import shutil
import sys

label=sys.argv[1]
evidence=Path('/tmp/camctl-sync8db-evidence/osmo')
runtime=Path(sys.argv[2]) if len(sys.argv)>2 else Path('/tmp/camctl-sync8db-osmo-runtime-ojv22ku3')
result=json.loads((evidence/(label+'-result.json')).read_text())
launcher=json.loads((evidence/(label+'-budget/launcher.json')).read_text())
stages=json.loads((evidence/(label+'-stages.json')).read_text())
worlds=[]
seen=set()
for path in sorted((runtime/(label+'-pytest')).glob('test_installed*/world/client-session-python.json')):
    if path.resolve() in seen:
        continue
    seen.add(path.resolve())
    python=json.loads(path.read_text())
    world=path.parent
    node=json.loads((world/'client-session-node.json').read_text())
    scenario=re.search(r'\[([^\]]+)\]$',python['nodeid']).group(1)
    target=evidence/(label+'-worlds')/scenario
    target.mkdir(parents=True,exist_ok=True)
    for name in ['client-session-python.json','client-session-node.json','client-session.stdout.log','client-session.stderr.log','module.log','deployment/camctl.log']:
        src=world/name
        if src.exists():
            shutil.copy2(src,target/name.replace('/','-'))
    resources=[json.loads(p.read_text()) for p in sorted((world/'memory-budget').glob('*.json'))]
    (target/'resource-receipts.json').write_text(json.dumps(resources,indent=2)+'\n')
    operations=defaultdict(lambda:{'count':0,'elapsed_seconds':0.0})
    for command in python['commands']:
        key=command['operation']
        operations[key]['count']+=1
        operations[key]['elapsed_seconds']+=command['elapsed_seconds']
    worlds.append({'nodeid':python['nodeid'],'scenario':scenario,'counts':node['counts'], 'status':node['status'],'phase_timeout_seconds':python['phase_timeout_seconds'],'startup_seconds':python['startup']['elapsed_seconds'],'operation_count':len(python['commands']),'operation_seconds':sum(c['elapsed_seconds'] for c in python['commands']),'operations':dict(operations),'all_operations_passed':all(c['outcome']=='passed' for c in python['commands']),'close':python['close'],'cli_group_count':len(resources),'groups_removed':all(r['group_removed'] for r in resources),'all_combined_limits_2GiB':all(r['evidence']['combined_limit_bytes']==2147483648 for r in resources),'max_combined_peak_bytes':max((r['evidence']['combined_peak_bytes'] for r in resources),default=0),'oom_or_limit_events':[{k:v for k,v in r['evidence']['events'].items() if v} for r in resources]})
summary={'source_head':result['git_head'],'test_sha256':result['test_sha256'],'exitcode':result['exitcode'],'wall_seconds':result['wall_seconds'],'finished_at_utc':datetime.fromtimestamp(launcher['finished_at'],timezone.utc).isoformat(),'pytest_phases':stages['pytest_phases'],'worlds':worlds,'world_count':len(worlds),'launcher_errors':launcher['errors'],'delegation_removed':launcher['removal']['ok'],'remaining_pids':sum((g['remaining_pids'] for g in launcher.get('groups',[])),[]),'concurrent_load':result['concurrent_load']}
(evidence/(label+'-summary.json')).write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k not in ['worlds','pytest_phases']},ensure_ascii=False))
for world in worlds:
    print(json.dumps({k:v for k,v in world.items() if k not in ['nodeid','oom_or_limit_events']},ensure_ascii=False))
