"""在协调者指定的固定组合提交上执行原 OSMO 八场景。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path('/tmp/camctl-quality-repair-lidlmje7/osmo-recovery')
evidence = Path('/tmp/camctl-quality-repair-evidence/final-osmo')
runtime = Path('/tmp/camctl-quality-final-osmo-runtime-lidlmje7')
python = '/workspaces/camctl/apps/camctl/.venv311/bin/python'
label = sys.argv[1]
node = 'tests/integration/test_osmo_builtin_record_segmentation_roundtrip.py'
expected_commit = os.environ['OSMO_RUN_COMMIT']
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
status = subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True)
assert head == expected_commit, (head, expected_commit)
assert status == '', status
prepared = json.loads((evidence / 'preparation.json').read_text())
for relative, expected in prepared['lock_sha256'].items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, relative
assert (root / 'apps/client/node_modules/tsx/package.json').is_file()
progress = evidence / (label + '-progress')
progress.mkdir()
command = [python, 'scripts/run-memory-budget.py', '--camctl-memory-limit', '2GiB',
           '--evidence-dir', str(evidence / (label + '-budget')), '--', python,
           '-m', 'pytest', '-p', 'camctl_memory_plugin', '-p', 'osmo_timing',
           '-p', 'integration_progress', node, '-q', '--junitxml',
           str(evidence / (label + '.xml')), '--basetemp',
           str(runtime / (label + '-pytest')), '--durations=10', '-x']
environment = os.environ.copy()
for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONPYCACHEPREFIX', 'PYTHONDONTWRITEBYTECODE',
            'CAMCTL_TIMING_REPO', 'CAMCTL_TIMING_DATA', 'CAMCTL_INTEGRATION_PROGRESS'):
    environment.pop(key, None)
environment.update(
    PYTHONPATH=os.pathsep.join([str(root / 'apps/camctl/src'), str(root / 'apps/camctl/tests'),
                              str(root / 'scripts'), str(root / 'tests/integration'), str(evidence)]),
    TMPDIR=str(runtime / 'tmp'), PYTHONPYCACHEPREFIX=str(runtime / 'pycache'),
    UV_PYTHON=python, UV_CACHE_DIR=str(runtime / 'uv-cache'),
    UV_PROJECT_ENVIRONMENT=str(runtime / 'uv-project'),
    NPM_CONFIG_CACHE=str(runtime / 'npm-cache'),
    OSMO_TIMING_FILE=str(evidence / (label + '-stages.json')),
    CAMCTL_INTEGRATION_PROGRESS=str(progress),
)
metadata = {
    'command': command, 'cwd': str(root), 'git_head': head, 'git_status': status,
    'test_sha256': hashlib.sha256((root / node).read_bytes()).hexdigest(),
    'python': subprocess.check_output([python, '--version'], text=True).strip(),
    'scope': 'original eight scenarios, normal first, stop on first failure',
    'concurrent_load': 'Other agents may run independent workspaces; no performance comparison claimed.',
    'environment': {key: environment[key] for key in ('PYTHONPATH', 'TMPDIR', 'PYTHONPYCACHEPREFIX',
        'UV_PYTHON', 'UV_CACHE_DIR', 'UV_PROJECT_ENVIRONMENT', 'NPM_CONFIG_CACHE',
        'OSMO_TIMING_FILE', 'CAMCTL_INTEGRATION_PROGRESS')},
    'started_at': time.time(),
}
metadata_path = evidence / (label + '-command.json')
metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
started = time.monotonic()
with (evidence / (label + '.log')).open('w') as output:
    process = subprocess.Popen(command, cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT)
    metadata['launcher_pid'] = process.pid
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    exitcode = process.wait()
metadata.update(exitcode=exitcode, wall_seconds=time.monotonic() - started, finished_at=time.time())
(evidence / (label + '-result.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'label': label, 'exitcode': exitcode, 'wall_seconds': metadata['wall_seconds']}), flush=True)
raise SystemExit(exitcode)
