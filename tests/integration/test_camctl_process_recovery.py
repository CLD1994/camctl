"""O6：真实 C 接入模块、camctl CLI 与受管本地工具的收场组合。

C 驱动仅在系统接口边界提供信号和扫描同步点。业务受理、状态库、
run/submit 及工具启动均使用生产入口，不使用 Python 放行谓词。
"""
from __future__ import annotations

import ctypes
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from camctl_fixtures import Deployment

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="C 原组收场组合使用 Linux 和部署版 Python")
_REPO = Path(__file__).resolve().parents[2]
_ENTRY = Path(__file__).with_name("_host_recovery_entry.py")


def snapshot(pid: int) -> dict | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = text[text.rfind(")") + 2:].split()
    return {"state": fields[0], "pgid": int(fields[2]), "start": fields[19]}


def stopped(pid: int) -> bool:
    state = snapshot(pid)
    return state is None or state["state"] in ("Z", "X", "x")


@pytest.fixture(scope="session")
def recovery_driver(tmp_path_factory) -> Path:
    build = tmp_path_factory.mktemp("host-recovery-build")
    for command in (
        ["cmake", "-S", str(_REPO / "apps/host-demo"), "-B", str(build),
         "-DHOST_BUILD_TESTS=ON", f"-DHOST_TEST_PYTHON={sys.executable}", "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", str(build), "--target", "host_driver", "--", "-j2"],
    ):
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
    return build / "host_driver"


class GroupSession:
    def __init__(self, root: Path, driver: Path, *, exit_code=7, retries=0,
                 server="none", repeat_error=False):
        self.root = root
        self.deployment = Deployment(root, devices=False)
        initialized = self.deployment.camctl("init", "--config", str(self.deployment.config_path))
        assert initialized.exit_code == 0, initialized.stderr
        (root / "hold-first-exit").touch()
        launcher = root / "camctl-launcher"
        launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                            shlex.quote(str(_ENTRY)) + ' "$@"\n')
        launcher.chmod(0o755)
        self.log = root / "module.log"
        environment = os.environ.copy()
        environment.update(
            CAMCTL_TEST_GROUP_ROOT=str(root), CAMCTL_TEST_EXIT_CODE=str(exit_code),
            CAMCTL_TEST_SERVER_MODE=server, HOST_TEST_LARGE_LOG="1",
        )
        if repeat_error:
            environment["CAMCTL_TEST_REPEAT_ERROR"] = "1"
        self.proc = subprocess.Popen(
            [str(driver), str(launcher), str(self.deployment.ready),
             str(self.deployment.processing), str(self.log),
             str(self.deployment.config_path), "-", str(retries)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=environment)
        assert self.reply() == "initialized"
        self.until(lambda: (root / "exit-ready").exists(), "首个 CLI 和受管工具准备退出")
        self.old = self.calls("run")[0]
        self.tool = self.roles("tool")[0]

    def reply(self) -> str:
        assert select.select([self.proc.stdout], [], [], 10)[0], "C 驱动没有及时返回"
        text = self.proc.stdout.readline().strip()
        if not text and self.proc.poll() is not None:
            raise AssertionError(self.proc.stderr.read())
        return text

    def command(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.reply()

    def records(self) -> list[dict]:
        trace = self.root / "process-trace"
        if not trace.exists():
            return []
        return [json.loads(line) for line in trace.read_text().splitlines() if line.endswith("}")]

    def roles(self, role: str) -> list[dict]:
        return [record for record in self.records() if record["role"] == role]

    def calls(self, command: str) -> list[dict]:
        return [record for record in self.roles("cli") if record["command"] == command]

    def logs(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def until(self, predicate, message: str):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            assert self.proc.poll() is None, self.proc.stderr.read()
            time.sleep(.005)
        raise AssertionError(f"{message} 超时\n{self.records()}\n{self.logs()}")

    def submit(self, request=201, *, wait=True):
        scheduled = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        path = self.deployment.write_plan({
            "request_id": str(request),
            "plan": {"name": f"同步 {request}", "actions": [{
                "name": "sync", "type": "report_status", "scheduled_at": scheduled,
                "params": {"scope": "full"},
            }]},
        })
        assert self.command(f"submit {path}") == "submit 0"
        if wait:
            self.until(lambda: "command=submit" in self.logs() and
                       "result=success" in self.logs() and "needs_run=1" in self.logs(),
                       "真实 submit 建立待启动要求")
        return path

    def exit_first(self):
        (self.root / "hold-first-exit").unlink()
        self.until(lambda: snapshot(self.old["pid"]) and
                   snapshot(self.old["pid"])["state"] == "Z", "旧 CLI 的退出记录被保留")

    def assert_held(self):
        assert len(self.calls("run")) == 1
        old = snapshot(self.old["pid"])
        assert old is not None and old["state"] == "Z"
        assert old["start"] == self.old["start"]

    def close(self):
        if not hasattr(self, "proc"):
            return
        if self.proc.poll() is not None:
            self.proc.communicate(timeout=5)
            return
        # 冻结已知测试主程序后取得其具体子进程，避免清理时产生新调用。
        os.kill(self.proc.pid, signal.SIGSTOP)
        os.waitpid(self.proc.pid, os.WUNTRACED)
        groups = {record["pgid"] for record in self.records()}
        for task in Path(f"/proc/{self.proc.pid}/task").iterdir():
            children = (task / "children").read_text().split()
            for child in children:
                state = snapshot(int(child))
                if state:
                    groups.add(state["pgid"])
        for group in groups:
            assert group > 1 and group != os.getpgrp()
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.proc.kill()
        self.proc.communicate(timeout=5)
        # 测试父进程接管本测试的孤儿后代；只回收这些组里的具体 PID。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            owned = []
            for task in Path("/proc/self/task").iterdir():
                for child in (task / "children").read_text().split():
                    state = snapshot(int(child))
                    if state and state["pgid"] in groups:
                        owned.append(int(child))
            if not owned:
                return
            for pid in owned:
                os.waitpid(pid, os.WNOHANG)
            time.sleep(.005)
        raise AssertionError(f"测试进程未收场: {owned}")


@pytest.fixture
def session_factory(tmp_path, recovery_driver):
    libc = ctypes.CDLL(None, use_errno=True)
    original = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(original), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    sessions = []

    def create(**options):
        session = GroupSession.__new__(GroupSession)
        sessions.append(session)
        session.__init__(tmp_path, recovery_driver, **options)
        return session

    try:
        yield create
    finally:
        try:
            for session in sessions:
                session.close()
        finally:
            assert libc.prctl(36, original.value, 0, 0, 0) == 0


@pytest.mark.parametrize("exit_code", [0, 7])
def test_next_run_waits_for_old_group_settlement(session_factory, exit_code):
    session = session_factory(exit_code=exit_code)
    assert session.old["pgid"] == session.old["pid"]
    assert session.tool["pgid"] == session.old["pgid"] != session.proc.pid
    session.submit()
    assert session.command("block-kill") == "kill-blocked"
    session.exit_first()
    session.until(lambda: "group_kill_sent" in session.logs(), "终止请求")
    assert not stopped(session.tool["pid"])
    session.assert_held()
    assert session.command("kill-and-hold") == "kill-enabled"
    session.until(lambda: stopped(session.tool["pid"]), "真实 SIGKILL 终止工具")
    session.assert_held()  # 工具已停止但核验未完成，仍不能放行或回收。
    assert session.command("resume-scan") == "scan-enabled"
    session.until(lambda: len(session.calls("run")) == 2, "最终回收后启动下一 run")
    assert snapshot(session.old["pid"]) is None
    lines = session.logs().splitlines()
    reaped = next(i for i, line in enumerate(lines) if
                  f"pid={session.old['pid']} stream=group_reaped" in line)
    next_spawn = next(i for i, line in enumerate(lines) if
                      "call=3 command=run operation=spawn" in line)
    assert reaped < next_spawn
    assert "call=3 command=run operation=spawn plan=(none) automatic=0 retries=0" in session.logs()


def test_abnormal_restart_keeps_shared_budget(session_factory):
    session = session_factory(retries=1, repeat_error=True)
    session.command("block-kill")
    session.exit_first()
    session.until(lambda: "group_kill_sent" in session.logs(), "旧组终止请求")
    session.assert_held()
    session.command("release-kill")
    session.until(lambda: "retry_exhausted used=1" in session.logs(), "既有预算耗尽")
    assert len(session.calls("run")) == 2
    assert "automatic=1 retries=1" in session.logs()
    session.submit()
    session.until(lambda: len(session.calls("run")) == 3, "新提交触发一次正常启动")
    session.until(lambda: session.logs().count("retry_exhausted used=1") == 2, "预算没有重置")
    assert "automatic=0 retries=1" in session.logs()


def test_unknown_scan_keeps_call_and_pending_request(session_factory):
    session = session_factory()
    session.submit()
    session.command("deny-scan")
    session.exit_first()
    session.until(lambda: "stream=group_unknown" in session.logs(), "扫描权限故障诊断")
    session.assert_held()
    assert not stopped(session.tool["pid"])
    session.command("resume-scan")
    session.until(lambda: len(session.calls("run")) == 2, "扫描恢复后满足原待启动要求")


def test_early_reap_never_signals_stale_group_or_starts_run(session_factory):
    session = session_factory()
    session.submit()
    session.command("pause-scan")
    session.exit_first()
    assert session.command(f"steal-exit {session.old['pid']}") == f"stolen {session.old['pid']}"
    session.until(lambda: "stream=wait_unknown" in session.logs(), "提前回收诊断")
    session.command("resume-scan")
    session.submit(202, wait=False)
    session.until(lambda: len(session.calls("submit")) == 2, "原 run 身份失效时仍可并行提交")
    assert len(session.calls("run")) == 1
    assert session.command("group-signals") == "group-signals 0"
    assert not stopped(session.tool["pid"])


@pytest.mark.parametrize("server_mode", ["grouped", "detached", "racing"])
def test_original_group_does_not_kill_parallel_submit_or_other_groups(session_factory, server_mode):
    session = session_factory(server=server_mode)
    session.submit()
    (session.root / "hold-submit").touch()
    session.submit(202, wait=False)
    session.until(lambda: len(session.calls("submit")) == 2, "并行 submit 已启动")
    parallel = session.calls("submit")[-1]
    server = session.roles("server")[0]
    assert parallel["pgid"] != session.old["pgid"]
    assert (server["pgid"] == session.old["pgid"]) == (server_mode != "detached")
    session.command("kill-and-hold")
    if server_mode == "racing":
        (session.root / "detach-server").touch()
    session.exit_first()
    session.until(lambda: stopped(session.tool["pid"]), "旧组工具停止")
    session.assert_held()
    assert not stopped(parallel["pid"])
    if server_mode == "racing":
        session.until(lambda: stopped(server["pid"]) or
                      snapshot(server["pid"])["pgid"] == server["pid"], "脱离与原组终止竞争结束")
    else:
        assert stopped(server["pid"]) == (server_mode == "grouped")
    assert session.command("coexist") == "coexists"
    session.command("resume-scan")
    session.until(lambda: len(session.calls("run")) == 2, "原组收场后放行")
    (session.root / "hold-submit").unlink()
