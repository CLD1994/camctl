import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = '/tmp/camctl-merge-272f-inspection-resume'
evidence = Path(__file__).parent
label, *selection = sys.argv[1:]
python = '/workspaces/camctl/apps/camctl/.venv311/bin/python'
command = [python, '-m', 'pytest', *selection, '-q',
           '--junitxml=' + str(evidence / (label + '.xml')),
           '--basetemp=' + str(evidence / (label + '-tmp'))]
start = time.monotonic()
environment = os.environ.copy()
environment['PYTHONPATH'] = os.pathsep.join([root + '/apps/camctl/src',
    root + '/apps/camctl/tests', environment.get('PYTHONPATH', '')])
module_file = subprocess.check_output([python, '-c',
    'import camctl.capture.media_flow as module; print(module.__file__)'],
    cwd=root, env=environment, text=True).strip()
assert module_file.startswith(root + '/')
process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, start_new_session=True,
                           env=environment)
timed_out = False
try:
    output, _ = process.communicate(timeout=60)
except subprocess.TimeoutExpired:
    timed_out = True
    os.killpg(process.pid, signal.SIGKILL)
    output, _ = process.communicate()
(evidence / (label + '.log')).write_text(output)
receipt = {'command': command, 'cwd': root, 'pid': process.pid,
           'module_file': module_file, 'PYTHONPATH': environment['PYTHONPATH'],
           'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
           'python': subprocess.check_output([python, '--version'], text=True).strip(),
           'exit_code': process.returncode, 'timed_out': timed_out,
           'elapsed_seconds': time.monotonic() - start, 'process_reaped': process.poll() is not None}
(evidence / (label + '-receipt.json')).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(receipt, ensure_ascii=False))
print(output)
raise SystemExit(process.returncode)
