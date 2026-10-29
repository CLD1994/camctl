from pathlib import Path
import datetime,hashlib,json,os,platform,shlex,sqlite3,subprocess,sys,time
ROOT=Path('/tmp/camctl-same-check-options-ykdqfnct')
RUN=ROOT/'validation'
SOURCE=ROOT/'source'
PYTHON='/workspaces/camctl/apps/camctl/.venv311/bin/python'
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def hashes():
    result={}
    for base in (SOURCE/'apps/camctl/src',SOURCE/'apps/camctl/tests'):
        for file in sorted(base.rglob('*')):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix=='.py':
                result[str(file.relative_to(SOURCE))]=hashlib.sha256(file.read_bytes()).hexdigest()
    for file in (SOURCE/'apps/camctl/pyproject.toml',SOURCE/'apps/camctl/uv.lock'):
        result[str(file.relative_to(SOURCE))]=hashlib.sha256(file.read_bytes()).hexdigest()
    return result
before=hashes()
(RUN/'source-hashes-before.json').write_text(json.dumps(before,indent=2)+'\n')
targets=json.loads((RUN/'targets.json').read_text())
nodes=[f'apps/camctl/tests/integration/persistence/{file}::{name}' for file,names in targets.items() for name in names]
cmd=[PYTHON,'-m','pytest','-p','prototype_validation_plugin','-p','no:cacheprovider','-vv','--tb=short','--durations=20','--junitxml='+str(RUN/'junit.xml'),*nodes]
env=dict(os.environ)
env.update(PYTHONPATH=str(SOURCE/'apps/camctl/src')+':'+str(SOURCE/'apps/camctl/tests')+':'+str(RUN),TMPDIR=str(RUN/'tmp'),PYTHONPYCACHEPREFIX=str(RUN/'pycache'),PYTHONUNBUFFERED='1')
for key in ('PYTEST_ADDOPTS','PYTHONHOME'):env.pop(key,None)
record={'scope':'外部函数替换可行性试验；非正式实现或全量回归','source_head':json.loads((ROOT/'metadata.json').read_text())['source_head'],'source':str(SOURCE),'command':cmd,'command_shell':shlex.join(cmd),'cwd':str(SOURCE),'environment':{key:env[key] for key in ('PYTHONPATH','TMPDIR','PYTHONPYCACHEPREFIX')},'started_utc':utc(),'driver_pid':os.getpid(),'python':platform.python_version(),'executable':sys.executable,'sqlite':sqlite3.sqlite_version,'timeout_s':300}
(RUN/'command.txt').write_text(record['command_shell']+'\n')
started=time.monotonic()
with (RUN/'pytest.log').open('w') as log:
    proc=subprocess.Popen(cmd,cwd=SOURCE,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    record['pid']=proc.pid
    (RUN/'execution.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    try:record['exit_code']=proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        import signal
        record['timed_out']=True
        os.killpg(proc.pid,signal.SIGTERM)
        try:record['exit_code']=proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL)
            record['exit_code']=proc.wait()
record['wall_s']=time.monotonic()-started
record['finished_utc']=utc()
after=hashes()
(RUN/'source-hashes-after.json').write_text(json.dumps(after,indent=2)+'\n')
record['source_file_changes']=[name for name in sorted(set(before)|set(after)) if before.get(name)!=after.get(name)]
progress=RUN/'pytest-progress.json'
if progress.exists():
    state=json.loads(progress.read_text())
    record['collected']=state.get('collected')
    reports=state.get('reports',[])
    record['case_outcomes']={key:sum(item['when']=='call' and item['outcome']==key for item in reports) for key in ('passed','failed','skipped')}
(RUN/'execution.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(record,ensure_ascii=False,indent=2))
sys.exit(record['exit_code'] if record['exit_code']>=0 else 1)
