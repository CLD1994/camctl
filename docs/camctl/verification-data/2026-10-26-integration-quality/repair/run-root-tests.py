from pathlib import Path
import datetime,json,os,subprocess,sys,time
p=Path('/tmp/camctl-quality-repair-evidence/executor')/sys.argv[1]
p.mkdir(parents=True,exist_ok=True)
cmd=['/workspaces/camctl/apps/camctl/.venv311/bin/python','-m','pytest',*sys.argv[2:],'-q','--junitxml='+str(p/'result.xml')]
env=os.environ.copy();env['PYTHONPATH']='apps/camctl/src'
r={'command':cmd,'cwd':os.getcwd(),'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'environment_override':{'PYTHONPATH':env['PYTHONPATH']},'python':subprocess.check_output([cmd[0],'--version'],text=True).strip()}
t=time.monotonic()
with (p/'pytest.log').open('w') as f:c=subprocess.run(cmd,env=env,stdout=f,stderr=subprocess.STDOUT)
r.update(exit_code=c.returncode,wall_seconds=time.monotonic()-t,finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat());(p/'receipt.json').write_text(json.dumps(r,indent=2)+'\n')
print((p/'pytest.log').read_text()[-3000:]);print('exit_code',c.returncode)
