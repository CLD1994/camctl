import datetime
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

root = pathlib.Path('/tmp/camctl-quality-repair-lidlmje7/background-exit')
out = pathlib.Path(__file__).parent
label = sys.argv[1]
env = os.environ.copy()
env['PYTHONPATH'] = str(root / 'apps/camctl/src') + os.pathsep + str(root / 'apps/camctl/tests')
for name in ('CAMCTL_TIMING_REPO', 'PYTEST_ADDOPTS'):
    env.pop(name, None)
args = [sys.executable, '-m', 'pytest', *sys.argv[2:]]
start = datetime.datetime.now(datetime.timezone.utc).isoformat()
clock = time.monotonic()
with (out / f'{label}.log').open('w') as log:
    process = subprocess.Popen(args, cwd=root, env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    pid = process.pid
    code = process.wait()
process_rows = subprocess.check_output(['ps', '-eo', 'pid,ppid,pgid,args'], text=True)
remaining = [row for row in process_rows.splitlines()[1:]
             if len(row.split(None, 3)) >= 3 and row.split(None, 3)[2] == str(pid)]
receipt = dict(command=args, cwd=str(root), python=sys.version,
               sqlite=sqlite3.sqlite_version, pythonpath=env['PYTHONPATH'],
               source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
               start_utc=start, end_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               elapsed_s=time.monotonic()-clock, pid=pid, exit_code=code,
               process_reaped=process.poll() is not None, remaining_process_group=remaining,
               log=str(out/f'{label}.log'))
(out / f'{label}.json').write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
print((out / f'{label}.log').read_text()[-18000:])
sys.exit(code)
