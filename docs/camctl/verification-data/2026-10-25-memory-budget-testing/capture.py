from pathlib import Path
import datetime, hashlib, json, os, subprocess, sys, time
root=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
out=Path('/tmp/camctl-memory-final-validation-20261025')
python='/workspaces/camctl/apps/camctl/.venv311/bin/python'
env=dict(os.environ)
env.update(PYTHONPATH=str(root/'apps/camctl/src')+os.pathsep+str(root/'apps/camctl/tests')+os.pathsep+str(root/'scripts'),PYTHONPYCACHEPREFIX=str(out/'pycache'),TMPDIR=str(out/'tmp'))
(out/'tmp').mkdir(exist_ok=True)
name=sys.argv[1]
command=[python,*sys.argv[2:]]
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
begin=time.monotonic()
with (out/(name+'.log')).open('w') as stream:
    child=subprocess.Popen(command,cwd=root,env=env,stdout=stream,stderr=subprocess.STDOUT)
    code=child.wait()
data=dict(command=command,cwd=str(root),environment={key:env[key] for key in ('PYTHONPATH','PYTHONPYCACHEPREFIX','TMPDIR')},started_utc=started,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),pid=child.pid,exit_code=code,wall_seconds=time.monotonic()-begin,source_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip())
(out/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(name=name,exit_code=code,wall_seconds=data['wall_seconds'],log=str(out/(name+'.log')))),flush=True)
sys.exit(code)
