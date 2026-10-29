import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

if len(sys.argv) > 1:
    phase = sys.argv[2]
    _, store, host, port, identity, metrics = sys.argv[3:]
    if phase == 'startup':
        signal.pause()
    else:
        Path(metrics).write_text(json.dumps({'session_id': identity, 'node_pid': os.getpid(), 'browser_processes': []}))
        stream = socket.create_connection((host, int(port)))
        stream.sendall((json.dumps({'kind': 'ready', 'session_id': identity, 'pid': os.getpid()}) + '\n').encode())
        stream.recv(65536)
        signal.pause()
    raise SystemExit()

workspace = Path('/tmp/camctl-quality-client-session-20261026')
sys.path.insert(0, str(workspace / 'tests/integration'))
from _client_session import ClientSession

root = Path('/tmp/camctl-quality-evidence/client-session')
results = []
for phase in ('startup', 'call', 'close'):
    world = root / f'timeout-{phase}'
    started = time.monotonic()
    try:
        session = ClientSession([sys.executable, __file__, 'worker', phase], cwd=workspace, env=os.environ.copy(), root=world, timeout=0.5)
        if phase == 'call':
            session.call('export', output=world / 'unused.json')
        else:
            session.close()
    except TimeoutError as error:
        assert '0.5秒预算' in str(error), error
        metrics = json.loads((world / 'client-session-python.json').read_text())
        assert metrics['phase_timeout_seconds'] == 0.5
        assert metrics['cleanup']['survivors'] == []
        results.append({'phase': phase, 'passed': True, 'elapsed_seconds': time.monotonic() - started})
    else:
        raise AssertionError(f'{phase}未按实例预算超时')
(root / 'timeout-result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(results, ensure_ascii=False))
