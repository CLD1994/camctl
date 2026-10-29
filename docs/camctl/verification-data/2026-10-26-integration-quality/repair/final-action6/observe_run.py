from pathlib import Path
import hashlib
import json
import os
import signal
import sys
import time

root = Path(sys.argv[1]).resolve()
archive = root / 'observed-logs'
archive.mkdir(exist_ok=True)
seen = {}
stop = False


def stop_observer(signum, frame):
    global stop
    stop = True


signal.signal(signal.SIGTERM, stop_observer)
signal.signal(signal.SIGINT, stop_observer)
(root / 'observer-pid.txt').write_text(str(os.getpid()) + '\n')
with (root / 'log-archive-index.jsonl').open('a') as index, (root / 'process-snapshots.jsonl').open('a') as snapshots:
    while True:
        now = time.time_ns()
        for log in (root / 'data').rglob('camctl.log*'):
            try:
                stat = log.stat()
                identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if seen.get(str(log)) == identity or not log.is_file():
                    continue
                data = log.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                target = archive / (digest + ('.gz' if log.suffix == '.gz' else '.log'))
                if not target.exists():
                    target.write_bytes(data)
                index.write(json.dumps({'observed_time_ns': now, 'source': str(log), 'sha256': digest,
                    'size': len(data), 'archived': str(target)}) + '\n')
                index.flush()
                seen[str(log)] = identity
            except FileNotFoundError:
                pass
        processes = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                command = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                if str(root) not in command or int(proc.name) == os.getpid():
                    continue
                stat = (proc / 'stat').read_text()
                tail = stat[stat.rfind(')') + 2:].split()
                processes.append({'pid': int(proc.name), 'state': tail[0], 'ppid': int(tail[1]),
                    'pgid': int(tail[2]), 'starttime': tail[19], 'command': command,
                    'cgroup': (proc / 'cgroup').read_text()})
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
        snapshots.write(json.dumps({'observed_time_ns': now, 'processes': processes}) + '\n')
        snapshots.flush()
        if stop or (root / 'observer.stop').exists():
            break
        time.sleep(1)
(root / 'observer-exit.json').write_text(json.dumps({'pid': os.getpid(), 'observed_time_ns': time.time_ns(), 'status': 'stopped'}) + '\n')
