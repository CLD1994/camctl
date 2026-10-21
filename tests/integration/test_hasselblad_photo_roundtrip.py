"""Hasselblad 安装能力、真实网页及文件上传的跨组件接缝。

相机拍摄链在后续用例消费同一组入口；本文件的网页与上传定向
用例分别证明实际预设重开/下载和真实 Files.upload，不连接设备。
"""
from __future__ import annotations

from dataclasses import dataclass
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

import pytest

from test_hasselblad_photo_parameters import InstalledCamctl, installed_camctl
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl_fixtures import Deployment


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


@dataclass(frozen=True)
class _NumberToken:
    text: str


def _token(text: str, path: tuple[str | int, ...]) -> str:
    value = json.loads(text, parse_float=_NumberToken)
    for part in path:
        value = value[part]
    assert isinstance(value, _NumberToken), (path, value)
    return value.text


@pytest.mark.parametrize("shutter", ("0.000667", "0.000333", "0.00025"))
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
        '"exposure":{"mode":"M","iso":100,"aperture":8,"shutter_s":' + shutter + '},'
        '"white_balance":{"mode":"manual","temperature_k":5500},'
        '"focus":{"mode":"MF","relative_move":5}}'
    )
    input_path = tmp_path / "browser-input.json"
    input_path.write_text(json.dumps({
        "device_id": "hb-host", "action_type": "camera_take_photo",
        "action_name": "capture", "case_name": f"网页精确快门-{shutter}",
        "params_text": params_text,
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
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

    @property
    def name(self):
        return f"{self.capture_format}-{self.source}-{self.shutter}"

    @property
    def params_text(self):
        exposure = ('"mode":"S","iso":100' if self.shutter == "0.000333"
                    else '"mode":"M","iso":100,"aperture":8')
        wb, focus = {
            "0.000667": ('{"mode":"manual","temperature_k":5500}', '{"mode":"MF","relative_move":5}'),
            "0.000333": ('{"mode":"auto"}', '{"mode":"AF-S"}'),
            "0.00025": ('{"mode":"daylight"}', '{"mode":"MF"}'),
        }[self.shutter]
        return ('{"type":"hasselblad_photo","capture_format":' + json.dumps(self.capture_format)
                + ',"exposure":{' + exposure + ',"shutter_s":' + self.shutter + '},'
                + '"white_balance":' + wb + ',"focus":' + focus + '}')

    @property
    def settings_argv(self):
        # 独立手写代表期望，绝不调用被测 photo_start_commands。
        commands = [("set", "drive-mode", "single"), ("set", "capture-format", self.capture_format),
                    ("set", "exposure-mode", "S" if self.shutter == "0.000333" else "M"),
                    ("set", "iso", "100")]
        if self.shutter != "0.000333":
            commands.append(("set", "aperture", "8"))
        commands.append(("set", "shutter", self.shutter))
        if self.shutter == "0.000667":
            commands.extend((("set", "wb-mode", "manual"), ("set", "wb", "5500,0"),
                             ("set", "focus-mode", "MF"), ("focus", "5")))
        elif self.shutter == "0.000333":
            commands.extend((("set", "wb-mode", "auto"), ("set", "focus-mode", "AF-S"), ("autofocus",)))
        else:
            commands.extend((("set", "wb-mode", "daylight"), ("set", "focus-mode", "MF")))
        return tuple(commands)


NORMAL_CASES = tuple(PhotoCase(fmt, source, shutter)
                     for fmt in ("raw", "jpeg") for source in ("host", "device")
                     for shutter in ("0.000667", "0.000333", "0.00025"))


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
        if case.capture_format == "jpeg" and case.shutter == "0.00025":
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
        self.host.stop()
        with sqlite3.connect(f"file:{self.deployment.state_db}?mode=ro", uri=True) as source:
            with sqlite3.connect(self.root / "evidence-state.db") as target:
                source.backup(target)

    def argv(self):
        return [json.loads(line) for line in self.argv_log.read_text().splitlines()]

    def export_photo(self):
        input_path = self.root / "photo-input.json"
        input_path.write_text(json.dumps({
            "device_id": "hb-host", "action_type": "camera_take_photo", "action_name": "capture",
            "case_name": self.case.name, "params_text": self.case.params_text,
            "scheduled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }))
        plan = self.root / "photo-request.json"
        self.browser = _client(self.package, "browser-photo-export", str(self.store), str(input_path), str(plan),
                               output=self.root / "photo-browser.json")
        self.photo_plan = plan
        return plan

    def export_plan(self, name: str, action: dict):
        body = self.root / f"{name}-body.json"
        body.write_text(json.dumps({"name": name, "actions": [{"name": name, **action}]}))
        target = self.root / f"{name}-request.json"
        receipt = _client(self.package, "browser-plan-export", str(self.store), str(body), str(target),
                          output=self.root / f"{name}-browser.json")
        return target, receipt

    def report(self, name: str):
        def found():
            for directory in (self.deployment.ready, self.deployment.processing):
                for path in directory.glob("status-report-*.json"):
                    try:
                        text = path.read_text()
                    except FileNotFoundError:
                        continue
                    report = json.loads(text, parse_float=Decimal)
                    for plan in report["plans"]:
                        for action in plan["actions"]:
                            if action["name"] == name and action["status"] in ("succeeded", "failed"):
                                assert action["status"] == "succeeded", action
                                return path, report, action
            return None
        return wait_for(found, 90, f"{self.case.name}/{name} 的成功报告; {self.root}")

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
        return _client(self.package, "upload-files", str(self.store), str(manifest),
                       output=self.root / f"{label}-receipt.json")

    def assert_precision_and_sources(self, report_path, capture):
        expected = json.loads(self.case.params_text, parse_float=Decimal)
        for field in ("source_draft_text", "reopened_draft_text", "applied_draft_text", "request_text"):
            assert _token(self.browser[field], ("actions", 0, "params", "exposure", "shutter_s")) == self.case.shutter
        for field in ("preset_params_text", "reopened_preset_params_text"):
            assert _token(self.browser[field], ("exposure", "shutter_s")) == self.case.shutter
        assert _token(self.browser["preset_http_text"], ("params", "exposure", "shutter_s")) == self.case.shutter
        action_id = int(capture["action_instance_id"])
        row, = _rows(self.deployment.state_db, "SELECT * FROM actions WHERE id=?", (action_id,))
        assert _token(row["input_fields_json"], ("params", "exposure", "shutter_s")) == self.case.shutter
        assert _token(row["effective_params_json"], ("exposure", "shutter_s")) == self.case.shutter
        assert json.loads(row["input_fields_json"], parse_float=Decimal)["params"] == expected
        assert json.loads(row["effective_params_json"], parse_float=Decimal) == expected
        assert json.loads(row["execution_spec_json"])["output_storage"] == self.case.source
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

    def assert_upload_and_ack(self, deliveries):
        archive = self.root / "claimed"
        reports = sorted(archive.glob("status-report-*.json"), key=lambda p: int(p.name.split("-")[2]))
        paths = [archive / delivery["file_name"] for delivery in deliveries]
        if self.case.shutter == "0.000333":
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
            selected = paths if self.case.shutter == "0.000667" else [paths[0], paths[2]]
            final = self.upload(selected, "media-after-reports")
        expected_count = 2 if self.case.shutter == "0.00025" else 4
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
        world.host.submit(world.export_photo())
        report_path, report, capture = world.report("capture")
        assert "result" not in capture
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
        if case.capture_format == "jpeg" and case.shutter == "0.00025":
            assert outputs["original"]["media_type"] == outputs["preview"]["media_type"] == "image/jpeg"
            assert outputs["original"]["checksum"] == outputs["preview"]["checksum"]
        world.assert_upload_and_ack(deliveries)
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
