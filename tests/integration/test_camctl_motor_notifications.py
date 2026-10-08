"""真实客户端、camctl、SQLite 和 C host 的电机通知业务链。

C 主程序只通过公开 int position 回调记录开始和返回，不解析通知
JSON，也不声明设备到位。真实相机协作使用既有驱动契约替身；电机
通知、受理、持久化、报告、领取及客户端导入均使用生产路径。
"""
from __future__ import annotations

import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _wsl_host_demo import query_state
from camctl_fixtures import Deployment, future_schedule, photo_file, stub_driver_spec

pytestmark = pytest.mark.skipif(os.name != "posix", reason="真实 C host 使用 POSIX 管道")
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def motor_host_driver(host_demo: str, tmp_path_factory) -> Path:
    """从本次 host 构建的静态库链接只使用公开接口的 C 主程序。"""
    binary = tmp_path_factory.mktemp("motor-host") / "motor-host-driver"
    command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
               "-I", str(_REPO / "apps/host-demo/include"),
               str(_REPO / "tests/integration/motor_host_driver.c"),
               str(Path(host_demo).parent / "libcamctl_host.a"),
               "-pthread", "-o", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return binary


def _init(deployment: Deployment, spec: dict | None = None) -> Path:
    result = deployment.camctl("init", "--config", str(deployment.config_path))
    assert result.exit_code == 0, result.stderr
    if spec is not None:
        deployment.install_client_capabilities(spec)
    else:
        described = deployment.camctl("describe", "--config", str(deployment.config_path))
        assert described.exit_code == 0, described.stderr
        store = deployment.root / "client-store"
        store.mkdir()
        (store / "device-capabilities.json").write_text(described.stdout, encoding="utf-8")
    launcher = deployment.root / "motor-camctl-launcher"
    if spec is None:
        entry = "from camctl.cli import main; raise SystemExit(main())"
        body = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(entry)} \"$@\"\n"
    else:
        bridge = _REPO / "tests/integration/_camctl_stub_entry.py"
        body = (f"export CAMCTL_TEST_DRIVER_SPEC={shlex.quote(str(deployment.driver_spec))}\n"
                f"export CAMCTL_TEST_STATE_DB={shlex.quote(str(deployment.state_db))}\n"
                f"exec {shlex.quote(sys.executable)} {shlex.quote(str(bridge))} \"$@\"\n")
    launcher.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def _motor_action(name: str, position: int, scheduled_at: str) -> dict:
    return {"name": name, "type": "motor_control", "scheduled_at": scheduled_at,
            "params": {"position": position}, "policy": {"max_delay_ms": 60000}}


def _fault_launcher(deployment: Deployment, launcher: Path, phase: str) -> None:
    """CLI 的真实持久化／管道操作完成后只中断一次，恢复仍走原生产入口。"""
    entry = Path(__file__).with_name("_motor_fault_entry.py")
    environment = {
        "CAMCTL_MOTOR_FAULT": phase,
        "CAMCTL_MOTOR_FAULT_MARKER": str(deployment.root / "fault.marker"),
        "CAMCTL_MOTOR_WRITE_TRACE": str(deployment.root / "write.trace"),
    }
    exports = "".join(f"export {key}={shlex.quote(value)}\n"
                      for key, value in environment.items())
    launcher.write_text("#!/bin/sh\n" + exports
                        + f"exec {shlex.quote(sys.executable)} {shlex.quote(str(entry))} \"$@\"\n",
                        encoding="utf-8")


def _motor_body(name: str, positions: list[int]) -> dict:
    scheduled = future_schedule(-1)
    return {"name": name, "actions": [
        _motor_action(f"{name}-{index}", position, scheduled)
        for index, position in enumerate(positions)]}


class MotorHost:
    """真实 C 驱动的观察会话；以事实条件等待，不按固定延时推进。"""
    def __init__(self, deployment: Deployment, binary: Path, launcher: Path,
                 initial: Path, *, hold: bool = False) -> None:
        self.deployment = deployment
        self.log = deployment.root / "motor-host.log"
        self.events: list[dict] = []
        self.buffer = b""
        command = [str(binary), str(launcher), str(deployment.config_path),
                   str(deployment.ready), str(deployment.processing), str(self.log),
                   str(initial)]
        if hold:
            command.append("hold")
        environment = {**os.environ, "CAMCTL_TEST_TRACE_DISPATCH": "1",
                       "CAMCTL_TEST_TRACE_CALLS": "1"}
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, env=environment)
        self.wait(lambda: any(e["event"] == "initialized" for e in self.events),
                  "C 主程序未初始化")

    def drain(self) -> None:
        while select.select([self.proc.stdout], [], [], 0)[0]:
            data = os.read(self.proc.stdout.fileno(), 4096)
            if not data:
                break
            self.buffer += data
            assert len(self.buffer) <= 65536, "C 观察输出超过测试缓冲上限"
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.events.append(json.loads(line))

    def diagnostics(self) -> str:
        text = self.log.read_text(errors="replace") if self.log.exists() else ""
        return f"events={self.events!r}\nhost log:\n{text[-16384:]}"

    def wait(self, predicate, message: str, timeout: float = 60):
        deadline = time.monotonic() + timeout
        while True:
            self.drain()
            value = predicate()
            if value:
                return value
            assert self.proc.poll() is None, f"{message}: host 提前退出\n{self.diagnostics()}"
            assert time.monotonic() < deadline, f"{message}\n{self.diagnostics()}"
            time.sleep(.01)

    def command(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def submit(self, plan: Path) -> None:
        before = len([e for e in self.events if e["event"] == "submit"])
        self.command(f"submit {plan}")
        self.wait(lambda: len([e for e in self.events if e["event"] == "submit"]) > before,
                  "C 主程序未接收递交路径")
        assert [e for e in self.events if e["event"] == "submit"][-1]["result"] == 0

    def callbacks(self) -> list[dict]:
        return [e for e in self.events if e["event"] == "callback"]

    def run_completions(self) -> int:
        text = self.log.read_text(errors="replace") if self.log.exists() else ""
        return len(re.findall(r"command=run pid=\d+ result=success", text))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.command("release")
            # 只终止本测试主程序仍然持有的具体子进程组。
            groups = set()
            root = Path(f"/proc/{self.proc.pid}/task")
            for task in root.iterdir() if root.exists() else []:
                try:
                    children = (task / "children").read_text().split()
                except FileNotFoundError:
                    continue
                for child in children:
                    pid = int(child)
                    try:
                        if os.getpgid(pid) == pid:
                            groups.add(pid)
                    except ProcessLookupError:
                        continue
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3
            while any(Path(f"/proc/{pid}").exists() for pid in groups) and time.monotonic() < deadline:
                time.sleep(.01)
            self.proc.terminate()
        self.proc.communicate(timeout=10)


def _report(directory: Path, statuses: dict[str, str]) -> dict | None:
    for path in directory.glob("status-report-*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        actions = {action["name"]: action for plan in value["plans"] for action in plan["actions"]}
        if all(actions.get(name, {}).get("status") == status for name, status in statuses.items()):
            return value
    return None


def _assert_sent(deployment: Deployment, count: int) -> None:
    rows = query_state(deployment.state_db,
                       "SELECT outcome,intent_at,written_bytes,errno,finished_at"
                       " FROM motor_notifications ORDER BY id")
    assert len(rows) == count
    for outcome, intent_at, written_bytes, error, finished_at in rows:
        assert outcome == 3
        assert intent_at is not None and finished_at is not None
        assert written_bytes > 0 and error is None


def _assert_motor_report(report: dict, positions: list[int]) -> None:
    motors = [action for plan in report["plans"] for action in plan["actions"]
              if action["type"] == "motor_control"]
    assert [action["input_params"]["position"] for action in motors] == positions
    for action in motors:
        assert action["status"] == "succeeded"
        assert not ({"device_id", "device_execution", "effective_params", "outputs",
                     "deliveries", "result"} & action.keys())


def _claim_import(host: MotorHost, expected: dict[str, str]) -> tuple[dict, dict]:
    deployment = host.deployment
    report = host.wait(lambda: _report(deployment.ready, expected), "最终报告未发布")
    host.command("claim")
    host.wait(lambda: _report(deployment.processing, expected), "报告未被真实 C claim 领取")
    saved = deployment.import_reports_with_client(deployment.processing)
    assert report["report_id"] in saved["saved_report_ids"]
    assert saved["ack_id"] == report["report_id"]
    return report, saved


def test_client_export_host_callback_report_import_boundaries(tmp_path, motor_host_driver):
    """客户端导出四个边界位置，经真实 C host 接收并导入报告及累计确认。"""
    deployment = Deployment(tmp_path, devices=False)
    launcher = _init(deployment)
    positions = [-2147483648, -1, 0, 2147483647]
    body = _motor_body("边界", positions)
    path, receipt = deployment.export_plan_with_client(body)
    assert not receipt["has_ack"]
    host = MotorHost(deployment, motor_host_driver, launcher, path)
    try:
        host.wait(lambda: len(host.callbacks()) == len(positions), "边界位置未全部进入 C 回调")
        callbacks = host.callbacks()
        assert [e["position"] for e in callbacks] == positions
        assert [e["sequence"] for e in callbacks] == [1, 2, 3, 4]
        assert all(set(e) == {"event", "sequence", "position"} for e in callbacks)
        host.wait(lambda: host.run_completions() >= 1, "首个真实 CLI 未完成回收与解析")
        _assert_sent(deployment, 4)
        report, _ = _claim_import(host, {action["name"]: "succeeded" for action in body["actions"]})
        _assert_motor_report(report, positions)
        ack_body = {"name": "累计确认", "actions": [{"name": "同步", "type": "report_status",
                    "scheduled_at": future_schedule(-1), "params": {"scope": "full"}}]}
        ack_path, ack = deployment.export_plan_with_client(ack_body)
        assert ack["last_report_id"] == report["report_id"]
        host.submit(ack_path)
        host.wait(lambda: query_state(deployment.state_db,
                  "SELECT acknowledged_report_id FROM runtime_state") == [(int(report["report_id"]),)],
                  "客户端累计确认未经 C submit 吸收")
    finally:
        host.stop()


def test_slow_callback_allows_submit_new_run_and_fifo(tmp_path, motor_host_driver):
    """回调未返回时发送结果、报告、并行递交和下一真实 run 继续，旧新队列共同 FIFO。"""
    deployment = Deployment(tmp_path, devices=False)
    launcher = _init(deployment)
    first = _motor_body("旧会话", [-1, 0, 1])
    path, _ = deployment.export_plan_with_client(first)
    host = MotorHost(deployment, motor_host_driver, launcher, path, hold=True)
    try:
        host.wait(lambda: len(host.callbacks()) == 1, "首个慢回调未进入门闩")
        host.wait(lambda: host.run_completions() >= 1, "慢回调阻止首个 CLI 完成")
        _assert_sent(deployment, 3)
        second = _motor_body("新会话", [100, 2147483647])
        second_path, _ = deployment.export_plan_with_client(second)
        host.submit(second_path)
        host.wait(lambda: host.run_completions() >= 2, "慢回调阻止递交触发下一 run")
        _assert_sent(deployment, 5)
        expected = {a["name"]: "succeeded" for body in (first, second) for a in body["actions"]}
        report, _ = _claim_import(host, expected)
        _assert_motor_report(report, [-1, 0, 1, 100, 2147483647])
        assert len(host.callbacks()) == 1
        assert not any(e["event"] == "callback_returned" for e in host.events)
        host.command("release")
        host.wait(lambda: len(host.callbacks()) == 5, "慢回调释放后旧新通知未排空")
        assert [e["position"] for e in host.callbacks()] == [-1, 0, 1, 100, 2147483647]
        assert [e["sequence"] for e in host.callbacks()] == [1, 2, 3, 4, 5]
    finally:
        host.stop()


def test_motor_and_camera_share_cli_without_device_result_confusion(tmp_path, motor_host_driver):
    """电机回调阻塞不妨碍相机生产流程，报告保持各自动作的完成含义。"""
    deployment = Deployment(tmp_path)
    spec = stub_driver_spec({"2": [photo_file("motor-camera-shot")]})
    launcher = _init(deployment, spec)
    scheduled = future_schedule(-1)
    body = {"name": "电机与相机共存", "actions": [
        _motor_action("电机", -12, scheduled),
        {"name": "照片", "type": "camera_take_photo", "device_id": "cam-1",
         "scheduled_at": scheduled, "params": {"type": "single_shot"},
         "policy": {"max_delay_ms": 60000}}]}
    path, _ = deployment.export_plan_with_client(body)
    host = MotorHost(deployment, motor_host_driver, launcher, path, hold=True)
    try:
        host.wait(lambda: len(host.callbacks()) == 1, "电机回调未进入门闩")
        host.wait(lambda: query_state(deployment.state_db,
                  "SELECT name,status FROM actions ORDER BY id") == [("电机", 3), ("照片", 3)],
                  "电机回调阻塞了相机运行或相机结果未保存")
        _assert_sent(deployment, 1)
        assert query_state(deployment.state_db,
                           "SELECT source_action_id FROM outputs") == [(2,)]
        report, _ = _claim_import(host, {"电机": "succeeded", "照片": "succeeded"})
        _assert_motor_report(report, [-12])
        assert not any(e["event"] == "callback_returned" for e in host.events)
        host.command("release")
        host.wait(lambda: any(e["event"] == "callback_returned" for e in host.events),
                  "电机回调未返回")
    finally:
        host.stop()


@pytest.mark.parametrize("phase,writes", [("after_intent", 0), ("after_write", 1)])
def test_host_restarts_interrupted_cli_without_repeating_notification(
        tmp_path, motor_host_driver, phase, writes):
    """真实 host 回收异常 CLI 并自动重启，保存的意图阻止再次发出同一通知。"""
    deployment = Deployment(tmp_path, devices=False)
    launcher = _init(deployment)
    _fault_launcher(deployment, launcher, phase)
    body = _motor_body("异常恢复", [-2147483648])
    path, _ = deployment.export_plan_with_client(body)
    host = MotorHost(deployment, motor_host_driver, launcher, path)
    try:
        host.wait(lambda: "result=abnormal" in host.log.read_text(), "异常 CLI 未被真实 host 回收")
        host.wait(lambda: host.run_completions() >= 1, "host 未自动重启真实 CLI 并完成恢复")
        text = host.log.read_text()
        interrupted = re.search(r"command=run pid=(\d+) result=abnormal .*exit=91", text)
        assert interrupted, text
        assert not Path(f"/proc/{interrupted.group(1)}").exists(), "异常具体 PID 尚未回收"
        assert re.search(r"command=run operation=spawn plan=\(none\) automatic=1", text)
        assert (deployment.root / "fault.marker").read_text() == phase
        assert query_state(deployment.state_db,
                           "SELECT status,error_code FROM actions WHERE type=8") == [(4, 29)]
        assert query_state(deployment.state_db,
                           "SELECT outcome,written_bytes FROM motor_notifications") == [(5, None)]
        if writes:
            host.wait(lambda: len(host.callbacks()) == writes, "写入完成的通知未进入真实 C 回调")
        assert len(host.callbacks()) == writes
        assert [e["position"] for e in host.callbacks()] == ([-2147483648] if writes else [])
        trace = deployment.root / "write.trace"
        assert len((trace.read_bytes() if trace.exists() else b"").splitlines()) == writes
        report, _ = _claim_import(host, {body["actions"][0]["name"]: "failed"})
        motor = [a for p in report["plans"] for a in p["actions"]
                 if a["type"] == "motor_control"]
        assert len(motor) == 1
        assert motor[0]["error"]["code"] == "motor_notification_unconfirmed"
        assert motor[0]["input_params"] == {"position": -2147483648}
        assert not ({"device_id", "device_execution", "effective_params", "outputs",
                     "deliveries", "result"} & motor[0].keys())
        # 恢复已完成后同一客户端请求再经真实 C submit 递交，仍不建立第二次发送。
        host.submit(path)
        host.wait(lambda: "command=submit" in host.log.read_text()
                  and re.search(r"command=submit pid=\d+ result=success", host.log.read_text()),
                  "恢复后的重复请求未完成真实 CLI 受理")
        host.drain()
        assert len(host.callbacks()) == writes
        assert len((trace.read_bytes() if trace.exists() else b"").splitlines()) == writes
        assert query_state(deployment.state_db,
                           "SELECT status,error_code FROM actions WHERE type=8") == [(4, 29)]
    finally:
        host.stop()
