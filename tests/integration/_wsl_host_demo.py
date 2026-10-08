"""真实 host-demo 主程序的构建、启动与替身桥 launcher 支持。

C 模块为 POSIX 实现，按部署验证裁决在 WSL x86 Linux 构建；构建产
物经 worktree 固定在部署验证环境内，跨测试文件以 session 夹具共
享一次构建。带设备的递交链经 ``write_stub_launcher`` 生成的部署装
配桥 launcher 接入受契约约束的设备替身，与正式部署装配同一接入
面，随后不加修改地运行 camctl CLI。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent

_WSL_WORKTREE = "/tmp/camctl-i4-worktree"
_WSL_BUILD = "/tmp/camctl-i4-build"
_WSL_DEMO = "/tmp/camctl-i4-build/host-demo"
_WSL_LAUNCHER = "/tmp/camctl-i4-launcher.sh"

_PROMPT = "模块初始化完成；可输入 submit <绝对路径>、claim、logs 或 help。"

#: 测试自身已在 Linux（部署验证环境）时，构建与执行不再经 wsl.exe。
_NATIVE_POSIX = os.name == "posix"

#: camctl 启动桥：读 CAMCTL_TEST_DRIVER_SPEC 登记替身后进入生产 CLI。
_BRIDGE = _REPO / "tests" / "integration" / "_camctl_stub_entry.py"


def to_wsl(path: Path) -> str:
    """Windows 盘符路径转 WSL 挂载形式（C:\\a\\b → /mnt/c/a/b）。

    测试自身已在 Linux（部署验证环境）时路径保持不变。
    """
    text = str(path).replace("\\", "/")
    if _NATIVE_POSIX or text[1:2] != ":":
        return text
    return f"/mnt/{text[0].lower()}{text[2:]}"


def run_wsl(script: str, timeout_s: float = 600.0) -> subprocess.CompletedProcess:
    """在部署验证环境执行 shell 步骤；Windows 开发机经 wsl.exe 进入。"""
    if _NATIVE_POSIX:
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_s)
    return subprocess.run(
        ["wsl.exe", "-e", "sh", "-c", script],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout_s)


def query_state(state_db: Path, sql: str, params=()) -> list[tuple]:
    import sqlite3

    connection = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def wait_for(predicate, timeout_s: float, message: str):
    """轮询直到谓词为真；显式同步点，不使用随机 sleep。"""
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"等待超时: {message}"


def _write_launcher(wsl_path: str, body: str) -> None:
    written = run_wsl(
        f"cat > {wsl_path} <<'LAUNCHER'\n{body}LAUNCHER\nchmod +x {wsl_path}")
    assert written.returncode == 0, written.stderr


_LAUNCHER_CONVERSION = (
    "conv() {\n"
    "  case \"$1\" in\n"
    "    /mnt/[a-z]/*) local d=${1#/mnt/}; d=${d%%/*};"
    " printf '%s:/%s' \"$(echo \"$d\" | tr 'a-z' 'A-Z')\" \"${1#/mnt/$d/}\";;\n"
    "    *) printf '%s' \"$1\";;\n"
    "  esac\n"
    "}\n"
    "args=()\n"
    "for a in \"$@\"; do [ -n \"$a\" ] && args+=(\"$(conv \"$a\")\"); done\n")


def _launcher_body(exec_line: str) -> str:
    return f"#!/bin/bash\n{_LAUNCHER_CONVERSION}{exec_line}"


def build_host_demo() -> str:
    """在部署验证环境构建真实 host-demo 并部署 camctl 启动桥。

    WSL 不可用时跳过：C 主程序组合按部署验证裁决需要 WSL x86
    Linux。构建经固定 worktree 固定提交内容，基础 launcher 直接
    进入生产 CLI（无设备替身）。
    """
    import pytest

    if not _NATIVE_POSIX:
        probe = subprocess.run(["wsl.exe", "-e", "true"],
                               capture_output=True, timeout=30)
        if probe.returncode != 0:
            pytest.skip("WSL 不可用：C 主程序组合按部署验证裁决需要 WSL x86 Linux")
    python = Path(sys.executable)
    steps = [
        f"rm -rf {_WSL_WORKTREE} {_WSL_BUILD}",
        f"git -C {to_wsl(_REPO)} worktree prune",
        f"git -C {to_wsl(_REPO)} worktree add --detach {_WSL_WORKTREE} HEAD",
        (f"cmake -S {_WSL_WORKTREE}/apps/host-demo -B {_WSL_BUILD}"
         " -DCMAKE_BUILD_TYPE=Release >/dev/null"),
        (f"cmake --build {_WSL_BUILD} --target host-demo -j4 >/dev/null"),
    ]
    for step in steps:
        result = run_wsl(step)
        assert result.returncode == 0, (
            f"WSL 构建步骤失败: {step}\n{result.stderr}")
    direct = (
        f"exec {to_wsl(python)} -c"
        " 'import sys; from camctl.cli import main; sys.exit(main(sys.argv[1:]))'"
        " \"${args[@]}\"\n")
    _write_launcher(_WSL_LAUNCHER, _launcher_body(direct))
    return _WSL_DEMO


def write_stub_launcher(deployment) -> str:
    """为带设备的部署生成装配桥 launcher，返回其 WSL 挂载路径。

    环境变量嵌入部署自身的替身剧本与状态库路径；host-demo 的每
    次 submit/run 都经部署装配桥登记替身后进入生产 CLI。launcher
    放在部署目录内，生命周期与部署一致；返回路径供 host-demo 的
    ``--camctl`` 参数使用，须为部署验证环境的绝对路径（以 / 开
    头）。
    """
    python = Path(sys.executable)
    bridged = (
        f"export CAMCTL_TEST_DRIVER_SPEC='{deployment.driver_spec}'\n"
        f"export CAMCTL_TEST_STATE_DB='{deployment.state_db}'\n"
        # WSL 启动 Windows 进程时只传递 WSLENV 声明的变量；装配桥的
        # 两个输入都必须随 exec 到达 camctl 进程。
        "export WSLENV=\"${WSLENV:+$WSLENV:}"
        "CAMCTL_TEST_DRIVER_SPEC:CAMCTL_TEST_STATE_DB\"\n"
        f"exec {to_wsl(python)} '{_BRIDGE}' \"${{args[@]}}\"\n")
    launcher = deployment.root / "stub-launcher.sh"
    _write_launcher(to_wsl(launcher), _launcher_body(bridged))
    return to_wsl(launcher)


class HostDemo:
    """真实 host-demo 主程序的子进程会话（终端命令协议）。"""

    def __init__(self, deployment, demo_path: str,
                 launcher: str | None = None) -> None:
        self.deployment = deployment
        self.log_path = deployment.root / "module.log"
        self._demo_path = demo_path
        self._launcher = launcher or _WSL_LAUNCHER
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        command = [
            self._demo_path,
            "--camctl", self._launcher,
            "--ready", to_wsl(self.deployment.ready),
            "--processing", to_wsl(self.deployment.processing),
            "--log", to_wsl(self.log_path),
            "--config", to_wsl(self.deployment.config_path)]
        if not _NATIVE_POSIX:
            command = ["wsl.exe", "-e", *command]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        assert self._reply() == _PROMPT

    def _reply(self) -> str:
        assert self.proc is not None
        line = self.proc.stdout.readline().strip()
        if not line and self.proc.poll() is not None:
            self.fail("host-demo 提前退出")
        return line

    def fail(self, message: str) -> None:
        assert self.proc is not None
        raise AssertionError(f"{message}: {self.proc.stderr.read()[:400]}")

    def submit(self, plan: Path) -> None:
        assert self.proc is not None
        self.proc.stdin.write(f"submit {to_wsl(plan)}\n")
        self.proc.stdin.flush()
        assert self._reply() == "路径已接收；计划受理与执行结果请查看状态报告。"

    def claim(self) -> None:
        assert self.proc is not None
        self.proc.stdin.write("claim\n")
        self.proc.stdin.flush()
        assert self._reply() == "领取调用已返回；可按主程序流程处理 processing 内的文件。"

    def module_log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)
        run_wsl("pkill -f host-demo || true", timeout_s=60)
        self.proc = None
