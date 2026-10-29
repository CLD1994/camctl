import datetime,hashlib,json,os,subprocess,time,xml.etree.ElementTree as ET
from pathlib import Path
root=Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
out=Path('/tmp/camctl-full-272f-final-combination');out.mkdir(exist_ok=False)
paths=set()
for tc in ET.parse('/tmp/camctl-merge-272f-combination.xml').iter('testcase'):
 parts=tc.attrib['classname'].split('.')
 for n in range(len(parts),0,-1):
  p=root/'apps/camctl'/Path(*parts[:n]).with_suffix('.py')
  if p.is_file():paths.add(str(p.relative_to(root)));break
 else:raise RuntimeError(tc.attrib)
paths.update(['apps/camctl/tests/unit/capture/test_internal_read_save_owner.py','apps/camctl/tests/unit/capture/test_media_stopped_read_resume.py','apps/camctl/tests/integration/bootstrap/test_inspection_read_dispatch.py'])
python='/workspaces/camctl/apps/camctl/.venv311/bin/python'
env=os.environ.copy();env['PYTHONPATH']=str(root/'apps/camctl/src')+':'+str(root/'apps/camctl/tests')
cmd=[python,'-m','pytest',*sorted(paths),'-q','--junitxml='+str(out/'results.xml'),'--basetemp='+str(out/'pytest-tmp')]
receipt={'command':cmd,'cwd':str(root),'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'git_status':subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True),'PYTHONPATH':env['PYTHONPATH'],'source_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'apps/camctl/src').rglob('*.py'))},'selection':sorted(paths),'selection_origin':'original 352-case combination XML module union plus direct merge-fix modules; current source, original assertions retained','started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
receipt['module_file']=subprocess.check_output([python,'-c','import camctl.capture.media_flow as m;print(m.__file__)'],cwd=root,env=env,text=True).strip()
assert receipt['module_file']==str(root/'apps/camctl/src/camctl/capture/media_flow.py')
start=time.monotonic()
with (out/'output.log').open('w') as log:
 p=subprocess.Popen(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
 receipt['pid']=p.pid;(out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');code=p.wait()
receipt.update(exit_code=code,wall_seconds=time.monotonic()-start,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),process_reaped=True)
receipt['source_unchanged']=all((root/k).is_file() and hashlib.sha256((root/k).read_bytes()).hexdigest()==v for k,v in receipt['source_hashes'].items())
(out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print((out/'output.log').read_text());print(json.dumps({k:v for k,v in receipt.items() if k!='source_hashes'},ensure_ascii=False,indent=2))
raise SystemExit(code)
