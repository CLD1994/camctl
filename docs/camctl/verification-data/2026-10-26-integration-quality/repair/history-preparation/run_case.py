from __future__ import annotations
import datetime, json, os, pathlib, subprocess, sys, time

label = sys.argv[1]
folder = pathlib.Path('/tmp/camctl-quality-repair-evidence/history-preparation')
node = 'apps/camctl/tests/integration/history/test_report_selection.py::test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window'
command = [sys.executable, '-m', 'pytest', node, '-q', '-s', '--durations=1', '-p', 'history_preparation_timing', f'--junitxml={folder / (label + ".xml")}']
environment = os.environ.copy()
environment['PYTHONPATH'] = f'apps/camctl/src:apps/camctl/tests:{folder}'
environment['PYTHONDONTWRITEBYTECODE'] = '1'
environment['CAMCTL_HISTORY_PREPARATION_TIMING'] = str(folder / (label + '-phases.jsonl'))
started = time.perf_counter()
record = {'label': label, 'command': command, 'worktree': str(pathlib.Path.cwd()), 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
(folder / (label + '-receipt.json')).write_text(json.dumps(record, indent=2) + '\n')
try:
    with (folder / (label + '.log')).open('w') as stream:
        result = subprocess.run(command, env=environment, stdout=stream, stderr=subprocess.STDOUT, timeout=420)
    record['exit_code'] = result.returncode
except subprocess.TimeoutExpired:
    record['exit_code'] = 124
    record['error'] = 'single-node deadline exceeded'
record['wall_seconds'] = time.perf_counter() - started
record['ended_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(folder / (label + '-receipt.json')).write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2))
sys.exit(record['exit_code'])
