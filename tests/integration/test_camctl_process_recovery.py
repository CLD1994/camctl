"""O6 主机收场与下一调用组合的跨组件集成测试。

真实本地进程验证主机前提：旧会话遗留的本地执行进程尚未结束时
不启动新的 run；全部停止并确认后才放行；进程状态未知不解释为
没有遗留工作。发送终止信号本身不触发放行。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from camctl.session.host_guard import (
    HostSettlement,
    SettlementProbe,
    check_host_settlement,
)

_SLEEP_SCRIPT = textwrap.dedent(
    """
    import sys
    import time
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    time.sleep(120)
    """
)


def _spawn_tool() -> subprocess.Popen:
    child = subprocess.Popen(
        [sys.executable, "-c", _SLEEP_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "READY"
    return child


class _PopenProbe(SettlementProbe):
    """持有真实 Popen 对象的探测：alive 由 poll 结果表达。"""

    def __init__(self) -> None:
        self.processes: dict[int, subprocess.Popen] = {}

    def register(self, child: subprocess.Popen) -> int:
        self.processes[child.pid] = child
        return child.pid

    def is_alive(self, pid: int) -> bool | None:
        child = self.processes.get(pid)
        if child is None:
            return None
        return child.poll() is None

    def wait_exit(self, pid: int, timeout_s: float) -> bool:
        child = self.processes.get(pid)
        if child is None:
            return False
        try:
            child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        return True


pytestmark = pytest.mark.asyncio


async def test_next_run_waits_for_old_group_settlement() -> None:
    """旧会话工具仍在运行：新 run 不启动；停止并确认后才放行。"""
    probe = _PopenProbe()
    tool = _spawn_tool()
    try:
        pid = probe.register(tool)
        settlement = check_host_settlement([pid], probe)
        assert settlement is HostSettlement.WAIT
        assert tool.poll() is None  # 仍在运行，未被动过

        # 终止信号本身不触发放行：进程退出且等待确认后才 CLEAR。
        tool.kill()
        tool.wait(timeout=30)
        settled = check_host_settlement([pid], probe)
        assert settled is HostSettlement.CLEAR
    finally:
        if tool.poll() is None:
            tool.kill()
            tool.wait(timeout=30)


async def test_multiple_leftover_processes_all_wait() -> None:
    """多个遗留进程：任一存活都不放行。"""
    probe = _PopenProbe()
    tools = [_spawn_tool() for _ in range(3)]
    try:
        pids = [probe.register(tool) for tool in tools]
        assert check_host_settlement(pids, probe) is HostSettlement.WAIT
        tools[0].kill()
        tools[0].wait(timeout=30)
        assert check_host_settlement(pids, probe) is HostSettlement.WAIT
        for tool in tools[1:]:
            tool.kill()
            tool.wait(timeout=30)
        assert check_host_settlement(pids, probe) is HostSettlement.CLEAR
    finally:
        for tool in tools:
            if tool.poll() is None:
                tool.kill()
                tool.wait(timeout=30)


async def test_unknown_status_is_not_empty() -> None:
    """状态无法确认：不解释为没有遗留工作，不放行。"""

    class FailingProbe(_PopenProbe):
        def is_alive(self, pid: int) -> bool | None:
            return None

    tool = _spawn_tool()
    try:
        probe = FailingProbe()
        probe.register(tool)
        assert check_host_settlement([tool.pid], probe) is HostSettlement.UNKNOWN
    finally:
        tool.kill()
        tool.wait(timeout=30)


async def test_empty_leftover_set_is_clear() -> None:
    assert check_host_settlement([], _PopenProbe()) is HostSettlement.CLEAR


async def test_exit_not_observed_keeps_waiting() -> None:
    """进程实际退出但等待确认失败：保留等待与诊断责任。"""

    class NoWaitProbe(_PopenProbe):
        def wait_exit(self, pid: int, timeout_s: float) -> bool:
            return False

    probe = NoWaitProbe()
    tool = _spawn_tool()
    try:
        pid = probe.register(tool)
        tool.kill()
        time.sleep(0.2)
        assert check_host_settlement([pid], probe) is HostSettlement.WAIT
    finally:
        tool.wait(timeout=30)


#: WSL 内建组脚手架：setsid 建立独立进程组并输出 PGID 与工具 PID。
_GROUP_SCRIPT = textwrap.dedent(
    """
    setsid sh -c 'echo GROUP_READY; while true; do sleep 1; done' &
    group_pid=$!
    sleep 0.3
    echo "$group_pid"
    """
)


def _wsl_available() -> bool:
    try:
        completed = subprocess.run(
            ["wsl.exe", "-e", "true"], capture_output=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class _WslProbe(SettlementProbe):
    """WSL 内真实 Linux 进程探测：kill -0 判活，轮询确认退出。"""

    def _run(self, *args: str) -> int:
        completed = subprocess.run(
            ["wsl.exe", "-e", *args], capture_output=True, timeout=15
        )
        return completed.returncode

    def is_alive(self, pid: int) -> bool | None:
        code = self._run("kill", "-0", str(pid))
        if code == 0:
            return True
        if code == 1:
            return False
        return None

    def wait_exit(self, pid: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._run("kill", "-0", str(pid)) == 1:
                return True
            time.sleep(0.2)
        return False


@pytest.mark.skipif(not _wsl_available(), reason="WSL 不可用时跳过 Linux 进程组验证")
class TestLinuxGroupSettlement:
    async def test_linux_group_settlement_blocks_until_group_ends(self) -> None:
        """独立进程组存活：不放行；组信号结束后确认收场才放行。"""
        completed = subprocess.run(
            ["wsl.exe", "-e", "sh", "-c", _GROUP_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().isdigit()
        ]
        assert lines, completed.stdout + completed.stderr
        pgid = int(lines[0])
        probe = _WslProbe()
        try:
            assert probe.is_alive(pgid) is True
            assert check_host_settlement([pgid], probe) is HostSettlement.WAIT

            # 组信号结束整个进程组（接入模块的收场动作）。
            terminated = subprocess.run(
                ["wsl.exe", "-e", "kill", "--", f"-{pgid}"],
                capture_output=True,
                timeout=15,
            )
            assert terminated.returncode == 0
            settlement = check_host_settlement([pgid], probe, timeout_s=10)
            assert settlement is HostSettlement.CLEAR
        finally:
            subprocess.run(
                ["wsl.exe", "-e", "kill", "-9", "--", f"-{pgid}"],
                capture_output=True,
                timeout=15,
            )

    async def test_linux_child_inherits_group(self) -> None:
        """未脱离组的后代保持组归属：子进程 PGID 等于组 PGID。"""
        script = (
            "sh -c 'sleep 30 & child=$!; sleep 0.3;"
            " ps -o pgid= -p $child; ps -o pgid= -p $$'"
        )
        completed = subprocess.run(
            ["wsl.exe", "-e", "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        pgids = [
            int(line.strip())
            for line in completed.stdout.splitlines()
            if line.strip().isdigit()
        ]
        assert len(pgids) == 2 and pgids[0] == pgids[1]
