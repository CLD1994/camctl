"""固定 f143fbbe，只复验一个原 bootstrap 目标。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time

root = Path('/tmp/camctl-quality-history-20261026')
evidence = Path('/tmp/camctl-quality-evidence/history')
prefix = 'latest83-bootstrap'
expected = 'f143fbbecf68e9333ef6c4db36188b2c6c642630'
source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
assert source == expected, source
assert not subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True).strip()
assert not (evidence / f'{prefix}.log').exists()
env = os.environ.copy()
env.update(PYTHONPATH=':'.join((str(evidence/'diagnostics'), str(root/'apps/camctl/src'), str(root/'apps/camctl/tests'), str(root/'scripts'))), TMPDIR=str(evidence/f'{prefix}-tmp'), PYTHONPYCACHEPREFIX=str(evidence/f'{prefix}-pycache'), PYTHONUNBUFFERED='1', CAMCTL_FIXED_PROGRESS=str(evidence/f'{prefix}-progress.jsonl'))
for key in ('TMPDIR', 'PYTHONPYCACHEPREFIX'):
    Path(env[key]).mkdir(exist_ok=True)
node = 'apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py::test_submit_during_initial_discovery_is_not_lost'
command = [sys.executable, '-m', 'pytest', '-p', 'fixed_progress', node, '-vv', '-s', '--durations=10', f'--basetemp={evidence}/{prefix}-data', f'--junitxml={evidence}/{prefix}.xml']
receipt = {'source': source, 'workdir': str(root), 'argv': command, 'env': {key: env[key] for key in ('PYTHONPATH', 'TMPDIR', 'PYTHONPYCACHEPREFIX', 'PYTHONUNBUFFERED', 'CAMCTL_FIXED_PROGRESS')}, 'python': sys.version, 'executable': sys.executable, 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(), 'started_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'one original bootstrap node; no history runs or phase timing; pytest event hooks only', 'whole_program_memory_budget': False, 'test_sha256': hashlib.sha256((root/node.split('::')[0]).read_bytes()).hexdigest()}
(evidence/f'{prefix}-command.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
started = time.perf_counter()
with (evidence/f'{prefix}.log').open('w') as log:
    completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
receipt.update(exit_code=completed.returncode, wall_seconds=time.perf_counter()-started, finished_utc=datetime.now(timezone.utc).isoformat(), final_git_status=subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True))
(evidence/f'{prefix}-result.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
(evidence/f'{prefix}.exit').write_text(str(completed.returncode)+'\n')
print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
sys.exit(completed.returncode)
