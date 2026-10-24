"""安装版 camctl 入口的绝对时钟边界包装器。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time as _real_time

from _osmo_builtin_clock import ControlledClock


def _append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _install_clock_ports(controlled_time) -> None:
    """替换安装进程各自捕获的时钟模块引用，不改全局标准库。"""
    from camctl.bootstrap import clocks, lifecycle
    from camctl.bootstrap import capture_assembly, cleanup_assembly, obtain_assembly
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


def _install_single_original_task() -> None:
    """既有清理链显式采用单原片任务声明，继续覆盖独立修复产物。"""
    from dataclasses import replace
    from camctl.devices.drivers.adb_cameras import osmo360ii_capture
    from camctl.devices.tasks import VideoResultMode

    original = osmo360ii_capture._task

    def task(params, *, result_mode=VideoResultMode.SINGLE_FILE):
        declared = original(params, result_mode=result_mode)
        return replace(
            declared,
            result_policy=None,
            product_rules=(
                {
                    "kind": "video",
                    "format_id": "osv",
                    "min_count": 1,
                    "exact_count": 1,
                    "require_pairing": False,
                    "min_size_bytes": 1,
                },
            ),
        )

    osmo360ii_capture._task = task


def main() -> int:
    started_ns = _real_time.monotonic_ns()
    log_path = Path(os.environ["CAMCTL_OSMO_TEST_PROCESS_LOG"])
    record = {
        "stage": "started",
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "python": sys.executable,
        "argv": [sys.argv[0], *sys.argv[1:]],
        "command": list(sys.argv[1:]),
        "started_ns": started_ns,
    }
    exitcode = 1
    try:
        state_path = Path(os.environ["CAMCTL_OSMO_TEST_CLOCK_FILE"])
        clock_log_path = Path(os.environ["CAMCTL_OSMO_TEST_CLOCK_LOG"])
        controlled_time = ControlledClock(state_path, clock_log_path)
        _install_clock_ports(controlled_time)
        if os.environ.get("CAMCTL_OSMO_TEST_SINGLE_ORIGINAL") == "1":
            _install_single_original_task()
        if os.environ.get("CAMCTL_OSMO_TEST_SEGMENT_FAULT"):
            from _osmo_segment_observer import install

            install()
        import camctl
        from camctl import cli as camctl_cli
        from camctl.capture import handlers
        from camctl.devices.drivers.adb_cameras import osmo360ii_capture, osmo360ii_results

        record["import_origins"] = {
            "camctl": camctl.__file__,
            "camctl.cli": camctl_cli.__file__,
            "camctl.capture.handlers": handlers.__file__,
            "camctl.devices.drivers.adb_cameras.osmo360ii_capture": osmo360ii_capture.__file__,
            "camctl.devices.drivers.adb_cameras.osmo360ii_results": osmo360ii_results.__file__,
        }
        _append_record(log_path, record)
        exitcode = int(camctl_cli.main(sys.argv[1:]))
    except BaseException as error:
        record["exception"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        final = {
            **record,
            "stage": "finished",
            "exitcode": int(exitcode),
            "finished_ns": _real_time.monotonic_ns(),
        }
        _append_record(log_path, final)
    return exitcode


if __name__ == "__main__":
    raise SystemExit(main())
