from decimal import Decimal
import asyncio
import os
import signal
import sys
import time

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.operations.owned_calls import owned_tool_call


BINDING = DeviceBinding("camera-1", DRIVER_ID)


async def check(driver):
    return await driver.check_availability(BINDING, expected_serial="CAMERA-1",
        expected_product_id=10, timeout_s=Decimal(5))


@pytest.mark.skipif(os.name == "nt", reason="受管脚本使用 POSIX 可执行入口")
@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 2])
async def test_real_process_pass_requires_zero_exit_and_keeps_streams(tmp_path, exit_code):
    executable = tmp_path / "hb_cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "assert sys.argv[1:3] == ['--pid', '0x000a']\n"
        "if sys.argv[3] == 'list': print('vid=0x2756 pid=0x000a  bus=  1 addr= 23')\n"
        "elif sys.argv[3] == 'info': print('serial : CAMERA-1')\n"
        "elif sys.argv[3] == 'doctor':\n"
        "    print('doctor: PASS')\n"
        f"    if {exit_code}: print('diagnostic', file=sys.stderr)\n"
        f"    sys.exit({exit_code})\n", encoding="utf-8")
    executable.chmod(0o700)
    device = {"kind": "camera", "driver": DRIVER_ID,
              "hasselblad": {"executable": str(executable), "serial": "CAMERA-1"}}
    real_driver = HasselbladDriver({"camera-1": device}, None, monotonic_ns=time.monotonic_ns)
    result = await check(real_driver)
    assert result.available is (exit_code == 0)
    assert result.reason == (None if exit_code == 0 else "exit_nonzero")
    assert [call.name for call in result.calls] == ["list", "info", "doctor"]
    raw = result.calls[-1].outcome
    assert raw.exit.exit_code == exit_code
    assert raw.output == b"doctor: PASS\n"
    assert raw.stderr == (b"diagnostic\n" if exit_code else b"")


@pytest.mark.skipif(os.name == "nt", reason="受管脚本使用 POSIX 信号")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_process_settles_and_preserves_streams_before_result(tmp_path, cancel):
    marker = tmp_path / "started"
    executable = tmp_path / "hb_cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, sys, time\n"
        "assert sys.argv[1:3] == ['--pid', '0x000a']\n"
        "if sys.argv[3] == 'list': print('vid=0x2756 pid=0x000a  bus=  1 addr= 23')\n"
        "elif sys.argv[3] == 'info': print('serial : CAMERA-1')\n"
        "elif sys.argv[3] == 'doctor':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    print('doctor: PASS', flush=True)\n"
        "    print('diagnostic', file=sys.stderr, flush=True)\n"
        f"    pathlib.Path({str(marker)!r}).write_text('started')\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    device = {"kind": "camera", "driver": DRIVER_ID,
              "hasselblad": {"executable": str(executable), "serial": "CAMERA-1"}}
    real_driver = HasselbladDriver({"camera-1": device}, None,
                                  monotonic_ns=time.monotonic_ns, terminate_grace_s=Decimal("0.05"))
    saved = []

    @owned_tool_call
    async def consume():
        value = await real_driver.check_availability(BINDING, expected_serial="CAMERA-1",
            expected_product_id=10, timeout_s=Decimal("0.5") if not cancel else Decimal(10))
        saved.append(value)

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                done, _ = await asyncio.wait({task}, timeout=0.01)
                if done:
                    await task
                    break
        assert marker.exists()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    finally:
        if not task.done():
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert done, "可用性调用未在取消后结束"
        if not task.cancelled():
            task.exception()
    assert len(saved) == 1
    result = saved[0]
    assert not result.available and result.settled
    assert result.reason == ("cancelled" if cancel else "timeout")
    assert [call.name for call in result.calls] == ["list", "info", "doctor"]
    raw = result.calls[-1].outcome
    assert raw.exit.signal == signal.SIGKILL and raw.used_grace_s == Decimal("0.05")
    assert raw.output == b"doctor: PASS\n" and raw.stderr == b"diagnostic\n"
