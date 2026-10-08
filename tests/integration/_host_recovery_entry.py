"""O6 的本地进程协作者：运行真实 CLI，并留下由生产入口启动的工具。

这里只安排进程退出与组归属，不实现业务恢复或 C 主机收场逻辑。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path


def record(root: Path, role: str, **fields) -> None:
    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = stat[stat.rfind(")") + 2:].split()[19]
    with (root / "process-trace").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps({
            "role": role, "pid": os.getpid(), "pgid": os.getpgrp(),
            "start": start, **fields,
        }) + "\n")
        stream.flush()


def tool(root: Path) -> None:
    mode = os.environ.get("CAMCTL_TEST_SERVER_MODE", "none")
    if mode != "none":
        child = os.fork()
        if not child:
            if mode == "detached":
                os.setsid()
                # 独立服务端不继续持有受管工具的输出管道。
                with open(os.devnull, "wb") as sink:
                    os.dup2(sink.fileno(), 1)
            record(root, "server")
            (root / "server-ready").touch()
            if mode == "racing":
                while not (root / "detach-server").exists():
                    time.sleep(.001)
                os.setsid()
                with open(os.devnull, "wb") as sink:
                    os.dup2(sink.fileno(), 1)
                record(root, "server-detached")
            while True:
                signal.pause()
        while not (root / "server-ready").exists():
            time.sleep(.005)
    record(root, "tool")
    (root / "tool-ready").touch()
    while True:
        signal.pause()


async def leave_tool(root: Path, exit_code: int) -> None:
    from camctl.operations.process import ToolSpec, spawn_subprocess

    handle = await spawn_subprocess(ToolSpec(
        (sys.executable, str(Path(__file__).resolve()), "tool"),
        None, Decimal("1")))
    while not (root / "tool-ready").exists():
        await asyncio.sleep(.005)
    (root / "exit-ready").touch()
    while (root / "hold-first-exit").exists():
        await asyncio.sleep(.005)
    # 句柄保持在此协程中；模拟 CLI 退出时尚未完成工具的退出管理。
    assert handle is not None
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


def main() -> int:
    root = Path(os.environ["CAMCTL_TEST_GROUP_ROOT"])
    args = sys.argv[1:]
    if args[0] == "tool":
        tool(root)
        return 0
    command = args[0]
    record(root, "cli", command=command)
    if command == "submit":
        while (root / "hold-submit").exists():
            time.sleep(.005)
    from camctl.cli import main as cli_main

    code = cli_main(args)
    if command == "run":
        try:
            fd = os.open(root / "first-run", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if os.environ.get("CAMCTL_TEST_REPEAT_ERROR"):
                return int(os.environ["CAMCTL_TEST_EXIT_CODE"])
        else:
            os.close(fd)
            assert code == 0
            asyncio.run(leave_tool(root, int(os.environ["CAMCTL_TEST_EXIT_CODE"])))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
