#!/usr/bin/env python3.11
"""持有更新锁，直到一个离线更新写入命令退出。"""
import sys

sys.dont_write_bytecode = True

import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock-fd', type=int, action='append', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command or any(fd < 0 for fd in args.lock_fd):
        parser.error('必须提供有效锁描述符及命令')
    return subprocess.run(command, pass_fds=tuple(args.lock_fd), check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
