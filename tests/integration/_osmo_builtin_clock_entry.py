"""安装版 camctl 入口的时钟边界包装器。

它仍调用 wheel 中的真实 CLI 和业务装配，只让已声明的时钟端口读取
共享偏移文件。asyncio.sleep、ADB transport、task factory 和相机合同均
保持安装包原实现。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time as _real_time
from types import SimpleNamespace


def _append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _controlled_time(offset_path: Path):
    def offset_ns() -> int:
        raw = offset_path.read_text(encoding="ascii").strip()
        value = int(raw)
        if value < 0:
            raise ValueError("OSMO 测试时钟偏移不能为负数")
        return value

    def time_ns() -> int:
        return _real_time.time_ns() + offset_ns()

    def monotonic_ns() -> int:
        return _real_time.monotonic_ns() + offset_ns()

    def wall_seconds() -> float:
        return time_ns() / 1_000_000_000

    return SimpleNamespace(
        time_ns=time_ns,
        monotonic_ns=monotonic_ns,
        time=wall_seconds,
    )


def _install_clock_ports(controlled_time) -> None:
    """替换安装进程各自捕获的时钟模块引用，不改全局标准库。"""
    from camctl.bootstrap import clocks, lifecycle
    from camctl.bootstrap import capture_assembly, cleanup_assembly, obtain_assembly
    from camctl.devices.drivers.adb_cameras import registration

    class ControlledSystemClock:
        def utc_micros(self) -> int:
            return controlled_time.time_ns() // 1_000

        def monotonic_ns(self) -> int:
            return controlled_time.monotonic_ns()

    clocks.time = controlled_time
    clocks.SystemClock = ControlledSystemClock
    lifecycle.SystemClock = ControlledSystemClock
    capture_assembly.time = controlled_time
    obtain_assembly.time = controlled_time
    cleanup_assembly.time = controlled_time
    registration.time = controlled_time


def main() -> int:
    started_ns = _real_time.monotonic_ns()
    log_path = Path(os.environ["CAMCTL_OSMO_TEST_PROCESS_LOG"])
    record = {
        "cwd": os.getcwd(),
        "python": sys.executable,
        "argv": [sys.argv[0], *sys.argv[1:]],
        "started_ns": started_ns,
    }
    try:
        offset_path = Path(os.environ["CAMCTL_OSMO_TEST_CLOCK_FILE"])
        controlled_time = _controlled_time(offset_path)
        _install_clock_ports(controlled_time)
        from camctl.cli import main as camctl_main

        exitcode = camctl_main(sys.argv[1:])
        record["exitcode"] = int(exitcode)
        return int(exitcode)
    except BaseException as error:
        record["exitcode"] = 1
        record["exception"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        record["finished_ns"] = _real_time.monotonic_ns()
        _append_record(log_path, record)


if __name__ == "__main__":
    raise SystemExit(main())
