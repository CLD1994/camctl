"""I4 无设备的真实导出、报告、领取与 ACK 闭环。

真实 camctl CLI（无设备部署）完成受理与报告发布；WSL 编译的真实
host-demo 主程序经受管工具调用 camctl submit/run，并把 ready 报告
领取到 processing；客户端经其服务的真实导出路径产生计划（消费
describe 能力、分配整数请求身份），经真实导入路径核验并保存报
告，随后导出的下一份计划自动携带 last_report_id（ACK），由统一
受理接口吸收。C 模块为 POSIX 实现，按部署验证裁决在 WSL x86
Linux 构建，操作同一部署目录。
"""

from __future__ import annotations

import glob
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from camctl_fixtures import Deployment, future_schedule

_REPO = Path(__file__).resolve().parent.parent.parent

_WSL_WORKTREE = "/tmp/camctl-i4-worktree"
_WSL_BUILD = "/tmp/camctl-i4-build"
_WSL_DEMO = "/tmp/camctl-i4-build/host-demo"
_WSL_LAUNCHER = "/tmp/camctl-i4-launcher.sh"

_PROMPT = "模块初始化完成；可输入 submit <绝对路径>、claim、logs 或 help。"


def _to_wsl(path: Path) -> str:
    """Windows 路径转 WSL 挂载形式（C:\\a\\b → /mnt/c/a/b）。"""
    text = str(path).replace("\\", "/")
    return f"/mnt/{text[0].lower()}{text[2:]}"


def _wsl(script: str, timeout_s: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl.exe", "-e", "sh", "-c", script],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout_s)


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    connection = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _await(predicate, timeout_s: float, message: str):
    """轮询直到谓词为真；显式同步点，不使用随机 sleep。"""
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"等待超时: {message}"


@pytest.fixture(scope="module")
def host_demo() -> str:
    """在 WSL 构建真实 host-demo 并部署 camctl 启动桥。"""
    probe = subprocess.run(["wsl.exe", "-e", "true"],
                           capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("WSL 不可用：C 主程序组合按部署验证裁决需要 WSL x86 Linux")
    python = Path(__import__("sys").executable)
    steps = [
        f"rm -rf {_WSL_WORKTREE} {_WSL_BUILD}",
        f"git -C {_to_wsl(_REPO)} worktree prune",
        f"git -C {_to_wsl(_REPO)} worktree add --detach {_WSL_WORKTREE} HEAD",
        (f"cmake -S {_WSL_WORKTREE}/apps/host-demo -B {_WSL_BUILD}"
         " -DCMAKE_BUILD_TYPE=Release >/dev/null"),
        (f"cmake --build {_WSL_BUILD} --target host-demo -j4 >/dev/null"),
    ]
    for step in steps:
        result = _wsl(step)
        assert result.returncode == 0, (
            f"WSL 构建步骤失败: {step}\n{result.stderr}")
    launcher = (
        "#!/bin/bash\n"
        "conv() {\n"
        "  case \"$1\" in\n"
        "    /mnt/[a-z]/*) local d=${1#/mnt/}; d=${d%%/*};"
        " printf '%s:/%s' \"$(echo \"$d\" | tr 'a-z' 'A-Z')\" \"${1#/mnt/$d/}\";;\n"
        "    *) printf '%s' \"$1\";;\n"
        "  esac\n"
        "}\n"
        "args=()\n"
        "for a in \"$@\"; do [ -n \"$a\" ] && args+=(\"$(conv \"$a\")\"); done\n"
        f"exec {_to_wsl(python)} -c"
        " 'import sys; from camctl.cli import main; sys.exit(main(sys.argv[1:]))'"
        " \"${args[@]}\"\n"
    )
    write_launcher = _wsl(f"cat > {_WSL_LAUNCHER} <<'LAUNCHER'\n{launcher}LAUNCHER\nchmod +x {_WSL_LAUNCHER}")
    assert write_launcher.returncode == 0, write_launcher.stderr
    return _WSL_DEMO


class HostDemo:
    """真实 host-demo 主程序的子进程会话（终端命令协议）。"""

    def __init__(self, deployment: Deployment, demo_path: str) -> None:
        self.deployment = deployment
        self.log_path = deployment.root / "module.log"
        self._demo_path = demo_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["wsl.exe", "-e", self._demo_path,
             "--camctl", _WSL_LAUNCHER,
             "--ready", _to_wsl(self.deployment.ready),
             "--processing", _to_wsl(self.deployment.processing),
             "--log", _to_wsl(self.log_path),
             "--config", _to_wsl(self.deployment.config_path)],
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
        self.proc.stdin.write(f"submit {_to_wsl(plan)}\n")
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
        _wsl("pkill -f host-demo || true", timeout_s=60)
        self.proc = None


def _plan_body(name: str) -> dict:
    return {
        "name": name,
        "actions": [
            {
                "name": "sync",
                "type": "report_status",
                "scheduled_at": future_schedule(1),
                "params": {"scope": "full"},
            }
        ],
    }


def _deployment_with_client(tmp_path: Path) -> tuple[Deployment, Path]:
    """初始化无设备部署，并把 describe 能力装入客户端存储。"""
    deployment = Deployment(tmp_path, devices=False)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    described = deployment.camctl(
        "describe", "--config", str(deployment.config_path))
    assert described.exit_code == 0, described.stderr
    assert json.loads(described.stdout) == {"devices": []}
    client_store = deployment.root / "client-store"
    client_store.mkdir()
    (client_store / "device-capabilities.json").write_text(
        described.stdout, encoding="utf-8")
    return deployment, deployment.state_db


def _published_report(deployment: Deployment, demo: HostDemo,
                      plan_path: Path) -> tuple[int, str]:
    """递交计划并等待报告发布，返回（报告 id、ready 文件名）。"""
    demo.submit(plan_path)
    ready_name = _await(
        lambda: glob.glob("status-report-*.json",
                          root_dir=str(deployment.ready)) or None,
        timeout_s=120, message="报告未发布到 ready")[0]
    _await(lambda: (
        _query(deployment.state_db,
               "SELECT status FROM actions WHERE name = 'sync'"),
        _query(deployment.state_db, "SELECT status FROM plans"),
    ) == ([(3,)], [(3,)]) or None,
        timeout_s=120, message="同步动作与计划未到终态")
    rows = _query(
        deployment.state_db,
        "SELECT id FROM reports WHERE status = 4 ORDER BY id")
    assert rows, "没有已发布报告"
    return rows[-1][0], ready_name


def test_report_import_ack_roundtrip(tmp_path: Path, host_demo: str) -> None:
    """无设备闭环：导出→submit/run→发布→领取→导入→ACK 吸收。

    请求身份由客户端分配并在主机侧保持；报告经 C 领取移动位置，
    客户端按原字节核验保存；下一份导出携带的 ACK 被受理接口吸收
    并推进主机累计确认。
    """
    deployment, state_db = _deployment_with_client(tmp_path)

    # 客户端真实导出：能力校验+整数请求身份分配；首份无 ACK。
    plan_path, receipt = deployment.export_plan_with_client(_plan_body("首份同步"))
    request_id = receipt["request_id"]
    assert receipt["has_ack"] is False

    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        report_id, ready_name = _published_report(deployment, demo, plan_path)
        size_sha = _query(
            state_db, "SELECT size_bytes, sha256 FROM reports WHERE id = ?",
            (report_id,))[0]
        assert str(report_id) == json.loads(
            (deployment.ready / ready_name).read_text("utf-8")
        )["report_id"]

        demo.claim()
        # 领取后位置：ready 撤空、processing 收到同一文件（原字节）。
        _await(lambda: glob.glob("status-report-*",
                                  root_dir=str(deployment.processing)) or None,
               timeout_s=30, message="领取未移动报告到 processing")
        assert not glob.glob("status-report-*",
                             root_dir=str(deployment.ready))
        claimed = deployment.processing / ready_name
        content_bytes = claimed.read_bytes()
        assert len(content_bytes) == size_sha[0]
        assert hashlib.sha256(content_bytes).hexdigest() == size_sha[1]

        # 客户端真实导入：原字节核验、可靠保存并形成 ACK 依据。
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [str(report_id)]
        assert saved["ack_id"] == str(report_id)

        # 第二份导出自动携带 ACK；经主程序递交后被受理接口吸收。
        second_plan, second_receipt = deployment.export_plan_with_client(
            _plan_body("第二份同步"))
        assert second_receipt["has_ack"] is True
        assert second_receipt["last_report_id"] == str(report_id)
        demo.submit(second_plan)
        _await(lambda: _query(
            state_db,
            "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
        ) == [(report_id, _query(
            state_db, "SELECT to_wm FROM reports WHERE id = ?", (report_id,))[0][0])]
        or None, timeout_s=120, message="ACK 未被吸收")

        # 主程序侧事实：两次 submit 与领取都有模块日志记录。
        log = demo.module_log()
        assert f"submit accepted path={_to_wsl(plan_path)}" in log
        assert "claim" in log
    finally:
        demo.stop()


def test_invalid_body_with_valid_ack_absorbs_ack_only(
        tmp_path: Path, host_demo: str) -> None:
    """非法正文但有效 ACK：正文被拒、不建计划，ACK 仍被吸收。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("同步前序"))
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        report_id, _ = _published_report(deployment, demo, plan_path)
        plans_before = _query(state_db, "SELECT COUNT(*) FROM plans")[0][0]
        # 正文缺少名称与动作：受理必须拒绝；顶层 ACK 引用已发布报告。
        invalid = deployment.root / "invalid-with-ack.json"
        invalid.write_text(json.dumps({
            "request_id": "414141",
            "last_report_id": str(report_id),
        }, ensure_ascii=False), encoding="utf-8")
        demo.submit(invalid)
        _await(lambda: _query(
            state_db,
            "SELECT acknowledged_report_id FROM runtime_state"
        ) == [(report_id,)] or None,
            timeout_s=120, message="合法 ACK 未被吸收")
        # 正文被拒绝：不产生新计划，也不产生新动作。
        assert _query(state_db, "SELECT COUNT(*) FROM plans")[0][0] \
            == plans_before
        assert not _query(
            state_db, "SELECT id FROM plans WHERE request_id = 414141")
    finally:
        demo.stop()


def test_resubmitted_request_keeps_single_identity(
        tmp_path: Path, host_demo: str) -> None:
    """同一请求重送：计划与动作身份不重复，重送不产生新事实。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, receipt = deployment.export_plan_with_client(_plan_body("重送同步"))
    request_id = int(receipt["request_id"])
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        demo.submit(plan_path)
        _await(lambda: _query(
            state_db, "SELECT id FROM plans WHERE request_id = ?",
            (request_id,)) or None,
            timeout_s=120, message="首次递交未受理")
        demo.submit(plan_path)
        # 会话推进到计划终态后重送：身份与事实数量保持一次受理。
        _await(lambda: _query(
            state_db, "SELECT status FROM plans WHERE request_id = ?",
            (request_id,)) == [(3,)] or None,
            timeout_s=120, message="计划未到终态")
        assert _query(
            state_db, "SELECT COUNT(*) FROM plans WHERE request_id = ?",
            (request_id,)) == [(1,)]
        assert _query(
            state_db, "SELECT COUNT(*) FROM actions WHERE plan_id ="
            " (SELECT id FROM plans WHERE request_id = ?)",
            (request_id,)) == [(1,)]
    finally:
        demo.stop()


def test_report_generation_failure_preserves_responsibility(
        tmp_path: Path) -> None:
    """普通生成失败：失败责任保留、同步动作不虚构终态、会话按
    report_error 退出；下一会话首轮重试并发布同一报告。

    会话边界由直接 CLI 驱动：重试语义要求新会话没有可冻结的新工
    作，C 主程序的受管通道只有 submit/claim，无法表达空 run。
    """
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("生成失败重试"))

    # staging 的报告子目录被普通文件占据：生成子进程无法写出报告文
    # 件，构成与状态库无关的普通生成失败。
    reports_staging = deployment.staging / "reports"
    reports_staging.write_text("", encoding="utf-8")

    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path))
    assert submitted.exit_code == 0, submitted.stderr
    failing = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    # 会话不为等待报告重试持续运行：按报告失败退出并携带机器原因。
    assert failing.exit_code == 1
    failure = json.loads(failing.stdout)
    assert failure["kind"] == "error"
    assert failure["body"]["reason"] == "report_error"

    # 同步动作与计划保留未完成事实（不虚构为成功或业务失败）；报告
    # 保留失败责任且从未取得确定字节；ready 不出现报告文件。
    assert _query(state_db, "SELECT status FROM plans") == [(2,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(2,)]
    rows = _query(
        state_db,
        "SELECT id, status, size_bytes, sha256, last_error_json FROM reports")
    assert len(rows) == 1
    report_id, status, size, sha, error = rows[0]
    assert status == 5
    assert (size, sha) == (None, None)
    assert "生成失败" in json.loads(error)["error"]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(None,)]
    assert not glob.glob("status-report-*", root_dir=str(deployment.ready))

    # 故障消除后：新的 run 会话在首轮重试此前失败的报告并发布；同
    # 步动作以该报告本地完成，计划随后到终态，覆盖这些变化的下一
    # 份报告在同会话发布并按生命周期撤下旧文件。
    reports_staging.unlink()
    retrying = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert retrying.exit_code == 0, retrying.stderr
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(3,)]
    assert _query(state_db, "SELECT status FROM reports") == [(4,), (4,)]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(report_id,)]
    names = glob.glob("status-report-*.json", root_dir=str(deployment.ready))
    assert len(names) == 1
    latest = json.loads((deployment.ready / names[0]).read_text("utf-8"))
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [latest["report_id"]]


def test_report_save_failure_retry_keeps_determined_bytes(
        tmp_path: Path) -> None:
    """报告保存失败重试：字节已登记的失败报告由下一会话重建发布，
    重建字节与首次确定字节保持一致。"""
    deployment, state_db = _deployment_with_client(tmp_path)
    plan_path, _ = deployment.export_plan_with_client(_plan_body("保存失败重试"))

    # ready 位置被普通文件占据：报告字节已生成并登记，交接无法进行。
    shutil.rmtree(deployment.ready)
    deployment.ready.write_text("", encoding="utf-8")

    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path))
    assert submitted.exit_code == 0, submitted.stderr
    failing = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert failing.exit_code == 1
    failure = json.loads(failing.stdout)
    assert failure["kind"] == "error"
    assert failure["body"]["reason"] == "report_error"

    # 同步动作与计划保留未完成事实；报告保留保存失败责任，首次生成
    # 已确定的字节事实不被失败抹除。
    assert _query(state_db, "SELECT status FROM plans") == [(2,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(2,)]
    rows = _query(
        state_db,
        "SELECT id, status, size_bytes, sha256, last_error_json FROM reports")
    assert len(rows) == 1
    report_id, status, determined, sha, error = rows[0]
    assert status == 5
    assert determined is not None and sha is not None
    assert "发布失败" in json.loads(error)["error"]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(None,)]

    # 恢复 ready 后：下一会话首轮重建同一报告并发布；重建字节与首
    # 次确定字节不一致会在字节登记事务被拒绝，发布不可能完成。
    deployment.ready.unlink()
    deployment.ready.mkdir()
    retrying = deployment.camctl(
        "run", "--config", str(deployment.config_path))
    assert retrying.exit_code == 0, retrying.stderr
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'sync'") == [(3,)]
    assert _query(
        state_db, "SELECT status, size_bytes, sha256 FROM reports WHERE id = ?",
        (report_id,)) == [(4, determined, sha)]
    assert _query(state_db, "SELECT status FROM reports") == [(4,), (4,)]
    assert _query(
        state_db, "SELECT local_report_id FROM state_syncs") == [(report_id,)]
    names = glob.glob("status-report-*.json", root_dir=str(deployment.ready))
    assert len(names) == 1
    latest = json.loads((deployment.ready / names[0]).read_text("utf-8"))
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [latest["report_id"]]
