#!/usr/bin/env python3
"""仅作为进程协作者，输出和退出由测试控制；不实现业务。"""
import fcntl
import json
import os
import pathlib
import sys
import time

args = sys.argv[1:]
root = pathlib.Path(args[args.index('--config') + 1])
command = args[0]
plan = args[1] if len(args) > 1 and args[1] != '--config' else None
with (root / 'trace').open('a') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    stream.write(json.dumps({'command': command, 'plan': plan, 'pid': os.getpid()}) + '\n')
    stream.flush()
if command == 'run':
    while (root / 'hold-run').exists():
        time.sleep(.005)
    mode = (root / 'run-mode').read_text() if (root / 'run-mode').exists() else 'ok'
else:
    while (root / 'hold-submit').exists():
        time.sleep(.005)
    mode = pathlib.Path(plan).read_text()
if mode == '127':
    sys.exit(127)
if mode == 'overflow':
    for _ in range(256):
        os.write(1, b'x' * 4096)
    sys.exit(0)
if mode == 'noisy':
    for _ in range(256):
        os.write(2, b'diagnostic ' * 300)
if mode == 'error':
    print(json.dumps({'kind': 'error', 'body': {'reason': 'future_error', 'details': {}}}))
    sys.exit(1)
if mode == 'empty':
    sys.exit(0)
message = {'kind': 'succeeded'}
if command == 'submit':
    message['body'] = {'needs_run': mode == 'true'}
print(json.dumps(message))
