"""Hasselblad 安装能力、真实网页及文件上传的跨组件接缝。

相机拍摄链在后续用例消费同一组入口；本文件的网页与上传定向
用例分别证明实际预设重开/下载和真实 Files.upload，不连接设备。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_hasselblad_photo_parameters import InstalledCamctl, installed_camctl


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
