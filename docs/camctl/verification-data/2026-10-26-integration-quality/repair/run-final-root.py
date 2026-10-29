from pathlib import Path
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = Path('/opt/devcontainer/.codex/worktrees/de38/camctl')
OUT = Path('/tmp/camctl-quality-repair-evidence') / os.environ.get('CAMCTL_REPAIR_ROOT_EVIDENCE', 'final-root')
PYTHON = '/workspaces/camctl/apps/camctl/.venv311/bin/python'
SCOPES = {
    'unit': ['apps/camctl/tests/unit'],
    'capture': [
        'apps/camctl/tests/integration/capture/test_baseline_prepare.py',
        'apps/camctl/tests/integration/capture/test_baseline_read_owner.py',
        'apps/camctl/tests/integration/capture/test_result_page_history.py::test_page_and_first_discovery_fault_reuses_original_key_after_reopen',
    ],
    'persistence': ['apps/camctl/tests/integration/persistence/test_executor.py'],
    'bootstrap': [
        'apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py',
        'apps/camctl/tests/integration/bootstrap/test_inspection_read_dispatch.py',
        'apps/camctl/tests/integration/bootstrap/test_output_binding_changes.py',
    ],
    'outputs': ['apps/camctl/tests/integration/outputs/test_read_discovery.py'],
    'bootstrap-established': [
        'apps/camctl/tests/integration/bootstrap/test_inspection_read_dispatch.py',
        'apps/camctl/tests/integration/bootstrap/test_output_binding_changes.py',
    ],
}

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()

def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

expected = sys.argv[1]
if git('rev-parse', 'HEAD') != expected or git('status', '--porcelain'):
    raise SystemExit('Final source must match the supplied clean commit')
OUT.mkdir(parents=True, exist_ok=True)
source = {'head': expected, 'cwd': str(ROOT), 'at_utc': utc(),
          'python': subprocess.check_output([PYTHON, '--version'], text=True).strip(),
          'sqlite': subprocess.check_output([PYTHON, '-c', 'import sqlite3;print(sqlite3.sqlite_version)'], text=True).strip(),
          'tracked_diff': git('diff', '--stat')}
(OUT/'source.json').write_text(json.dumps(source, indent=2)+'\n')
env = os.environ.copy()
env['PYTHONPATH'] = 'apps/camctl/src'
for name in tuple(env):
    if name.startswith('CAMCTL_TIMING_'):
        del env[name]
for scope in sys.argv[2:] or SCOPES:
    dest = OUT/scope
    dest.mkdir(parents=True, exist_ok=True)
    command = [PYTHON, '-m', 'pytest', *SCOPES[scope], '-q', '-x',
               '--durations=15', '--junitxml='+str(dest/'result.xml')]
    receipt = dict(command=command, cwd=str(ROOT), head=expected,
                   started_at_utc=utc(), environment_override={'PYTHONPATH':env['PYTHONPATH']})
    started = time.monotonic()
    with (dest/'pytest.log').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        receipt['pid'] = process.pid
        (dest/'running.json').write_text(json.dumps(receipt, indent=2)+'\n')
        code = process.wait()
    receipt.update(exit_code=code, finished_at_utc=utc(), wall_seconds=time.monotonic()-started,
                   head_after=git('rev-parse', 'HEAD'), tracked_status_after=git('status','--porcelain'))
    (dest/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(scope, code, receipt['wall_seconds'], flush=True)
    print((dest/'pytest.log').read_text()[-4500:], flush=True)
    if code:
        raise SystemExit(code)
