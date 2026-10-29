import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ.get('OSMO_RUN_ROOT', '/tmp/camctl-quality-osmo-20261026'))
evidence = Path('/tmp/camctl-quality-evidence/osmo')
label = sys.argv[1]
python = '/workspaces/camctl/apps/camctl/.venv311/bin/python'
node = os.environ.get('OSMO_RUN_TARGET', 'tests/integration/test_osmo_builtin_record_segmentation_roundtrip.py::test_installed_segment_source_default_explicit_delivery_and_ack[normal-indexes0-2-succeeded]')
runtime = Path(os.environ.get('OSMO_RUNTIME_ROOT', '/tmp/camctl-quality-osmo-runtime'))
(runtime / 'tmp').mkdir(parents=True, exist_ok=True)
(runtime / 'pycache').mkdir(parents=True, exist_ok=True)
command = [python, 'scripts/run-memory-budget.py', '--evidence-dir', str(evidence / (label + '-budget')), '--', python, '-m', 'pytest', '-p', 'camctl_memory_plugin', '-p', 'osmo_timing', node, '-q', '--junitxml', str(evidence / (label + '.xml')), '--basetemp', str(runtime / (label + '-pytest')), '--durations=10']
env = os.environ.copy()
env.update(PYTHONPATH=':'.join([str(root / 'apps/camctl/src'), str(root / 'apps/camctl/tests'), str(root / 'scripts'), str(root / 'tests/integration'), str(evidence)]), TMPDIR=str(runtime / 'tmp'), PYTHONPYCACHEPREFIX=str(runtime / 'pycache'), UV_PYTHON=python, OSMO_TIMING_FILE=str(evidence / (label + '-stages.json')))
metadata = {'command': command, 'cwd': str(root), 'git_head': subprocess.check_output(['git','rev-parse','HEAD'], cwd=root, text=True).strip(), 'git_status': subprocess.check_output(['git','status','--short'], cwd=root, text=True), 'test_sha256': hashlib.sha256((root / node.split('::')[0]).read_bytes()).hexdigest(), 'python': subprocess.check_output([python,'--version'], text=True).strip(), 'concurrent_load': 'Other quality agents may run isolated workspaces concurrently; single diagnostic sample only.', 'environment': {k: env[k] for k in ('PYTHONPATH','TMPDIR','PYTHONPYCACHEPREFIX','UV_PYTHON','OSMO_TIMING_FILE')}}
(evidence / (label + '-command.json')).write_text(json.dumps(metadata, indent=2) + '\n')
started = time.monotonic()
with (evidence / (label + '.log')).open('w') as log:
    result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
metadata.update(exitcode=result.returncode, wall_seconds=time.monotonic()-started)
(evidence / (label + '-result.json')).write_text(json.dumps(metadata, indent=2) + '\n')
print(json.dumps({'label': label, 'exitcode': result.returncode, 'wall_seconds': metadata['wall_seconds']}), flush=True)
raise SystemExit(result.returncode)
