import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path('/tmp/camctl-quality-outputs-20261026')
evidence = Path('/tmp/camctl-quality-evidence/outputs')
label = sys.argv[1]
nodes = sys.argv[2:] or ['apps/camctl/tests/integration/outputs/test_read_discovery.py::test_candidate_capacity_tracks_batch_not_total']
env = os.environ.copy()
env.update(PYTHONPATH=f'{root}/apps/camctl/src:{root}/apps/camctl/tests:{root}/scripts', TMPDIR='/tmp/camctl-quality-outputs-temp', PYTHONPYCACHEPREFIX='/tmp/camctl-quality-outputs-pycache')
entrypoint = [str(evidence / 'retain_page.py')] if label == 'retained-page' else ['-m', 'pytest']
prefix = [sys.executable] if label == 'retained-page' else [sys.executable, '-m', 'cProfile', '-o', str(evidence / f'{label}.prof')]
command = [*prefix, *entrypoint, *nodes, '-q', '-s', '--durations=0', f'--junitxml={evidence}/{label}.xml', f'--basetemp=/tmp/camctl-quality-outputs-temp/{label}', '-o', f'cache_dir=/tmp/camctl-quality-outputs-temp/pytest-cache-{label}']
metadata = {'command': command, 'cwd': str(root), 'environment': {key: env[key] for key in ('PYTHONPATH', 'TMPDIR', 'PYTHONPYCACHEPREFIX')}, 'python': sys.version, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(), 'source_status': subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True), 'source_diff': subprocess.check_output(['git', 'diff'], cwd=root, text=True), 'concurrent_load': 'Other agents may run isolated tests concurrently; one timing sample does not establish stable speedup.'}
start = time.perf_counter()
with (evidence / f'{label}.log').open('w') as log:
    outcome = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
metadata.update(exit_code=outcome.returncode, wall_seconds=time.perf_counter() - start)
(evidence / f'{label}.json').write_text(json.dumps(metadata, indent=2) + '\n')
print(json.dumps({'label': label, 'exit_code': outcome.returncode, 'wall_seconds': metadata['wall_seconds']}))
raise SystemExit(outcome.returncode)
