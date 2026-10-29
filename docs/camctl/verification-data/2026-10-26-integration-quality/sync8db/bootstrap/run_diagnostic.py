import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path('/tmp/camctl-sync8db-tests-ojv22ku3/bootstrap')
evidence = Path('/tmp/camctl-sync8db-evidence/bootstrap')
label, target = sys.argv[1:3]
base = evidence / label
base.mkdir(exist_ok=True)
python = '/workspaces/camctl/apps/camctl/.venv311/bin/python'
command = [python, '-m', 'pytest', target, '-vv', '-ra', '--tb=short', '-p', 'bootstrap_progress', '-p', 'bootstrap_diagnostic', '-o', 'faulthandler_timeout=45', '--junitxml=' + str(base / 'junit.xml'), '--basetemp=' + str(base / 'pytest-tmp')]
env = os.environ.copy()
env.update(PYTHONPATH=f'{root}/apps/camctl/src:{root}/apps/camctl/tests:{evidence}', PYTHONDONTWRITEBYTECODE='1', BOOTSTRAP_PROGRESS_PATH=str(base / 'progress.jsonl'))
for name in tuple(env):
    if name.startswith('CAMCTL_TIMING_'):
        env.pop(name)
receipt = {'command': command, 'cwd': str(root), 'python': subprocess.check_output([python, '--version'], text=True).strip(), 'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(), 'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=root, text=True).strip(), 'status': subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True), 'start_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'source_files': {}}
for tree in ('apps/camctl/src', 'apps/camctl/tests/integration/bootstrap'):
    for path in sorted((root / tree).rglob('*.py')):
        receipt['source_files'][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
(base / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
start = time.monotonic()
with (base / 'pytest.log').open('wb') as log:
    process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    receipt['pid'] = process.pid
    (base / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    try:
        code = process.wait(timeout=150)
    except subprocess.TimeoutExpired:
        receipt['external_timeout_s'] = 150
        os.killpg(process.pid, signal.SIGTERM)
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
receipt.update(exit_code=code, elapsed_s=time.monotonic()-start, end_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
(base / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
print(json.dumps({key:value for key,value in receipt.items() if key != 'source_files'}, ensure_ascii=False))
