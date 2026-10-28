"""Hasselblad 安装能力、真实网页及文件上传的跨组件接缝。

网页、正常拍摄与恢复用例消费同一组安装入口；定向网页与上传
用例分别证明实际预设重开/下载和真实 Files.upload，不连接设备。
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from test_hasselblad_photo_parameters import InstalledCamctl, installed_camctl
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl_fixtures import Deployment
from _client_session import ClientSession


REPO = Path(__file__).resolve().parents[2]


def _client(package: InstalledCamctl, *args: str, output: Path) -> dict:
    """在已准备的准确候选副本运行真实客户端，不使用混合依赖。"""
    client_root = Path(os.environ["CAMCTL_HB_TEST_CLIENT_ROOT"]).resolve()
    assert client_root != REPO and REPO not in client_root.parents
    driver = client_root / "tests/integration/client_driver.ts"
    # 产品源码/构建固定为本候选；测试接缝随当前修改同步。
    shutil.copy2(REPO / "tests/integration/client_driver.ts", driver)
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = package.run([
        "node", "--import", "tsx", driver, *args, output,
    ], cwd=client_root / "apps/client", timeout=180)
    assert completed.returncode == 0
    return json.loads(output.read_text(), parse_float=Decimal)


def _token(text: str, path: tuple[str | int, ...]) -> str:
    value = json.loads(text)
    for part in path:
        value = value[part]
    assert isinstance(value, str), (path, value)
    return value


@pytest.mark.parametrize("shutter", ("1/1500", "1/3000", "1/4000"))
def test_installed_hasselblad_browser_preset_keeps_precise_shutter(
    tmp_path: Path, installed_camctl: InstalledCamctl, shutter: str,
):
    package = installed_camctl
    store = tmp_path / "client-store"
    store.mkdir()
    description = package.cli("describe", "--config", str(package.config_path)).stdout
    (store / "device-capabilities.json").write_text(description)
    params_text = (
        '{"type":"hasselblad_photo","capture_format":"jpeg",'
        '"exposure":{"mode":"M","iso":100,"aperture":8,"shutter_s":' + json.dumps(shutter) + '},'
        '"white_balance":{"mode":"manual","temperature_k":5500},'
        '"focus":{"mode":"MF","relative_move":5}}'
    )
    input_path = tmp_path / "browser-input.json"
    refresh_schedule = shutter == "1/3000"
    before_browser = datetime.now(timezone.utc).replace(microsecond=0)
    scheduled_at = (before_browser + timedelta(minutes=-2 if refresh_schedule else 5)).strftime("%Y-%m-%d %H:%M:%S")
    input_path.write_text(json.dumps({
        "device_id": "hb-host", "action_type": "camera_take_photo",
        "action_name": "capture", "case_name": f"网页精确快门-{shutter}",
        "params_text": params_text,
        "scheduled_at": scheduled_at,
        "refresh_scheduled_at": refresh_schedule,
    }, ensure_ascii=False))
    plan = tmp_path / "downloaded-plan.json"
    receipt = _client(package, "browser-photo-export", str(store), str(input_path), str(plan),
                      output=tmp_path / "browser-receipt.json")
    assert receipt["source_draft_id"] != receipt["applied_draft_id"]
    assert receipt["preset_id"] == receipt["reopened_preset_id"]
    expected = json.loads(params_text, parse_float=Decimal)
    plan_path = ("actions", 0, "params")
    for field, path in (
        ("source_draft_text", plan_path), ("reopened_draft_text", plan_path),
        ("applied_draft_text", plan_path), ("request_text", plan_path),
        ("preset_params_text", ()), ("reopened_preset_params_text", ()),
    ):
        document = json.loads(receipt[field], parse_float=Decimal)
        for part in path:
            document = document[part]
        assert document == expected, field
        assert _token(receipt[field], (*path, "exposure", "shutter_s")) == shutter, field
    assert plan.read_text() == receipt["request_text"]
    request = json.loads(plan.read_text(), parse_float=Decimal)
    assert len(request["actions"]) == 1
    source_action = json.loads(receipt["source_draft_text"], parse_float=Decimal)["actions"][0]
    reopened_action = json.loads(receipt["reopened_draft_text"], parse_float=Decimal)["actions"][0]
    applied_action = json.loads(receipt["applied_draft_text"], parse_float=Decimal)["actions"][0]
    assert source_action == reopened_action
    assert source_action["scheduled_at"] == scheduled_at
    assert source_action["policy"] == {"max_delay_ms": 60000}
    assert applied_action == {**source_action, "scheduled_at": request["actions"][0]["scheduled_at"]}
    assert request["actions"][0] == applied_action
    if refresh_schedule:
        assert request["actions"][0]["scheduled_at"] != scheduled_at
        assert datetime.strptime(request["actions"][0]["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) >= before_browser
    else:
        assert request["actions"][0]["scheduled_at"] == scheduled_at
    assert str(request["request_id"]) == receipt["request_id"]
    assert receipt["capabilities_sha256"] == hashlib.sha256(description.encode()).hexdigest()
    assert receipt["server_open_count"] == 2
    assert receipt["preset_http_status"] == 201
    assert _token(receipt["preset_http_text"], ("params", "exposure", "shutter_s")) == shutter
    assert not package.device_marker.exists()


def _upload_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    """从权威正常报告派生小型图片映射，仅用于上传顺序接缝。"""
    report_path, = (REPO / "protocol/examples/client-protocol/01-success").glob("status-report-*.json")
    report = json.loads(report_path.read_text())
    content = b"\xff\xd8h8b-files-upload-photo\xff\xd9"
    digest = hashlib.sha256(content).hexdigest()
    capture, obtain = report["plans"][0]["actions"]
    capture["type"] = "camera_take_photo"
    capture.pop("result")
    capture["input_params"] = capture["effective_params"] = {"type": "single_shot"}
    output = capture["outputs"][0]
    output.update(size=len(content), media_type="image/jpeg", original_name="shot.jpg")
    output["checksum"] = {"status": "available", "sha256": digest}
    delivery = obtain["deliveries"][0]
    delivery.update(file_name="1.jpg", display_name="图片.jpg", size=len(content), sha256=digest)
    media = tmp_path / delivery["file_name"]
    media.write_bytes(content)
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode()
    target = tmp_path / f"status-report-{report['report_id']}-{hashlib.sha256(encoded).hexdigest()}.json"
    target.write_bytes(encoded)
    return media, target, delivery


@pytest.mark.parametrize("media_first", (False, True))
def test_installed_hasselblad_files_upload_preserves_arrival_order(
    tmp_path: Path, installed_camctl: InstalledCamctl, media_first: bool,
):
    media, report, delivery = _upload_fixture(tmp_path)
    store = tmp_path / "upload-store"

    def upload(path: Path, kind: str, label: str):
        manifest = tmp_path / f"{label}-manifest.json"
        manifest.write_text(json.dumps([{"path": str(path), "kind": kind}]))
        return _client(installed_camctl, "upload-files", str(store), str(manifest),
                       output=tmp_path / f"{label}-receipt.json")

    first = upload(media if media_first else report, "media" if media_first else "report", "first")
    if media_first:
        assert first["saved_report_ids"] == []
        assert first["media"][0]["status"] == "waiting_report"
        original_id = first["media"][0]["id"]
    else:
        assert first["saved_report_ids"] == ["1"]
        assert first["media"] == []
    final = upload(report if media_first else media, "report" if media_first else "media", "second")
    assert final["worker_error"] is None
    assert final["saved_report_ids"] == ["1"]
    assert final["coverage"] == 20
    assert final["ack_id"] == "1"
    assert [row["kind"] for row in final["imports"]].count("report") == 1
    assert [row["kind"] for row in final["imports"]].count("media") == 1
    row, = final["media"]
    assert row["status"] == "verified"
    assert row["fileName"] == media.name
    assert row["sha256"] == delivery["sha256"]
    assert row["size"] == delivery["size"]
    if media_first:
        assert row["id"] == original_id
        assert row["path"] == first["media"][0]["path"]
    assert Path(row["path"]).read_bytes() == media.read_bytes()
    assert final["snapshot"]["plans"][0]["actions"][1]["deliveries"][0] == delivery
    ack_body = tmp_path / "ack-body.json"
    ack_body.write_text(json.dumps({"name": "网页累计确认", "actions": [{
        "name": "ack", "type": "report_status", "params": {"scope": "full"},
    }]}))
    ack_plan = tmp_path / "ack-plan.json"
    ack = _client(installed_camctl, "browser-plan-export", str(store), str(ack_body), str(ack_plan),
                  output=tmp_path / "ack-receipt.json")
    assert ack["last_report_id"] == "1"
    assert ack_plan.read_text() == ack["request_text"]
    assert json.loads(ack["request_text"])["last_report_id"] == "1"


@dataclass(frozen=True)
class PhotoCase:
    capture_format: str
    source: str
    shutter: str
    focus_override: tuple[str, int | None] | None = None

    @property
    def name(self):
        shutter_name = self.shutter.replace("/", "-")
        return f"{self.capture_format}-{self.source}-{shutter_name}"

    @property
    def params_text(self):
        exposure = ('"mode":"S","iso":100' if self.shutter == "1/3000"
                    else '"mode":"M","iso":100,"aperture":8')
        wb, focus = {
            "1/1500": ('{"mode":"manual","temperature_k":5500}', '{"mode":"MF","relative_move":5}'),
            "1/3000": ('{"mode":"auto"}', '{"mode":"AF-S"}'),
            "1/4000": ('{"mode":"daylight"}', '{"mode":"MF"}'),
        }[self.shutter]
        if self.focus_override is not None:
            assert self.focus_override in (("MF", 5), ("MF", None))
            focus = '{"mode":"MF"' + (',"relative_move":5' if self.focus_override[1] == 5 else '') + '}'
        return ('{"type":"hasselblad_photo","capture_format":' + json.dumps(self.capture_format)
                + ',"exposure":{' + exposure + ',"shutter_s":' + json.dumps(self.shutter) + '},'
                + '"white_balance":' + wb + ',"focus":' + focus + '}')

    @property
    def settings_argv(self):
        # 独立手写代表期望，绝不调用被测 photo_start_commands。
        commands = [("set", "drive-mode", "single"), ("set", "capture-format", self.capture_format),
                    ("set", "exposure-mode", "S" if self.shutter == "1/3000" else "M"),
                    ("set", "iso", "100")]
        if self.shutter != "1/3000":
            commands.append(("set", "aperture", "8"))
        commands.append(("set", "shutter", self.shutter))
        if self.shutter == "1/1500":
            commands.extend((("set", "wb-mode", "manual"), ("set", "wb", "5500,0"),
                             ("set", "focus-mode", "MF"), ("focus", "5")))
        elif self.shutter == "1/3000":
            commands.extend((("set", "wb-mode", "auto"), ("set", "focus-mode", "AF-S"), ("autofocus",)))
        else:
            commands.extend((("set", "wb-mode", "daylight"), ("set", "focus-mode", "MF")))
        if self.focus_override is not None:
            assert self.focus_override in (("MF", 5), ("MF", None))
            commands = [item for item in commands if item[:2] != ("set", "focus-mode")
                        and item[0] not in ("focus", "autofocus")]
            commands.append(("set", "focus-mode", "MF"))
            if self.focus_override[1] == 5:
                commands.append(("focus", "5"))
        return tuple(commands)


NORMAL_CASES = tuple(PhotoCase(fmt, source, shutter)
                     for fmt in ("raw", "jpeg") for source in ("host", "device")
                     for shutter in ("1/1500", "1/3000", "1/4000"))
FULL_UI_CASE_NAMES = frozenset((
    "raw-host-1-1500", "raw-device-1-3000", "raw-host-1-4000",
    "jpeg-device-1-1500", "jpeg-host-1-3000", "jpeg-device-1-4000",
))


def _tool_source():
    path = REPO / "apps/camctl/tests/integration/bootstrap/test_distribution.py"
    spec = importlib.util.spec_from_file_location("h8b_distribution_helper", path)
    module = importlib.util.module_from_spec(spec)
    # dataclass 通过模块登记解析自身类型；这里只读导入现有同源 helper。
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rows(db: Path, sql: str, params=()):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def _history_observer():
    spec = importlib.util.spec_from_file_location(
        "h8b_history_observer", REPO / "tests/integration/_hasselblad_photo_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hasselblad_history_observer_rejects_middle_event_as_boundary():
    observer = _history_observer()
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("CREATE TABLE history_transactions(id,operation_key,first_event_id,last_event_id);"
                                 "INSERT INTO history_transactions VALUES(8,'original-key',20,22);")
        observer._require_boundary(connection, {"txn_id": 8, "last_event_id": 22})
        with pytest.raises(AssertionError, match="完整事务"):
            observer._require_boundary(connection, {"txn_id": 8, "last_event_id": 20})


@pytest.mark.parametrize("damage", ("missing-photo", "missing-entered", "wrong-activity", "wrong-role", "wrong-sha"))
def test_hasselblad_history_observer_rejects_incomplete_start(damage):
    observer = _history_observer()
    original = {"original_name": "host-copy.jpg", "size_bytes": 1,
                "sha256": hashlib.sha256(b"a").hexdigest(), "media_type": "image/jpeg"}
    preview = {**original, "original_name": "host-copy.jpg.thumbnail.jpg"}
    stdout = b"fixture"
    steps = [{"name": "capture", "local_exit_code": 0, "local_signal": None,
              "transport_error": None, "output_failure": None, "signal_failures": [],
              "stdout_size_bytes": len(stdout), "stdout_sha256": hashlib.sha256(stdout).hexdigest()}]
    raw = {"format_version": 1, "settlement": {"basis": "observed", "evidence": {
        "type": "operation_returned", "version": 1, "data": {}}}, "observations": [
        {"type": "photo_taken", "version": 1, "data": {"activity_id": "41", "original": original.copy(),
         "preview": preview.copy(), "steps": steps}},
        {"type": "hasselblad_start_entered", "version": 1, "data": {"activity_id": "41"}}],
        "diagnostics": {"version": 1, "call_limit": 14, "calls": [{"step_index": 1,
            "step_name": "capture", "stdout": {"state": "complete", "hex": stdout.hex()},
            "stderr": {"state": "complete", "hex": ""}}]}}
    expected = {"steps": ["capture"], "artifacts": {"original": original, "preview": preview}}
    attempt = {"status": 2, "effect_state": 3, "error_json": None, "attempt_no": 1}
    target = {"run_id": 17, "activity_id": 41, "responsibility_key": "start/29"}
    observer._assert_start_result(attempt, json.dumps(raw), target, expected)
    if damage == "missing-photo":
        raw["observations"].pop(0)
    elif damage == "missing-entered":
        raw["observations"].pop(1)
    elif damage == "wrong-activity":
        raw["observations"][1]["data"]["activity_id"] = "42"
    elif damage == "wrong-role":
        raw["observations"][0]["data"]["original"] = preview
    else:
        raw["observations"][0]["data"]["original"]["sha256"] = "0" * 64
    with pytest.raises((AssertionError, ValueError)):
        observer._assert_start_result(attempt, json.dumps(raw), target, expected)


class PhotoWorld:
    def __init__(self, root: Path, installed: InstalledCamctl, case: PhotoCase, host_demo: str):
        assert not root.resolve().is_relative_to(REPO)
        self.case = case
        self.deployment = Deployment(root, devices=False)
        self.package = InstalledCamctl(root, installed.python, installed.environment.copy())
        self.installed_environment = installed.environment.copy()
        self.root = root
        self.argv_log = root / "hb-cli-argv.jsonl"
        self.provenance = root / "processes.jsonl"
        self.tool = root / "hb_cli"
        helper = _tool_source()
        self.original_bytes = (helper._HASSELBLAD_ORIGINAL_BYTES if case.capture_format == "raw"
                               else helper._HASSELBLAD_JPEG_ORIGINAL_BYTES)
        self.preview_bytes = helper._HASSELBLAD_PREVIEW_BYTES
        if case.capture_format == "jpeg" and case.shutter == "1/4000":
            self.original_bytes = self.preview_bytes
        self.tool.write_text(helper._hasselblad_tool_source(
            self.argv_log, "H8B-SERIAL", installed_python=installed.python,
            expected_commands=case.settings_argv, provenance_log=self.provenance,
            jpeg_original_bytes=self.original_bytes if case.capture_format == "jpeg" else None))
        self.tool.chmod(0o700)
        config = self.deployment.config_path.read_text() + "\n".join((
            "\n[devices.hb-host]", 'kind = "camera"', 'driver = "hasselblad_cfv100c"',
            "[devices.hb-host.hasselblad]", f"executable = {json.dumps(str(self.tool))}",
            'serial = "H8B-SERIAL"', f'output_storage = "{case.source}"', ""))
        self.deployment.config_path.write_text(config)
        self.package.cli("init", "--config", str(self.deployment.config_path))
        self.store = root / "client-store"
        self.store.mkdir()
        description = self.package.cli("describe", "--config", str(self.deployment.config_path)).stdout
        (self.store / "device-capabilities.json").write_text(description)
        self.launcher = root / "installed-camctl"
        entry = root / "_hasselblad_photo_entry.py"
        shutil.copy2(REPO / "tests/integration/_hasselblad_photo_entry.py", entry)
        self.launcher.write_text(f"#!{installed.python}\nimport runpy\nrunpy.run_path({str(entry)!r}, run_name='__main__')\n")
        self.launcher.chmod(0o700)
        self.package.environment["CAMCTL_HB_TEST_ROOT"] = str(root)
        self.package.environment["CAMCTL_HB_TEST_INSTALLED_PYTHON"] = str(installed.python)
        self.host = HostDemo(self.deployment, host_demo, str(self.launcher))
        self.request_ids = {}
        self.report_checks = {}
        self.client_session = None

    def start(self):
        def fault_value(environment):
            return {"present": "CAMCTL_HB_TEST_FAULT" in environment,
                    "value": environment.get("CAMCTL_HB_TEST_FAULT")}
        (self.root / "environment-provenance.json").write_text(json.dumps({
            "test_process_pid": os.getpid(),
            "test_environment": fault_value(os.environ),
            "installed_environment": fault_value(self.installed_environment),
            "host_start_environment": fault_value(self.package.environment),
            "host_binary": str(self.host._demo_path),
            "host_binary_sha256": hashlib.sha256(Path(self.host._demo_path).read_bytes()).hexdigest(),
        }, indent=2))
        self.host.start(env=self.package.environment)

    def close(self):
        primary = sys.exception()
        errors = []
        try:
            self.host.stop()
        except BaseException as error:
            errors.append(error)
        if self.client_session is not None:
            try:
                self.client_session.close()
            except BaseException as error:
                errors.append(error)
        try:
            with closing(sqlite3.connect(f"file:{self.deployment.state_db}?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(self.root / "evidence-state.db")) as target:
                    source.backup(target)
        except BaseException as error:
            errors.append(error)
        if errors:
            raise BaseExceptionGroup("Hasselblad world操作或收场失败", ([primary] if primary else []) + errors)

    def client(self, command: str, *arguments: str, output: Path):
        if self.client_session is None:
            client_root = Path(os.environ["CAMCTL_HB_TEST_CLIENT_ROOT"]).resolve()
            assert client_root != REPO and REPO not in client_root.parents
            driver = client_root / "tests/integration/client_driver.ts"
            shutil.copy2(REPO / "tests/integration/client_driver.ts", driver)
            self.client_session = ClientSession(
                ["node", "--import", "tsx", str(driver)],
                cwd=client_root / "apps/client", env=self.package.environment, root=self.root,
            )
        return self.client_session.call(command, *arguments, output=output)

    def argv(self):
        return [json.loads(line) for line in self.argv_log.read_text().splitlines()]

    def export_photo(self, *, browser_prefix=False):
        input_path = self.root / "photo-input.json"
        photo_input = {
            "device_id": "hb-host", "action_type": "camera_take_photo", "action_name": "capture",
            "case_name": self.case.name, "params_text": self.case.params_text,
            "scheduled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "refresh_scheduled_at": True,
        }
        input_path.write_text(json.dumps(photo_input))
        plan = self.root / "photo-request.json"
        mode = "browser" if browser_prefix else "application"
        receipt = self.client(f"{mode}-photo-export", str(input_path), str(plan),
                              output=self.root / f"photo-{mode}.json")
        assert receipt["export_mode"] == mode
        assert plan.read_text() == receipt["request_text"]
        assert receipt["capabilities_sha256"] == hashlib.sha256(
            (self.store / "device-capabilities.json").read_bytes()).hexdigest()
        request = json.loads(receipt["request_text"], parse_float=Decimal)
        assert str(request["request_id"]) == receipt["request_id"]
        assert request["name"] == f"{self.case.name}-applied"
        assert len(request["actions"]) == 1
        refresh = receipt["schedule_refresh"]
        assert refresh["previous_scheduled_at"] == photo_input["scheduled_at"]
        assert refresh["scheduled_at"] == datetime.fromisoformat(
            refresh["selected_utc"]).strftime("%Y-%m-%d %H:%M:%S")
        expected = json.loads(self.case.params_text, parse_float=Decimal)
        assert request["actions"][0] == {
            "name": "capture", "type": "camera_take_photo", "device_id": "hb-host",
            "scheduled_at": refresh["scheduled_at"], "policy": {"max_delay_ms": 60000}, "params": expected,
        }
        fields = (("source_draft_text", "reopened_draft_text", "applied_draft_text", "request_text")
                  if browser_prefix else ("draft_text", "request_text"))
        for field in fields:
            assert _token(receipt[field], ("actions", 0, "params", "exposure", "shutter_s")) == self.case.shutter
            assert json.loads(receipt[field], parse_float=Decimal)["actions"][0]["params"] == expected
        if browser_prefix:
            assert receipt["source_draft_id"] != receipt["applied_draft_id"]
            assert receipt["preset_id"] == receipt["reopened_preset_id"]
            assert receipt["server_open_count"] == 2
            assert receipt["preset_http_status"] == 201
            for field in ("preset_params_text", "reopened_preset_params_text"):
                assert _token(receipt[field], ("exposure", "shutter_s")) == self.case.shutter
                assert json.loads(receipt[field], parse_float=Decimal) == expected
            assert _token(receipt["preset_http_text"], ("params", "exposure", "shutter_s")) == self.case.shutter
            assert json.loads(receipt["preset_http_text"], parse_float=Decimal)["params"] == expected
            source_action = json.loads(receipt["source_draft_text"], parse_float=Decimal)["actions"][0]
            assert json.loads(receipt["reopened_draft_text"], parse_float=Decimal)["actions"][0] == source_action
            assert json.loads(receipt["applied_draft_text"], parse_float=Decimal)["actions"][0] == request["actions"][0]
            assert request["actions"][0] == {**source_action, "scheduled_at": refresh["scheduled_at"]}
        else:
            assert receipt["preview_intent"] == "disabled"
            assert receipt["draft_request_id"] == receipt["request_id"]
            assert receipt["request_draft_id"] == receipt["draft_id"]
            assert json.loads(receipt["draft_text"], parse_float=Decimal) == {
                "name": request["name"], "actions": request["actions"],
            }
            saved = json.loads(receipt["request_body_text"], parse_float=Decimal)
            ack = {"last_report_id": receipt["last_report_id"]} if receipt["has_ack"] else {}
            assert request == {**saved, **ack}
        self.photo_plan = plan
        self._remember_request("capture", receipt)
        return plan

    def export_plan(self, name: str, action: dict):
        body = self.root / f"{name}-body.json"
        body.write_text(json.dumps({"name": name, "actions": [{"name": name, **action}]}))
        target = self.root / f"{name}-request.json"
        receipt = self.client("browser-plan-export", str(body), str(target),
                              output=self.root / f"{name}-browser.json")
        self._remember_request(name, receipt)
        return target, receipt

    def _remember_request(self, name, receipt):
        request_id = self.request_ids[name] = str(receipt["request_id"])
        expected = {name: "SUCCEEDED"}
        # 在调用方 submit 前保存日志起点；成功或故障报告由 report 声明。
        failure = self.host.watch_failures(request_id=request_id, expected_statuses=expected)
        self.report_checks[name] = (self.host, expected, failure)

    def report(self, name: str, *, status="succeeded"):
        request_id = self.request_ids[name]
        observed_host, expected, failure = self.report_checks[name]
        expected[name] = status.upper()
        if observed_host is not self.host:
            # 恢复用例已换成新 host，不能把已主动结束的旧组当成失败。
            failure = self.host.watch_failures(request_id=request_id, expected_statuses=expected)
        def found():
            for directory in (self.deployment.ready, self.deployment.processing):
                for path in directory.glob("status-report-*.json"):
                    try:
                        text = path.read_text()
                    except FileNotFoundError:
                        continue
                    report = json.loads(text, parse_float=Decimal)
                    for plan in report["plans"]:
                        if str(plan["request_id"]) != request_id:
                            continue
                        for action in plan["actions"]:
                            if action["name"] == name and action["status"] in ("succeeded", "failed"):
                                assert action["status"] == status, action
                                return path, report, action
            return None
        return wait_for(found, 90, f"{self.case.name}/{name} 请求 {request_id} 的 {status} 报告; {self.root}",
                        failure=failure)

    def claim(self):
        self.host.claim()
        archive = self.root / "claimed"
        archive.mkdir(exist_ok=True)
        for path in self.deployment.processing.iterdir():
            if path.is_file():
                target = archive / path.name
                if target.exists():
                    assert target.read_bytes() == path.read_bytes()
                else:
                    shutil.copy2(path, target)
        return archive

    def upload(self, paths: list[Path], label: str):
        manifest = self.root / f"{label}-manifest.json"
        manifest.write_text(json.dumps([{
            "path": str(path), "kind": "report" if path.name.startswith("status-report-") else "media",
        } for path in paths]))
        return self.client("upload-files", str(manifest), output=self.root / f"{label}-receipt.json")

    def observe_history(self, label, previous=None):
        extension = ".3fr" if self.case.capture_format == "raw" else ".jpg"
        name = "host-copy" + extension
        aliases = {"drive-mode": "drive_mode_single", "capture-format": "capture_format",
                   "exposure-mode": "exposure_mode", "iso": "iso", "aperture": "aperture",
                   "shutter": "shutter", "wb-mode": "white_balance_mode", "wb": "white_balance",
                   "focus-mode": "focus_mode"}
        expected = {"params_text": self.case.params_text, "source": self.case.source,
                    "steps": ["list", "info", *[aliases[item[1]] if item[0] == "set" else item[0]
                                                  for item in self.case.settings_argv], "capture"],
                    "camera_name": "camera-copy" + extension,
                    "camera_path": "/CAMERA/DCIM/100HASSELBL/camera-copy" + extension,
                    "artifacts": {}}
        for role, content, original_name, media in (
            ("original", self.original_bytes, name, "image/x-hasselblad-3fr" if self.case.capture_format == "raw" else "image/jpeg"),
            ("preview", self.preview_bytes, name + ".thumbnail.jpg", "image/jpeg"),
        ):
            expected["artifacts"][role] = {"original_name": original_name, "size_bytes": len(content),
                                           "sha256": hashlib.sha256(content).hexdigest(), "media_type": media}
        input_path = self.root / "history-expected.json"
        input_path.write_text(json.dumps(expected))
        output = self.root / f"photo-history-{label}.json"
        command = [self.package.python, self.root / "_hasselblad_photo_entry.py", "--h8b-history",
                   self.deployment.state_db, self.photo_plan, input_path, output]
        if previous is not None:
            command.append(previous)
        self.package.run(command)
        return output

    def assert_precision_and_sources(self, report_path, capture):
        expected = json.loads(self.case.params_text, parse_float=Decimal)
        action_id = int(capture["action_instance_id"])
        row, = _rows(self.deployment.state_db, "SELECT * FROM actions WHERE id=?", (action_id,))
        assert _token(row["input_fields_json"], ("params", "exposure", "shutter_s")) == self.case.shutter
        assert _token(row["effective_params_json"], ("exposure", "shutter_s")) == self.case.shutter
        assert json.loads(row["input_fields_json"], parse_float=Decimal)["params"] == expected
        assert json.loads(row["effective_params_json"], parse_float=Decimal) == expected
        assert json.loads(row["execution_spec_json"])["output_storage"] == self.case.source
        assert json.loads(row["execution_spec_json"])["start_call_timeout_s"] == 180
        report_text = report_path.read_text()
        report = json.loads(report_text)
        report_id = report["report_id"]
        report_location, = [(pi, ai) for pi, plan in enumerate(report["plans"])
                            for ai, action in enumerate(plan["actions"])
                            if action["action_instance_id"] == str(action_id)]
        for field in ("input_params", "effective_params"):
            assert _token(report_text, ("plans", report_location[0], "actions", report_location[1],
                                        field, "exposure", "shutter_s")) == self.case.shutter
        history_script = (
            "import json,sys; from pathlib import Path; "
            "from camctl.persistence.repositories.history import HistoryRepository; "
            "from camctl.persistence.transaction import encode_json_value; "
            "h=HistoryRepository(Path(sys.argv[1])); f=h.frozen_registration(int(sys.argv[2])); "
            "rows=h.restore_entity('action',int(sys.argv[3]),f.boundary); "
            "row=rows[('actions',int(sys.argv[3]))]; "
            "row={k:encode_json_value(v) if k.endswith('_json') and v is not None else v for k,v in row.items()}; "
            "print(json.dumps({'txn_id':f.boundary.txn_id,"
            "'last_event_id':f.boundary.last_event_id,'from_wm':f.from_wm,'to_wm':f.to_wm,"
            "'action':row}))"
        )
        saved = self.package.run([self.package.python, "-c", history_script,
                                  self.deployment.state_db, report_id, action_id])
        (self.root / "capture-frozen-history.json").write_text(saved.stdout)
        history = json.loads(saved.stdout)
        for field, prefix in (("input_fields_json", ("params",)), ("effective_params_json", ())):
            assert _token(history["action"][field], (*prefix, "exposure", "shutter_s")) == self.case.shutter
        assert json.loads(history["action"]["execution_spec_json"]) == json.loads(row["execution_spec_json"])
        assert history["to_wm"] == report["to_wm"]
        sources = _rows(self.deployment.state_db, "SELECT * FROM outputs WHERE source_action_id=? ORDER BY kind", (action_id,))
        assert len(sources) == 2 and sources[0]["id"] != sources[1]["id"]
        pair, = _rows(self.deployment.state_db, "SELECT * FROM output_origins WHERE output_id=?", (sources[1]["id"],))
        assert pair["original_output_id"] == sources[0]["id"]
        if self.case.source == "device":
            files = _rows(self.deployment.state_db, "SELECT * FROM device_files WHERE source_action_id=? ORDER BY role", (action_id,))
            assert [row["id"] for row in files] == [row["device_file_id"] for row in sources]
            assert files[1]["original_device_file_id"] == files[0]["id"]
            name = "camera-copy" + (".3fr" if self.case.capture_format == "raw" else ".jpg")
            for file, variant in zip(files, ("original", "thumbnail"), strict=True):
                locator = json.loads(file["locator_json"])
                assert locator["camera_name"] == name
                assert locator["camera_path"] == "/CAMERA/DCIM/100HASSELBL/" + name
                assert locator["variant"] == variant
            self.frozen_locators = [file["locator_json"] for file in files]
        else:
            files = _rows(self.deployment.state_db, "SELECT * FROM intermediate_files WHERE owner_action_id=? ORDER BY purpose", (action_id,))
            assert [row["purpose"] for row in files] == [5, 6]
            assert [row["id"] for row in files] == [row["intermediate_file_id"] for row in sources]
            assert files[0]["relative_path"] != files[1]["relative_path"]
        self.frozen_execution_spec = row["execution_spec_json"]
        self.history_before = self.observe_history("before")

    def assert_upload_and_ack(self, deliveries):
        archive = self.root / "claimed"
        reports = sorted(archive.glob("status-report-*.json"), key=lambda p: int(p.name.split("-")[2]))
        paths = [archive / delivery["file_name"] for delivery in deliveries]
        if self.case.shutter == "1/3000":
            waiting = self.upload(paths, "media-before-report")
            assert waiting["saved_report_ids"] == []
            assert len(waiting["media"]) == 4
            assert {row["status"] for row in waiting["media"]} == {"waiting_report"}
            before = {row["fileName"]: row for row in waiting["media"]}
            final = self.upload(reports, "reports-after-media")
            for row in final["media"]:
                previous = before[row["fileName"]]
                assert (row["id"], row["path"], row["sha256"], row["size"]) == (
                    previous["id"], previous["path"], previous["sha256"], previous["size"])
        else:
            first = self.upload(reports, "reports-before-media")
            assert first["media"] == []
            selected = paths if self.case.shutter == "1/1500" else [paths[0], paths[2]]
            final = self.upload(selected, "media-after-reports")
        expected_count = 2 if self.case.shutter == "1/4000" else 4
        assert final["worker_error"] is None
        assert len(final["media"]) == expected_count
        assert {row["status"] for row in final["media"]} == {"verified"}
        assert len({row["id"] for row in final["media"]}) == expected_count
        assert len([row for row in final["imports"] if row["kind"] == "media"]) == expected_count
        by_name = {row["fileName"]: row for row in final["media"]}
        for index, delivery in enumerate(deliveries):
            if expected_count == 2 and index % 2:
                assert delivery["file_name"] not in by_name
                continue
            row = by_name[delivery["file_name"]]
            assert (row["sha256"], row["size"]) == (delivery["sha256"], delivery["size"])
            assert Path(row["path"]).read_bytes() == (archive / delivery["file_name"]).read_bytes()
        captures = [(pi, ai, action) for pi, plan in enumerate(final["snapshot"]["plans"])
                    for ai, action in enumerate(plan["actions"]) if action["name"] == "capture"]
        pi, ai, capture = captures[0]
        assert len(capture["outputs"]) == 2
        imported_deliveries = [delivery for plan in final["snapshot"]["plans"]
                               for action in plan["actions"] for delivery in action.get("deliveries", [])]
        assert {row["delivery_id"] for row in imported_deliveries} == {row["delivery_id"] for row in deliveries}
        receipt_text = next(path for path in self.root.glob("*-receipt.json")
                            if json.loads(path.read_text()).get("ack_id") == final["ack_id"] and
                            len(json.loads(path.read_text()).get("media", [])) == expected_count).read_text()
        for field in ("input_params", "effective_params"):
            assert _token(receipt_text, ("snapshot", "plans", pi, "actions", ai, field,
                                         "exposure", "shutter_s")) == self.case.shutter
        target, ack = self.export_plan("ack", {"type": "report_status", "params": {"scope": "full"}})
        assert ack["last_report_id"] == final["ack_id"]
        acknowledged, = _rows(self.deployment.state_db, "SELECT * FROM reports WHERE id=?", (int(final["ack_id"]),))
        assert acknowledged["to_wm"] == final["coverage"]
        before_calls = self.argv()
        self.host.submit(target)
        wait_for(lambda: query_state(self.deployment.state_db,
                 "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state") ==
                 [(int(final["ack_id"]), acknowledged["to_wm"])], 30, "网页累计 ACK 的原水位持久化")
        assert self.argv() == before_calls
        (self.root / "ack-runtime.json").write_text(json.dumps({
            "report_id": final["ack_id"], "watermark": acknowledged["to_wm"], "media_count": expected_count,
        }))


@pytest.mark.parametrize("case", NORMAL_CASES, ids=lambda case: case.name)
def test_installed_hasselblad_photo_roundtrip(tmp_path, installed_camctl, host_demo, monkeypatch, case):
    world = PhotoWorld(tmp_path, installed_camctl, case, host_demo)
    monkeypatch.chdir(tmp_path)
    world.start()
    try:
        world.host.submit(world.export_photo(browser_prefix=case.name in FULL_UI_CASE_NAMES))
        report_path, report, capture = world.report("capture")
        assert "result" not in capture
        assert not {"steps", "diagnostics", "observations", "call_info"} & capture.keys()
        assert json.loads(case.params_text, parse_float=Decimal) == capture["input_params"] == capture["effective_params"]
        outputs = {output["kind"]: output for output in capture["outputs"]}
        assert set(outputs) == {"original", "preview"}
        assert outputs["original"]["output_id"] != outputs["preview"]["output_id"]
        assert outputs["preview"]["preview_of_output_id"] == outputs["original"]["output_id"]
        assert outputs["original"]["source_action_instance_id"] == outputs["preview"]["source_action_instance_id"]
        world.assert_precision_and_sources(report_path, capture)
        world.claim()
        calls = world.argv()
        settings = [tuple(row[3:]) for row in calls if row[3] in ("set", "focus", "autofocus")]
        assert settings == list(case.settings_argv)
        captures = [row for row in calls if row[3] == "capture"]
        assert len(captures) == 1
        work = Path(captures[0][-1])
        original_name = "host-copy" + (".3fr" if case.capture_format == "raw" else ".jpg")
        if case.source == "host":
            world.tool.rename(world.tool.with_suffix(".offline"))
        else:
            wait_for(lambda: not (work / original_name).exists()
                     and not (work / (original_name + ".thumbnail.jpg")).exists(), 30,
                     "生产维护回收 capture 工作副本")
            capture_files = _rows(world.deployment.state_db, "SELECT * FROM intermediate_files WHERE owner_action_id=? ORDER BY purpose",
                                  (int(capture["action_instance_id"]),))
            assert len(capture_files) == 2
            assert all(row["retention_state"] == 2 and row["cleanup_state"] == 4 for row in capture_files)
            assert all(not (world.deployment.staging / row["relative_path"]).exists() for row in capture_files)
        deliveries = []
        for index, role in enumerate(("original", "preview", "original", "preview"), 1):
            name = f"obtain-{index}-{role}"
            target, _ = world.export_plan(name, {
                "type": "obtain_action_outputs",
                "scheduled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "params": {"source": {"action_instance_id": capture["action_instance_id"]},
                           "filter": "default" if role == "original" else "preview", "purpose": "manual"},
            })
            world.host.submit(target)
            _, _, obtain = world.report(name)
            delivery, = obtain["deliveries"]
            assert delivery["output_id"] == outputs[role]["output_id"]
            assert delivery["source_action_instance_id"] == capture["action_instance_id"]
            assert delivery["delivery_id"] not in [row["delivery_id"] for row in deliveries]
            assert delivery["file_name"] not in [row["file_name"] for row in deliveries]
            world.claim()
            content = (world.deployment.processing / delivery["file_name"]).read_bytes()
            assert content == (world.original_bytes if role == "original" else world.preview_bytes)
            assert delivery["size"] == len(content)
            assert delivery["sha256"] == hashlib.sha256(content).hexdigest()
            if case.source == "device":
                read_call = [row for row in world.argv() if row[3] == "download"][-1]
                read_workspace = Path(read_call[-1])
                wait_for(lambda: not read_workspace.exists(), 30, "真实 READ 操作收场后回收独占下载目录")
            deliveries.append(delivery)
            # processing 的已交付副本归主程序；原正式源由生产继续保留。
            (world.deployment.processing / delivery["file_name"]).unlink()
        after = world.argv()
        if case.source == "host":
            assert after == calls
        else:
            downloads = [row for row in after if row[3] == "download"]
            assert [row[4:6] for row in downloads] == [
                [role, "camera-copy" + (".3fr" if case.capture_format == "raw" else ".jpg")]
                for role in ("original", "thumbnail", "original", "thumbnail")]
            assert len({row[-1] for row in downloads}) == 4
        world.host.submit(world.photo_plan)
        assert world.argv() == after
        action_id = int(capture["action_instance_id"])
        assert len(_rows(world.deployment.state_db, "SELECT * FROM operation_runs WHERE responsibility_key=?", (f"start/{action_id}",))) == 1
        assert len(_rows(world.deployment.state_db, "SELECT * FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE responsibility_key=?)", (f"start/{action_id}",))) == 1
        assert _rows(world.deployment.state_db, "SELECT execution_spec_json FROM actions WHERE id=?", (action_id,))[0]["execution_spec_json"] == world.frozen_execution_spec
        if case.source == "device":
            assert [row["locator_json"] for row in _rows(world.deployment.state_db,
                    "SELECT locator_json FROM device_files WHERE source_action_id=? ORDER BY role", (action_id,))] == world.frozen_locators
        if case.capture_format == "jpeg" and case.shutter == "1/4000":
            assert outputs["original"]["media_type"] == outputs["preview"]["media_type"] == "image/jpeg"
            assert outputs["original"]["checksum"] == outputs["preview"]["checksum"]
        world.assert_upload_and_ack(deliveries)
        history_after = world.observe_history("after", world.history_before)
        before = json.loads(world.history_before.read_text(), parse_float=Decimal)
        after = json.loads(history_after.read_text(), parse_float=Decimal)
        assert before["facts"] == after["facts"]
        assert before["provenance"]["pid"] != after["provenance"]["pid"]
        provenance = [json.loads(line) for line in world.provenance.read_text().splitlines()]
        assert {row["kind"] for row in provenance} == {"camctl", "hb_cli"}
        for row in provenance:
            assert row["python"] == str(world.package.python)
            assert Path(row["camctl"]).is_relative_to(world.package.python.parent.parent)
            assert Path(row["cwd"]).is_relative_to(tmp_path)
            if row["kind"] == "camctl":
                assert row["fault_environment"]["value"] in (None, "")
        environments = json.loads((world.root / "environment-provenance.json").read_text())
        for field in ("test_environment", "installed_environment", "host_start_environment"):
            assert environments[field]["value"] in (None, "")
    finally:
        world.close()


def _capture_target(world):
    request_id = int(json.loads(world.photo_plan.read_text())["request_id"])
    rows = _rows(world.deployment.state_db,
        "SELECT p.id AS plan_id,a.id AS action_id,a.device_id,a.driver_id,r.id AS run_id,"
        "r.kind AS run_kind,r.activity_id,r.responsibility_key FROM plans p "
        "JOIN actions a ON a.plan_id=p.id JOIN operation_runs r ON r.action_id=a.id "
        "WHERE p.request_id=? AND a.name=? AND r.responsibility_key='start/'||a.id",
        (request_id, "capture"))
    target, = rows
    target["request_id"] = str(request_id)
    assert target["device_id"] == "hb-host" and target["driver_id"] == "hasselblad_cfv100c"
    return target


def _attempt(world, target):
    row, = _rows(world.deployment.state_db,
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (target["run_id"],))
    return row


def _begin_fault(world, fault):
    world.package.environment["CAMCTL_HB_TEST_FAULT"] = fault
    plan = world.export_photo()
    world.start()
    world.host.submit(plan)


def _fault_marker(world, name):
    def found():
        path = world.root / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text())
        # 同步点未装配时，正常完成直接证伪断点，而不是等超时冒充命中。
        actions = _rows(world.deployment.state_db, "SELECT status FROM actions WHERE name='capture'")
        if name in ("after-start-result", "before-start-result", "complete-commit", "complete-verifier") and actions and _rows(world.deployment.state_db, "SELECT id FROM outputs"):
            raise AssertionError(f"未命中 {name}，照片已正常完成；{world.root}")
        if name == "report-before-publish":
            for directory in (world.deployment.ready, world.deployment.processing):
                for report in directory.glob("status-report-*.json"):
                    try:
                        value = json.loads(report.read_text())
                    except FileNotFoundError:
                        continue
                    if any(action["name"] == "capture" and action["status"] == "succeeded"
                           and len(action.get("outputs", [])) == 2
                           for plan in value["plans"] for action in plan["actions"]):
                        raise AssertionError(f"未命中 {name}，成功双产物报告已经发布；{world.root}")
        return None
    return wait_for(found, 90, f"真实安装进程故障门闩 {name}; {world.root}")


def _stop_fault_host(world, marker):
    world.host.stop()
    live = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == marker['pgid'] and fields[0] != 'Z':
            live.append({'pid': int(path.name), 'state': fields[0]})
    (world.root / 'old-group-after-stop.json').write_text(json.dumps({
        'original_pid': marker['pid'], 'original_pgid': marker['pgid'], 'live_members': live,
    }))
    assert live == []


def _restart_fault_host(world):
    world.host = HostDemo(world.deployment, world.host._demo_path, str(world.launcher))
    _, expected, _ = world.report_checks['capture']
    failure = world.host.watch_failures(
        request_id=world.request_ids['capture'], expected_statuses=expected)
    world.report_checks['capture'] = (world.host, expected, failure)
    world.start()
    world.host.submit(world.photo_plan)


def _fault_pair(world, capture):
    outputs = {row['kind']: row for row in capture['outputs']}
    assert set(outputs) == {'original', 'preview'}
    assert outputs['original']['output_id'] != outputs['preview']['output_id']
    assert outputs['preview']['preview_of_output_id'] == outputs['original']['output_id']
    calls = world.argv()
    capture_call, = [row for row in calls if row[3] == 'capture']
    work = Path(capture_call[-1])
    if world.case.source == 'host':
        world.tool.rename(world.tool.with_suffix('.offline'))
    else:
        wait_for(lambda: not any(work.glob('host-copy*')), 30, '生产回收 capture 双工作副本')
    deliveries = []
    for index, role in enumerate(('original', 'preview', 'original', 'preview'), 1):
        name = f'fault-obtain-{index}-{role}'
        plan, _ = world.export_plan(name, {
            'type': 'obtain_action_outputs',
            'scheduled_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'params': {'source': {'action_instance_id': capture['action_instance_id']},
                       'filter': 'default' if role == 'original' else 'preview', 'purpose': 'manual'},
        })
        world.host.submit(plan)
        _, _, obtain = world.report(name)
        delivery, = obtain['deliveries']
        assert delivery['output_id'] == outputs[role]['output_id']
        assert delivery['source_action_instance_id'] == capture['action_instance_id']
        assert delivery['delivery_id'] not in [item['delivery_id'] for item in deliveries]
        assert delivery['file_name'] not in [item['file_name'] for item in deliveries]
        world.claim()
        content = (world.deployment.processing / delivery['file_name']).read_bytes()
        assert content == (world.original_bytes if role == 'original' else world.preview_bytes)
        assert delivery['size'] == len(content) and delivery['sha256'] == hashlib.sha256(content).hexdigest()
        deliveries.append(delivery)
        (world.deployment.processing / delivery['file_name']).unlink()
        if world.case.source == 'device':
            download = [row for row in world.argv() if row[3] == 'download'][-1]
            assert download[4:6] == ['original' if role == 'original' else 'thumbnail',
                                      'camera-copy' + ('.3fr' if world.case.capture_format == 'raw' else '.jpg')]
            wait_for(lambda: not Path(download[-1]).exists(), 30, '真实 READ 独占工作目录已回收')
    replay_commands = ('set', 'focus', 'autofocus', 'capture')
    assert [row for row in world.argv() if row[3] in replay_commands] == [row for row in calls if row[3] in replay_commands]
    if world.case.source == 'host':
        assert world.argv() == calls
    else:
        downloads = [row for row in world.argv() if row[3] == 'download']
        assert len(downloads) == len({row[-1] for row in downloads}) == 4
    return outputs


@pytest.mark.parametrize('case', (PhotoCase('jpeg', 'host', '1/1500'), PhotoCase('raw', 'device', '1/3000')), ids=lambda c:c.name)
def test_installed_hasselblad_saved_start_recovers_without_replay(tmp_path, installed_camctl, host_demo, monkeypatch, case):
    world = PhotoWorld(tmp_path, installed_camctl, case, host_demo)
    monkeypatch.chdir(tmp_path)
    try:
        _begin_fault(world, 'after-start-result')
        marker = _fault_marker(world, 'after-start-result')
        target = _capture_target(world)
        assert marker['target'] == target
        before = _attempt(world, target)
        codes = _fault_codes(world)
        assert target['run_kind'] == codes['start_kind']
        assert (before['status'], before['effect_state']) == (codes['attempt_succeeded'], codes['effect_confirmed'])
        assert marker['ticket'] == {'attempt_id':before['attempt_no'], 'operation':'control',
                                    'target_id':str(target['activity_id']), 'responsibility_key':target['responsibility_key'],
                                    'run_id':target['run_id']}
        assert before['result_event_id'] is not None and before['result_json'] is not None
        result = json.loads(before['result_json'])
        assert [row['type'] for row in result['observations']] == ['photo_taken', 'hasselblad_start_entered']
        assert all(row['version'] == 1 and row['data']['activity_id'] == str(target['activity_id'])
                   for row in result['observations'])
        event, = _rows(world.deployment.state_db, 'SELECT * FROM history_events WHERE id=?', (before['result_event_id'],))
        transaction, = _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE id=?', (event['transaction_id'],))
        assert transaction['operation_key'] == marker['operation_key']
        assert marker['result_event_id'] == before['result_event_id']
        assert marker['boundary'] == {'txn_id': transaction['id'], 'last_event_id': transaction['last_event_id']}
        assert marker['in_transaction'] is False
        assert _rows(world.deployment.state_db, "SELECT id FROM history_events WHERE json_extract(body_json,'$.evidence.host_photo_request.original_file_id') IS NOT NULL") == []
        assert transaction['first_event_id'] <= event['id'] <= transaction['last_event_id']
        assert _rows(world.deployment.state_db, 'SELECT id FROM outputs WHERE source_action_id=?', (target['action_id'],)) == []
        calls = world.argv()
        capture_call, = [row for row in calls if row[3] == 'capture']
        work = Path(capture_call[-1]); actual = sorted(path for path in work.iterdir() if path.is_file())
        assert len(actual) == 2
        assert sorted(path.read_bytes() for path in actual) == sorted((world.original_bytes, world.preview_bytes))
        (world.root / 'before-kill-start.json').write_text(json.dumps({'target':target, 'attempt':before, 'transaction':transaction}))
        _stop_fault_host(world, marker)
        _restart_fault_host(world)
        report_path, _, capture = world.report('capture')
        assert _capture_target(world) == target and _attempt(world, target) == before
        assert _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE id=?', (transaction['id'],)) == [transaction]
        assert world.argv() == calls
        world.assert_precision_and_sources(report_path, capture)
        _fault_pair(world, capture)
        assert [row for row in world.argv() if row[3] in ('set','focus','autofocus','capture')] == [row for row in calls if row[3] in ('set','focus','autofocus','capture')]
    finally:
        world.close()


@pytest.mark.parametrize('case', (PhotoCase('jpeg', 'host', '1/1500'), PhotoCase('jpeg', 'device', '1/3000')), ids=lambda c:c.name)
def test_installed_hasselblad_complete_commit_receipt_loss_reuses_request(tmp_path, installed_camctl, host_demo, monkeypatch, case):
    world = PhotoWorld(tmp_path, installed_camctl, case, host_demo)
    monkeypatch.chdir(tmp_path)
    try:
        _begin_fault(world, 'complete-receipt-loss')
        report_path, _, capture = world.report('capture')
        saved = _fault_marker(world, 'complete-commit')
        verified = _fault_marker(world, 'complete-verifier')
        assert saved['request'] == verified['request']
        assert saved['operation_key'] == verified['operation_key']
        assert saved['boundary'] == verified['boundary']
        assert saved['in_transaction'] is False and saved['returned_receipt'] == 'unknown'
        assert verified['returned_receipt'] == 'completed' and verified['query_only'] == 1
        assert verified['write_connection'] == saved['write_connection'] != verified['verifier_connection']
        target = _capture_target(world)
        assert saved['target'] == target
        transaction, = _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE operation_key=?', (saved['operation_key'],))
        assert (transaction['id'], transaction['last_event_id']) == (saved['boundary']['txn_id'], saved['boundary']['last_event_id'])
        files = _rows(world.deployment.state_db, 'SELECT * FROM intermediate_files WHERE owner_action_id=? ORDER BY purpose', (target['action_id'],))
        assert len(files) == 2
        for file, prefix in zip(files, ('original','preview'), strict=True):
            assert file['id'] == saved['request'][prefix+'_file_id']
            assert file['size_bytes'] == saved['request'][prefix+'_size_bytes']
            assert file['sha256'] == saved['request'][prefix+'_sha256']
        assert len(_rows(world.deployment.state_db, 'SELECT id FROM outputs WHERE source_action_id=?', (target['action_id'],))) == 2
        assert len(_rows(world.deployment.state_db, 'SELECT oo.id FROM output_origins oo JOIN outputs o ON o.id=oo.output_id WHERE o.source_action_id=?', (target['action_id'],))) == 1
        world.assert_precision_and_sources(report_path, capture)
        before = _attempt(world, target); calls = world.argv()
        _stop_fault_host(world, saved)
        _restart_fault_host(world)
        _, _, repeated = world.report('capture')
        assert repeated['outputs'] == capture['outputs']
        assert _attempt(world, target) == before and world.argv() == calls
        _fault_pair(world, capture)
    finally:
        world.close()


def _fault_codes(world):
    code = (
        "import json; from camctl.contracts.enums import enum_for; "
        "from camctl.contracts.workflow_errors import registered_error; "
        "fields={'attempt_succeeded':('operation_attempts.status','SUCCEEDED'),"
        "'effect_confirmed':('operation_attempts.effect_state','CONFIRMED'),'attempt_running':('operation_attempts.status','RUNNING'),"
        "'attempt_unknown':('operation_attempts.status','UNKNOWN'),"
        "'effect_unknown':('operation_attempts.effect_state','UNKNOWN'),"
        "'run_unconfirmed':('operation_runs.status','UNCONFIRMED'),"
        "'activity_unknown':('device_activities.activity_state','UNKNOWN'),"
        "'occupancy_held':('device_activities.occupancy_state','HELD'),"
        "'start_kind':('operation_runs.kind','START'),"
        "'report_failed':('reports.status','FAILED'),'report_published':('reports.status','PUBLISHED')}; "
        "values={key:int(enum_for(field)[member]) for key,(field,member) in fields.items()}; "
        "values['capture_error']=registered_error('capture_result_unconfirmed')['action_error_id']; "
        "print(json.dumps(values))"
    )
    return json.loads(world.package.run([world.package.python, '-c', code]).stdout)


def _no_private_details(action, world):
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(x) for x in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(x) for x in value))
        return set()
    assert not keys(action) & {'diagnostics','steps','stdout','stderr','executable','local_exit_code','transport_error'}
    assert str(world.root) not in json.dumps(action, default=str)


def test_installed_hasselblad_missing_start_result_never_replays(tmp_path, installed_camctl, host_demo, monkeypatch):
    case = PhotoCase('jpeg', 'host', '1/4000', focus_override=('MF', 5))
    world = PhotoWorld(tmp_path, installed_camctl, case, host_demo)
    monkeypatch.chdir(tmp_path)
    try:
        codes = _fault_codes(world)
        _begin_fault(world, 'before-start-result')
        marker = _fault_marker(world, 'before-start-result')
        target = _capture_target(world)
        assert marker['target'] == target and target['run_kind'] == codes['start_kind']
        before = _attempt(world, target)
        assert marker['in_transaction'] is False
        assert marker['ticket'] == {'attempt_id':before['attempt_no'], 'operation':'control',
                                    'target_id':str(target['activity_id']), 'responsibility_key':target['responsibility_key'],
                                    'run_id':target['run_id']}
        assert before['status'] == codes['attempt_running']
        assert before['result_event_id'] is None and before['result_json'] is None
        assert before['intent_event_id'] is not None
        calls = world.argv(); capture_call, = [row for row in calls if row[3] == 'capture']
        assert [tuple(row[3:]) for row in calls if row[3] in ('set', 'focus', 'autofocus')] == list(case.settings_argv)
        assert [row[3:] for row in calls if row[3] == 'focus'] == [['focus', '5']]
        work = Path(capture_call[-1]); files = sorted(path for path in work.iterdir() if path.is_file())
        assert len(files) == 2
        assert sorted(p.read_bytes() for p in files) == sorted((world.original_bytes, world.preview_bytes))
        assert _rows(world.deployment.state_db, 'SELECT id FROM outputs WHERE source_action_id=?', (target['action_id'],)) == []
        (world.root / 'before-missing-result-kill.json').write_text(json.dumps({'target':target,'attempt':before}))
        _stop_fault_host(world, marker)
        _restart_fault_host(world)
        _, _, capture = world.report('capture', status='failed')
        after = _attempt(world, target)
        assert _capture_target(world) == target
        assert after['id'] == before['id'] and after['intent_event_id'] == before['intent_event_id']
        assert (after['status'], after['effect_state']) == (codes['attempt_unknown'], codes['effect_unknown'])
        assert after['result_event_id'] is not None
        assert (world.root / 'start-recovery-observed.json').is_file(), '缺少真实 recover_attempt 观察，不能据结果推造恢复依据'
        recovery = _fault_marker(world, 'start-recovery-observed')
        assert recovery['target'] == target and recovery['ticket'] == marker['ticket']
        assert recovery['intent_event_id'] == before['intent_event_id']
        assert recovery['boundary'] == 'HOST_LOCAL_SETTLED'
        assert recovery['horizon'] is not None and before['intent_event_id'] <= recovery['horizon'] <= recovery['max_event_before']
        assert recovery['local_entry_active'] is False and recovery['returned'] is True and recovery['diagnostic'] is None
        assert recovery['attempt_status_before'] == codes['attempt_running']
        result = json.loads(after['result_json'])
        assert result['settlement'] == {'basis':'assumed','evidence':{'type':'local_control_recovery','version':1,'data':{}}}
        assert result['observations'] == []
        assert 'diagnostics' not in result and 'call_info' not in result
        run, = _rows(world.deployment.state_db, 'SELECT * FROM operation_runs WHERE id=?', (target['run_id'],))
        assert run['status'] == codes['run_unconfirmed']
        action, = _rows(world.deployment.state_db, 'SELECT * FROM actions WHERE id=?', (target['action_id'],))
        assert action['error_code'] == codes['capture_error']
        expected_error = {'code':'capture_result_unconfirmed','stage':'execution',
                          'details':{'activity_id':str(target['activity_id']),'reason':'outputs_unknown'}}
        assert capture['error'] == expected_error
        assert 'result' not in capture and not capture.get('outputs')
        activity, = _rows(world.deployment.state_db, 'SELECT * FROM device_activities WHERE id=?', (target['activity_id'],))
        assert activity['started_at'] is None
        assert (activity['activity_state'],activity['occupancy_state']) == (codes['activity_unknown'],codes['occupancy_held'])
        execution = {'status':'end_unconfirmed'}
        if activity['last_error_json'] is not None:
            execution['error'] = json.loads(activity['last_error_json'])
        assert capture['device_execution'] == execution
        _no_private_details(capture, world)
        assert world.argv() == calls
        assert all(path.is_file() for path in files)
        assert sorted(path.read_bytes() for path in files) == sorted((world.original_bytes, world.preview_bytes))
        assert _rows(world.deployment.state_db, 'SELECT id FROM outputs WHERE source_action_id=?', (target['action_id'],)) == []
        assert _rows(world.deployment.state_db, 'SELECT oo.id FROM output_origins oo JOIN outputs o ON o.id=oo.output_id WHERE o.source_action_id=?', (target['action_id'],)) == []
    finally:
        world.close()


def test_installed_hasselblad_report_rebuild_keeps_frozen_bytes(tmp_path, installed_camctl, host_demo, monkeypatch):
    case = PhotoCase('jpeg', 'device', '1/1500', focus_override=('MF', None))
    world = PhotoWorld(tmp_path, installed_camctl, case, host_demo)
    monkeypatch.chdir(tmp_path)
    try:
        codes = _fault_codes(world)
        _begin_fault(world, 'report-before-publish')
        marker = _fault_marker(world, 'report-before-publish')
        target = _capture_target(world)
        assert marker['target'] == target
        report_id = marker['report_id']
        row, = _rows(world.deployment.state_db, 'SELECT * FROM reports WHERE id=?', (report_id,))
        frozen, = _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE id=?', (marker['boundary']['txn_id'],))
        assert frozen['last_event_id'] == marker['boundary']['last_event_id']
        assert marker['in_transaction'] is False
        management = marker['management']
        event, = _rows(world.deployment.state_db, 'SELECT * FROM history_events WHERE id=?', (management['event_id'],))
        transaction, = _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE id=?', (event['transaction_id'],))
        assert transaction['operation_key'] == management['operation_key']
        assert transaction['first_event_id'] == management['first_event_id']
        assert (transaction['id'], transaction['last_event_id']) == (management['boundary']['txn_id'], management['boundary']['last_event_id'])
        assert json.loads(event['body_json']) == management['event_body']
        changed, = management['event_body']['rows']
        assert changed['table'] == 'reports' and changed['id'] == report_id
        assert (row['from_wm'],row['to_wm'],row['frozen_event_id']) == (marker['from_wm'],marker['to_wm'],marker['boundary']['last_event_id'])
        original, = (world.deployment.staging / 'reports').glob(f'status-report-{report_id}-*.json')
        original_bytes = original.read_bytes(); original_name = original.name
        assert row['size_bytes'] == len(original_bytes) and row['sha256'] == hashlib.sha256(original_bytes).hexdigest()
        capture, = [action for plan in json.loads(original_bytes, parse_float=Decimal)['plans'] for action in plan['actions'] if action['action_instance_id']==str(target['action_id'])]
        assert capture['status'] == 'succeeded' and len(capture['outputs']) == 2
        calls = world.argv()
        world.claim()
        assert list(world.deployment.ready.iterdir()) == []
        world.deployment.ready.rmdir()
        world.deployment.ready.write_bytes(b'H8b deterministic ready directory fault')
        (world.root / 'report-before-publish.release').touch()
        failed = wait_for(lambda: next((r for r in _rows(world.deployment.state_db,
                  'SELECT * FROM reports WHERE id=?',(report_id,)) if r['status']==codes['report_failed']),None),
                  60, '真实目录发布失败已可靠保存')
        assert failed['sha256'] == row['sha256'] and failed['size_bytes'] == row['size_bytes']
        assert failed['last_error_json'] is not None
        actual_error = json.loads(failed['last_error_json'])['error']
        assert all(value in actual_error for value in ('withdraw_failed', 'list_failed', 'NotADirectoryError', str(world.deployment.ready)))
        _stop_fault_host(world, marker)
        assert world.argv() == calls
        world.deployment.ready.unlink(); world.deployment.ready.mkdir()
        saved = world.root / 'failed-report-evidence'; saved.mkdir()
        shutil.move(original,saved/original_name)
        assert not list((world.deployment.staging/'reports').glob(f'status-report-{report_id}-*.json'))
        _restart_fault_host(world)
        rebuilt = _fault_marker(world, 'report-rebuilt-published')
        assert rebuilt['report_id'] == report_id and rebuilt['pid'] != marker['pid']
        assert rebuilt['in_transaction'] is False and rebuilt['returned_receipt'] == 'completed'
        assert rebuilt['publication']['report_id'] == report_id
        assert rebuilt['publication']['published_event_id'] == rebuilt['management']['event_id']
        new = world.deployment.ready / original_name
        assert new.read_bytes() == original_bytes
        current, = _rows(world.deployment.state_db,'SELECT * FROM reports WHERE id=?',(report_id,))
        publication_event, = _rows(world.deployment.state_db, 'SELECT * FROM history_events WHERE id=?', (rebuilt['management']['event_id'],))
        publication_transaction, = _rows(world.deployment.state_db, 'SELECT * FROM history_transactions WHERE id=?', (publication_event['transaction_id'],))
        assert current['last_published_event_id'] == publication_event['id']
        assert json.loads(publication_event['body_json']) == rebuilt['management']['event_body']
        assert publication_transaction['operation_key'] == rebuilt['management']['operation_key']
        assert publication_transaction['first_event_id'] == rebuilt['management']['first_event_id']
        assert (publication_transaction['id'], publication_transaction['last_event_id']) == (rebuilt['management']['boundary']['txn_id'], rebuilt['management']['boundary']['last_event_id'])
        (world.root / 'publication-states.json').write_text(json.dumps({
            'recorded_utc':datetime.now(timezone.utc).isoformat(), 'original_before_fault':row,
            'failed_before_stop':failed, 'published_before_claim':current, 'frozen_transaction':frozen,
            'intent_transaction':transaction, 'publication_transaction':publication_transaction,
            'file_name':original_name, 'size':len(original_bytes), 'sha256':hashlib.sha256(original_bytes).hexdigest(),
        }))
        for key in ('frozen_event_id','from_wm','to_wm','size_bytes','sha256'):
            assert current[key] == row[key]
        assert current['publication_count'] == 1 and current['status'] == codes['report_published']
        world.claim()
        delivered = world.deployment.processing / original_name
        assert delivered.read_bytes() == original_bytes
        (world.root / 'report-rebuilt-published.release').touch()
        assert world.argv() == calls
        world.assert_precision_and_sources(delivered,capture)
        _no_private_details(capture, world)
        _fault_pair(world,capture)
        assert delivered.read_bytes() == original_bytes
        assert (saved/original_name).read_bytes() == original_bytes
    finally:
        world.close()
