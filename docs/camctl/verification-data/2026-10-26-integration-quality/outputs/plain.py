import json
import os
from pathlib import Path
import subprocess
import sys
import time

label = sys.argv[1]
root = Path('/tmp/camctl-quality-outputs-before-plain-20261026' if label == 'plain-before' else '/tmp/camctl-quality-outputs-20261026')
evidence = Path('/tmp/camctl-quality-evidence/outputs')
temporary = Path(f'/tmp/camctl-quality-outputs-temp/{label}-root')
temporary.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
env.update(PYTHONPATH=f'{root}/apps/camctl/src:{root}/apps/camctl/tests:{root}/scripts', TMPDIR=str(temporary), PYTHONPYCACHEPREFIX=f'/tmp/camctl-quality-outputs-pycache/{label}')
command = [sys.executable, '-m', 'pytest', 'apps/camctl/tests/integration/outputs/test_read_discovery.py::test_candidate_capacity_tracks_batch_not_total', '-q', '-s', '--durations=0', f'--junitxml={evidence}/{label}.xml', f'--basetemp={temporary}/pytest', '-o', f'cache_dir={temporary}/pytest-cache']
metadata = {'command': command, 'cwd': str(root), 'environment': {key: env[key] for key in ('PYTHONPATH', 'TMPDIR', 'PYTHONPYCACHEPREFIX')}, 'python': sys.version, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(), 'source_status': subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True), 'source_diff': subprocess.check_output(['git', 'diff'], cwd=root, text=True), 'timing': 'One plain pytest run with no cProfile; wall includes interpreter startup and private bytecode preparation. Other isolated agents may run concurrently.'}
start = time.perf_counter()
with (evidence / f'{label}.log').open('w') as log:
    outcome = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
metadata.update(exit_code=outcome.returncode, wall_seconds=time.perf_counter() - start)
(evidence / f'{label}.json').write_text(json.dumps(metadata, indent=2) + '\n')
print(json.dumps({'label': label, 'exit_code': outcome.returncode, 'wall_seconds': metadata['wall_seconds']}))
raise SystemExit(outcome.returncode)
