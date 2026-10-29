import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

root = Path.cwd()
sys.path[:0] = [str(root / 'tests/integration'), str(root / 'apps/camctl/src')]
from _osmo_builtin_clock import set_frozen
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools

world = Path('/tmp/camctl-quality-repair-evidence/osmo-recovery/gate-smoke')
world.mkdir()
path = '/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261022100000_0007_D.OSV'
content = b'A' * 65536 + b'B' * 65536 + b'C' * 1024
local = world / path.lstrip('/')
local.parent.mkdir(parents=True)
local.write_bytes(content)
manifest = world / 'files.json'
manifest.write_text(json.dumps([{'path': path, 'source': str(local)}]))
clock = world / 'clock.json'
set_frozen(clock, utc_us=1792972800000000, monotonic_ns=1000000000)
gate = world / 'read.gate'
log = world / 'calls.jsonl'
environment = dict(os.environ)
environment.update(
    PYTHONPATH=os.pathsep.join([str(root / 'tests/integration'), str(root / 'apps/camctl/src')]),
    CAMCTL_OSMO_TEST_FILES=str(manifest),
    CAMCTL_OSMO_TEST_CLOCK_FILE=str(clock),
    CAMCTL_OSMO_TEST_READ_GATE=str(gate),
    CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES='0',
    GATE_SMOKE_ROOT=str(world),
    GATE_SMOKE_LOG=str(log),
    GATE_SMOKE_SCRIPT=ShellFileTools().read_script(path, 65536),
)
command = [sys.executable, '-u', '-c',
    'import os,sys; from pathlib import Path; from _osmo_builtin_adb import _file_call; '
    'sys.exit(_file_call(os.environ["GATE_SMOKE_SCRIPT"], '
    'root=Path(os.environ["GATE_SMOKE_ROOT"]), calls_path=Path(os.environ["GATE_SMOKE_LOG"])))']
process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    deadline = time.monotonic() + 5
    blocked = None
    while time.monotonic() < deadline:
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        blocked = next((row for row in rows if row['kind'] == 'read_blocked'), None)
        if blocked is not None:
            break
        assert process.poll() is None, process.communicate(timeout=1)
        time.sleep(0.01)
    assert blocked is not None
    assert blocked['forwarded_bytes'] == 0
    assert blocked['script'] == environment['GATE_SMOKE_SCRIPT']
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert not selector.select(0.1), '读门未释放前已转发数据'
    assert process.poll() is None
    gate.touch()
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, error
    assert output == content[65536:]
    result = {'status': 'passed', 'scope': 'existing real read gate and ShellFileTools dd command',
              'forwarded_before_release': 0, 'offset': 65536,
              'forwarded_after_release': len(output), 'exit_code': process.returncode,
              'does_not_validate': 'installed CLI, persistence or full OSMO scenario'}
    (world.parent / 'gate-smoke-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
finally:
    if process.poll() is None:
        process.kill()
        process.communicate(timeout=5)
