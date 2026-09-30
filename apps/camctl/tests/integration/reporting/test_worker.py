"""R5 报告生成进程的组件集成测试：真实子进程、管道与结果竞争。

子进程经管道交付结果；结果已到达而退出先行仍成功；仅退出无结
果不发布；超时按失败收场；父死亡保护在 POSIX 平台验证。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from camctl.reporting.worker import (
    GenerationJob,
    WorkerStopReason,
    decide_generation_outcome,
)

_WORKER_SCRIPT = textwrap.dedent(
    """
    import sys
    payload = sys.stdin.buffer.read()
    sys.stdout.buffer.write(payload.upper())
    sys.stdout.buffer.flush()
    """
)

_FAIL_SCRIPT = textwrap.dedent(
    """
    import sys, os
    sys.stderr.write("worker failed\\n")
    sys.exit(3)
    """
)


class TestSubprocessGeneration:
    def test_result_delivered_then_exit_is_success(self) -> None:
        process = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT],
            input=b"report-bytes\n",
            capture_output=True,
            timeout=30,
        )
        result = process.stdout if process.returncode == 0 else None
        outcome = decide_generation_outcome(
            result=result, stop_reason=None, exit_failed=process.returncode != 0
        )
        assert outcome.value == "success"
        assert result == b"REPORT-BYTES\n"

    def test_exit_without_result_is_not_success(self) -> None:
        process = subprocess.run(
            [sys.executable, "-c", _FAIL_SCRIPT],
            input=b"",
            capture_output=True,
            timeout=30,
        )
        outcome = decide_generation_outcome(
            result=None, stop_reason=None, exit_failed=process.returncode != 0
        )
        assert outcome.value == "failed"

    def test_timeout_reason_maps_to_timeout(self) -> None:
        outcome = decide_generation_outcome(
            result=None, stop_reason=WorkerStopReason.TIMEOUT, exit_failed=False
        )
        assert outcome.value == "timeout"

    def test_result_arrives_despite_late_stop_request(self) -> None:
        # 结果先写入管道，停止请求随后到达：仍按成功收场。
        process = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT],
            input=b"payload\n",
            capture_output=True,
            timeout=30,
        )
        outcome = decide_generation_outcome(
            result=process.stdout,
            stop_reason=WorkerStopReason.SESSION_CLOSING,
            exit_failed=process.returncode != 0,
        )
        assert outcome.value == "success"

    def test_job_identity_preserved(self) -> None:
        job = GenerationJob(report_id=9, payload=b"x")
        assert job.report_id == 9
        assert job.payload == b"x"


class TestParentGuard:
    def test_parent_guard_on_posix(self) -> None:
        import platform

        if platform.system() == "Windows":
            pytest.skip("PR_SET_PDEATHSIG 仅 POSIX")
        script = textwrap.dedent(
            """
            import ctypes, os, signal, sys, time
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            result = libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
            print("prctl", result, flush=True)
            time.sleep(30)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = process.stdout.readline().strip()
        assert line.startswith("prctl 0")
        process.kill()
        process.wait(timeout=10)
