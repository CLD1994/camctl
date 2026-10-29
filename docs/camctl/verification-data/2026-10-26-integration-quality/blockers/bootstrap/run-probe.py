from datetime import datetime,timezone
import json,os,platform,sqlite3,subprocess,sys,time
from pathlib import Path
work=Path('/tmp/camctl-failure-repair-20261026-4f5dfpqh/bootstrap'); out=Path('/tmp/camctl-failure-repair-evidence/bootstrap');prefix='before-fix'
env=os.environ.copy();env.update(PYTHONPATH=':'.join(map(str,(out/'diagnostics',work/'apps/camctl/src',work/'apps/camctl/tests',work/'scripts'))),TMPDIR=str(out/(prefix+'-tmp')),PYTHONPYCACHEPREFIX=str(out/(prefix+'-pycache')),PYTHONUNBUFFERED='1',CAMCTL_EXIT_PROBE=str(out/(prefix+'-probe.jsonl')))
for key in ('TMPDIR','PYTHONPYCACHEPREFIX'):Path(env[key]).mkdir(exist_ok=True)
command=[sys.executable,'-m','pytest','-p','exit_probe','apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py::test_submit_during_initial_discovery_is_not_lost','-vv','-s','--durations=10','--basetemp='+str(out/(prefix+'-data')),'--junitxml='+str(out/(prefix+'.xml'))]
receipt={'source':subprocess.check_output(['git','rev-parse','HEAD'],cwd=work,text=True).strip(),'status':subprocess.check_output(['git','status','--short'],cwd=work,text=True),'workdir':str(work),'argv':command,'env':{k:env[k] for k in ('PYTHONPATH','TMPDIR','PYTHONPYCACHEPREFIX','PYTHONUNBUFFERED','CAMCTL_EXIT_PROBE')},'python':sys.version,'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),'started_utc':datetime.now(timezone.utc).isoformat(),'diagnosis':'test-only observers; samples at 2/10/20/28 seconds before original 30s cancellation; no budget or synchronization changes'}
(out/(prefix+'-command.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');started=time.perf_counter()
with (out/(prefix+'.log')).open('w') as stream:result=subprocess.run(command,cwd=work,env=env,stdout=stream,stderr=subprocess.STDOUT)
receipt.update(exit_code=result.returncode,wall_seconds=time.perf_counter()-started,finished_utc=datetime.now(timezone.utc).isoformat());(out/(prefix+'-result.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');(out/(prefix+'.exit')).write_text(str(result.returncode)+'\n');print(json.dumps(receipt,ensure_ascii=False,indent=2));sys.exit(result.returncode)
