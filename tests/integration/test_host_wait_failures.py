"""等待观察器通过真实 C host、文件日志和 SQLite 提供失败诊断。"""

from __future__ import annotations

import json
import os
import select
import signal
import shlex
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import _wsl_host_demo as host_support
from _wsl_host_demo import HostDemo, wait_for
from camctl.contracts.enums import enum_for


def _deployment(root):
    ready, processing = root / "ready", root / "processing"
    ready.mkdir()
    processing.mkdir()
    return SimpleNamespace(
        root=root, ready=ready, processing=processing, config_path=None,
        state_db=root / "state.db",
    )


@pytest.mark.skipif(os.name != "posix", reason="测试进程使用 POSIX 可执行脚本")
@pytest.mark.parametrize("operation,output", [
    ("start", ""), ("start", "partial"),
    ("submit", ""), ("claim", "partial"),
])
def test_host_reply_requires_a_complete_line_within_deadline(tmp_path, operation, output):
    deployment = _deployment(tmp_path)
    host = tmp_path / "host"
    host.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        + ("print('submit < claim logs help', flush=True)\nsys.stdin.readline()\n"
           if operation != "start" else "")
        + f"sys.stdout.write({output!r}); sys.stdout.flush()\n"
        "sys.stderr.write('reply diagnostic'); sys.stderr.flush()\n"
        "time.sleep(30)\n", encoding="utf-8")
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher_is_camctl=False, reply_timeout_s=0.5)
    started = time.monotonic()
    try:
        if operation != "start":
            demo.start()
        with pytest.raises(AssertionError) as failure:
            if operation == "start":
                demo.start()
            elif operation == "submit":
                demo.submit(tmp_path / "plan")
            else:
                demo.claim()
        assert "超时" in str(failure.value)
        assert "reply diagnostic" in str(failure.value)
        assert time.monotonic() - started < 5
    finally:
        demo.stop()


@pytest.mark.skipif(os.name != "posix", reason="测试进程使用 POSIX 可执行脚本")
@pytest.mark.parametrize("hold_stdout", [False, True])
def test_host_exit_does_not_wait_for_descendant_to_close_output(tmp_path, hold_stdout):
    deployment = _deployment(tmp_path)
    host = tmp_path / "host"
    child_script = ("import time; from pathlib import Path; time.sleep(2); "
                    f"Path({str(tmp_path / 'child-finished')!r}).touch()")
    host.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}], "
        f"stdin=subprocess.DEVNULL, stdout={'None' if hold_stdout else 'subprocess.DEVNULL'})\n"
        "identity = str(child.pid)\n"
        "if sys.platform == 'linux':\n"
        "    identity += ' ' + Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]\n"
        f"open({str(tmp_path / 'child-pid')!r}, 'w').write(identity)\n"
        "sys.stderr.write('original error'); sys.stderr.flush()\n"
        "sys.exit(7)\n", encoding="utf-8")
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher_is_camctl=False, reply_timeout_s=1)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError) as failure:
            demo.start()
        assert "提前退出" in str(failure.value)
        assert "original error" in str(failure.value)
        assert time.monotonic() - started < 1.5
    finally:
        try:
            demo.stop()
        finally:
            pid_file = tmp_path / "child-pid"
            if pid_file.exists():
                identity = pid_file.read_text().split()
                pid = int(identity[0])
                finished = tmp_path / "child-finished"
                if hasattr(os, "pidfd_open"):
                    try:
                        pidfd = os.pidfd_open(pid)
                    except ProcessLookupError:
                        assert finished.exists(), f"子进程 {pid} 未留下完成记录"
                    else:
                        try:
                            try:
                                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                            except FileNotFoundError:
                                stat = None
                            if stat is None or stat[19] != identity[1]:
                                assert finished.exists(), f"子进程 {pid} 未留下完成记录"
                            elif not select.select([pidfd], [], [], 3)[0]:
                                try:
                                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                                assert select.select([pidfd], [], [], 1)[0], (
                                    f"测试创建的子进程 {pid} 未能结束")
                                raise AssertionError(f"测试创建的子进程 {pid} 未按时结束")
                            else:
                                assert finished.exists(), f"子进程 {pid} 未留下完成记录"
                        finally:
                            os.close(pidfd)
                else:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if finished.exists():
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                        time.sleep(.01)
                    else:
                        raise AssertionError(f"测试创建的子进程 {pid} 未在期限内结束")


@pytest.mark.skipif(os.name != "posix", reason="测试进程使用 POSIX 可执行脚本")
def test_host_exit_after_empty_output_probe_preserves_error(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    host = tmp_path / "host"
    host.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdin.readline()\n"
        "sys.stderr.write('late original error'); sys.stderr.flush()\n"
        "sys.exit(7)\n", encoding="utf-8")
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher_is_camctl=False, reply_timeout_s=2)
    probe = host_support._readable_pipes
    first_probe = []

    def release_after_empty_probe(pipes, timeout):
        if not first_probe:
            first_probe.append(probe(pipes, 0))
            demo.proc.stdin.write("continue\n")
            demo.proc.stdin.flush()
            demo.proc.wait(timeout=1)
            return first_probe[0]
        return probe(pipes, timeout)

    monkeypatch.setattr(host_support, "_readable_pipes", release_after_empty_probe)
    try:
        with pytest.raises(AssertionError) as failure:
            demo.start()
        assert first_probe == [[]]
        assert "late original error" in str(failure.value)
        demo._reader.join(timeout=1)
        assert not demo._reader.is_alive()
    finally:
        demo.stop()


@pytest.mark.skipif(os.name != "posix", reason="测试进程使用 POSIX 可执行脚本")
def test_host_reply_drains_stderr_before_accepting_complete_line(tmp_path):
    deployment = _deployment(tmp_path)
    host = tmp_path / "host"
    host.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stderr.write('x' * 200000); sys.stderr.flush()\n"
        "print('submit < claim logs help', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8")
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher_is_camctl=False, reply_timeout_s=2)
    try:
        demo.start()
        assert demo.proc.poll() is None
    finally:
        demo.stop()


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX host 观察器")
@pytest.mark.parametrize("outcome", [
    "error", "abnormal", "success", "not_started", "abnormal_once", "not_started_once",
])
def test_wait_observes_current_run_result_while_host_stays_alive(tmp_path, host_demo, outcome):
    deployment = _deployment(tmp_path)
    launcher = tmp_path / "cli"
    message = {"kind": "error", "body": {"reason": "state_db_error", "details": {"stage": "execution"}}}
    if outcome in {"success", "abnormal_once", "not_started_once"}:
        message = {"kind": "succeeded"}
    script = (
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps(message) + "'\n"
        + ("printf '%s\\n' 'result=error is diagnostic text' >&2\n" if outcome == "success" else "")
        + ("exit 0\n" if message["kind"] == "succeeded" else "exit 1\n" if outcome == "error" else "exit 7\n")
    )
    if outcome == "abnormal_once":
        marker = tmp_path / "first-attempt"
        script = "#!/bin/sh\nif [ ! -f '" + str(marker) + "' ]; then touch '" + str(marker) + "'; exit 7; fi\n" + script
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(0o700)
    if outcome in {"not_started", "not_started_once"}:
        launcher.unlink()
    host = tmp_path / "host"
    # 永久故障只需走完一次真实重试；恢复分区保留创建入口的时间。
    retry_delay = 5000 if outcome.endswith("_once") else 0
    host.write_text(
        "#!/bin/sh\nexec " + shlex.quote(host_demo)
        + f' --retry-limit 1 --retry-delay-ms {retry_delay} "$@"\n',
        encoding="utf-8",
    )
    host.chmod(0o700)
    demo = HostDemo(deployment, str(host), launcher=str(launcher), launcher_is_camctl=False)
    # 复用同一部署目录时，前一个 host 的失败不能污染新 host。
    demo.log_path.write_text("call=1 command=run pid=99 result=error exit=1\n", encoding="utf-8")
    check_failure = demo.watch_failures()
    try:
        demo.start()
        if outcome == "not_started_once":
            wait_for(lambda: "not_started errno=" in demo.module_log(), 30, "未观察到启动失败")
            assert check_failure() is None
            launcher.write_text(script, encoding="utf-8")
            launcher.chmod(0o700)
        if outcome in {"success", "abnormal_once", "not_started_once"}:
            result = wait_for(
                lambda: "finished" if "result=success" in demo.module_log() else None,
                30, "正常 run 未结束", failure=check_failure,
            )
            assert result == "finished"
            assert check_failure() is None
            assert wait_for(lambda: ["ready"], 30, "兼容原调用") == ["ready"]
        else:
            with pytest.raises(AssertionError, match="run 调用") as failure:
                wait_for(lambda: None, 30, "等待业务同步点", failure=check_failure)
            assert "等待超时" not in str(failure.value)
            assert "command=run" in str(failure.value)
            if outcome == "error":
                assert "state_db_error" in str(failure.value)
            # 之后的新请求有独立观察起点，并可重新建立 run 职责。
            later_log_start = len(demo.module_log())
            later_failure = demo.watch_failures()
            launcher.write_text(
                '#!/bin/sh\nif [ "$1" = submit ]; then\n'
                'printf \'%s\\n\' \'{"kind":"succeeded","body":{"needs_run":true}}\'\n'
                'else printf \'%s\\n\' \'{"kind":"succeeded"}\'; fi\n',
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            plan = tmp_path / "later-plan.json"
            plan.write_text("{}", encoding="utf-8")
            demo.submit(plan)
            wait_for(
                lambda: any("command=run" in line and "result=success" in line
                            for line in demo.module_log()[later_log_start:].splitlines()),
                30, "后续请求未重新完成 run", failure=later_failure,
            )
        assert demo.proc.poll() is None
    finally:
        demo.stop()


@pytest.mark.parametrize("name", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELED"])
def test_wait_limits_incompatible_terminal_to_requested_actions(tmp_path, name):
    deployment = _deployment(tmp_path)
    statuses = enum_for("actions.status")
    with closing(sqlite3.connect(deployment.state_db)) as connection, connection:
        connection.executescript(
            "CREATE TABLE plans(id INTEGER,request_id TEXT);"
            "CREATE TABLE actions(id INTEGER,plan_id INTEGER,name TEXT,status INTEGER,"
            "error_code INTEGER,error_details_json TEXT);"
            "INSERT INTO plans VALUES(1,'old'),(2,'current');"
        )
        connection.executemany(
            "INSERT INTO actions VALUES(?,?,?,?,NULL,NULL)",
            [(1, 1, "capture", int(statuses.FAILED)),
             (2, 2, "other", int(statuses.FAILED)),
             (3, 2, "capture", int(statuses[name]))],
        )
    demo = HostDemo(deployment, "unused")
    reason = demo.watch_failures(request_id="current", expected_statuses={"capture": "SUCCEEDED"})()
    if name in {"FAILED", "EXPIRED", "CANCELED"}:
        assert "current" in reason and "capture" in reason and name in reason
    else:
        assert reason is None
