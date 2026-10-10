"""安装后的内建 Action6 驱动经真实 C host 完成录像、取回与报告确认。

相机通信只在 PATH 上的 adb 进程边界替换；生产登记、响应解释、
等待与文件适配均从 wheel 装载，不通过测试 launcher 注入驱动。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from camctl.contracts.enums import enum_for
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl_fixtures import Deployment
from test_camctl_c_module_roundtrip import _report_with_statuses


_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent
_PROJECT = _REPO / "apps/camctl"
_VIDEO_PATH = "/mnt/media_rw/emulated/DCIM/DJI_001/DJI_20000101080811_0001_D.MP4"
_PREVIEW_PATH = _VIDEO_PATH.removesuffix(".MP4") + ".LRF"
_OLD_PATH = "/mnt/media_rw/emulated/DCIM/DJI_001/DJI_OLD.MP4"

# 固定的有效 H.264/MP4：16×16、10 秒；只作为软件文件字节样例。
# 它的媒体属性不充当真实 Action6 的 4K/30 fps 或时长证据。
_MP4_BASE64 = """
AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAOxbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAJxAAAQAAAQAA
AAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAA
Att0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAJxAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAA
AAAAAAAAAAAAAABAAAAAABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAACcQAACAAAABAAAAAAJTbWRpYQAAACBtZGhk
AAAAAAAAAAAAAAAAAABAAAACgABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAB/m1p
bmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAb5zdGJsAAAAvnN0c2QA
AAAAAAAAAQAAAK5hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABFUxhdmM2MC4zMS4xMDIgbGli
eDI2NAAAAAAAAAAAAAAAGP//AAAANGF2Y0MBZAAK/+EAF2dkAAqs2V7ARAAAAwAEAAADAAg8SJZYAQAGaOvjyyLA/fj4AAAAABBw
YXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAAKYAAACmAAAABhzdHRzAAAAAAAAAAEAAAAKAABAAAAAABRzdHNzAAAAAAAAAAEAAAAB
AAAAYGN0dHMAAAAAAAAACgAAAAEAAIAAAAAAAQABQAAAAAABAACAAAAAAAEAAAAAAAAAAQAAQAAAAAABAAFAAAAAAAEAAIAAAAAA
AQAAAAAAAAABAABAAAAAAAEAAIAAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAKAAAAAQAAADxzdHN6AAAAAAAAAAAAAAAKAAACxQAA
AAwAAAAMAAAADAAAAAwAAAASAAAADgAAAAwAAAAMAAAAEgAAABRzdGNvAAAAAAAAAAEAAAPhAAAAYnVkdGEAAABabWV0YQAAAAAA
AAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2MC4xNi4x
MDAAAAAIZnJlZQAAA0dtZGF0AAACrQYF//+p3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzEwOCAzMWUxOWY5IC0g
SC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQu
aHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9
NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhk
Y3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTEgbG9va2Fo
ZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0w
IGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWln
aHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTEgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVz
aD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFw
c3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAQZYiEABf//vfUt8yy7gcjgQAAAAhBmiRsQX/+8AAAAAhBnkJ4gt+M
gQAAAAgBnmF0QV+SgAAAAAgBnmNqQV+SgQAAAA5BmmhJqEFomUwILf/+8QAAAApBnoZFESwW/4yBAAAACAGepXRBX5KBAAAACAGe
p2pBX5KAAAAADkGaqUmoQWyZTAgr//7w
"""


def _run(command, *, cwd, env, timeout_s=180):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=timeout_s)
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return result


@dataclass(frozen=True)
class _Package:
    python: Path
    camctl: Path
    environment: dict[str, str]


@pytest.fixture(scope="module")
def action6_package(tmp_path_factory):
    """安装真实 wheel 与锁定运行依赖，不携带测试装配桥。"""
    root = tmp_path_factory.mktemp("action6-builtin-package")
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in ("PYTHONPATH", "PYTHONHOME") or name.startswith("CAMCTL_TEST_"):
            environment.pop(name)
    dist = root / "dist"
    _run(["uv", "build", "--project", str(_PROJECT), "--wheel", "--out-dir", str(dist)],
         cwd=root, env=environment)
    wheels = list(dist.glob("camctl-*.whl"))
    assert len(wheels) == 1, wheels
    requirements = root / "requirements.txt"
    _run(["uv", "export", "--project", str(_PROJECT), "--frozen", "--no-dev",
          "--no-emit-project", "-o", str(requirements)], cwd=root, env=environment)
    venv = root / "venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=root, env=environment)
    python = venv / "bin/python"
    _run(["uv", "pip", "install", "--python", str(python), "--require-hashes",
          "-r", str(requirements)], cwd=root, env=environment)
    _run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])],
         cwd=root, env=environment)
    return _Package(python, venv / "bin/camctl", environment)


def _calls(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _results(state_db, action_id, kind):
    return query_state(state_db,
        "SELECT t.result_event_id,h.occurred_at,t.result_json FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id JOIN history_events h ON h.id=t.result_event_id"
        " WHERE r.action_id=? AND r.kind=? ORDER BY t.id", (action_id, int(kind)))


def _action(report, name):
    actions = [action for plan in report["plans"] for action in plan["actions"] if action["name"] == name]
    assert len(actions) == 1, actions
    return actions[0]


@pytest.mark.skipif(os.name != "posix", reason="真实内建 ADB 进程与安装版 C host 链在 Linux 验证")
def test_installed_builtin_action6_capture_obtain_claim_and_ack(
    tmp_path, host_demo, action6_package,
):
    """内建登记未启用、停止后提前登记或副本/ACK交接断裂均会失败。"""
    package = action6_package
    deployment = Deployment(tmp_path / "deployment-world", devices=False)
    environment = dict(package.environment)
    remote_root = deployment.root / "camera-files"
    old = remote_root / _OLD_PATH.lstrip("/")
    old.parent.mkdir(parents=True)
    content = base64.b64decode(_MP4_BASE64)
    old.write_bytes(content)
    (deployment.root / "sample.mp4").write_bytes(content)
    tools = deployment.root / "tools"
    tools.mkdir()
    shutil.copyfile(_ROOT / "_action6_builtin_adb.py", tools / "action6-adb.py")
    adb = tools / "adb"
    adb.write_text("#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
                   + shlex.quote(str(tools / "action6-adb.py")) + ' "$@"\n')
    adb.chmod(0o755)
    environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
    environment["CAMCTL_ACTION6_ADB_ROOT"] = str(remote_root)

    # 提取包内原始模板并检查导入来自独立安装环境；生成文件另选名称。
    extracted = _run([str(package.python), "-c", (
        "from pathlib import Path; import json,camctl; "
        "from camctl.resources import available_resources,resource_bytes; "
        f"root=Path({str(deployment.root)!r}); prefix='examples/camera-demo/'; "
        "names=sorted(n for n in available_resources() if n.startswith(prefix)); "
        "[(root/n.removeprefix(prefix)).write_bytes(resource_bytes(n)) for n in names]; "
        "print(json.dumps({'origin':camctl.__file__,'names':names}))")],
        cwd=deployment.root, env=environment)
    package_info = json.loads(extracted.stdout)
    assert package.python.parent.parent in Path(package_info["origin"]).parents
    assert not Path(package_info["origin"]).is_relative_to(_REPO)
    template_config = (deployment.root / "config.toml").read_text()
    with deployment.config_path.open("a") as stream:
        stream.write("\n" + template_config.replace("REPLACE_ACTION6_SERIAL", "action6-test-serial"))
    initialized = _run([str(package.camctl), "init", "--config", str(deployment.config_path)],
                       cwd=deployment.root, env=environment)
    assert initialized.stdout == ""
    described = _run([str(package.camctl), "describe", "--config", str(deployment.config_path)],
                    cwd=deployment.root, env=environment)
    capabilities = json.loads(described.stdout)
    assert [(device["device_id"], device["driver_id"]) for device in capabilities["devices"]] == [
        ("action6", "dji-action6")]
    assert [action["type"] for action in capabilities["devices"][0]["actions"]] == ["camera_record"]
    capabilities_path = deployment.root / "capabilities.json"
    capabilities_path.write_text(described.stdout)
    client_store = deployment.root / "client-store"
    client_store.mkdir()
    (client_store / "device-capabilities.json").write_text(described.stdout)
    generator = deployment.root / "prepare-plan.py"
    capture_plan = deployment.root / "capture-plan.json"
    _run([str(package.python), str(generator), "action6-record", "--delay-s", "1",
          "--capabilities", str(capabilities_path), "--output", str(capture_plan)],
         cwd=deployment.root, env=environment)
    params = json.loads(capture_plan.read_text())["actions"][0]["params"]
    assert "aperture" not in params and "bitrate" not in params
    assert params["duration_s"] == 10
    calls_path = deployment.root / "adb-calls.jsonl"
    demo = HostDemo(deployment, host_demo, launcher=str(package.camctl))
    try:
        demo.start(env=environment)
        demo.submit(capture_plan)
        stop_call = wait_for(lambda: next((call for call in _calls(calls_path)
                            if call["kind"] == "stop"), None), 60, "没有取得正常 STOP 原始调用")
        stop_ns = stop_call["returned_ns"]
        # 文件在 START 时已存在也不能提前成功；至少检查停止后的前三秒。
        deadline = stop_ns + 3_000_000_000
        while time.monotonic_ns() < deadline:
            assert query_state(deployment.state_db, "SELECT id FROM outputs") == []
            time.sleep(0.02)
        report = wait_for(lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
                          60, "安装后的内建录像未成功或未发布报告")
        action_id = int(_action(report, "capture")["action_instance_id"])
        rows = query_state(deployment.state_db,
            "SELECT effective_params_json,execution_spec_json FROM actions WHERE id=?", (action_id,))
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == params
        execution = json.loads(rows[0][1])
        assert execution["target_duration_ms"] == 10000
        assert execution["file_completion_wait_ms"] == 5000
        assert execution["output_scope"] == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
        assert "aperture" not in json.loads(rows[0][0]) and "bitrate" not in json.loads(rows[0][0])
        stop_results = _results(deployment.state_db, action_id, enum_for("operation_runs.kind").STOP)
        assert len(stop_results) == 1
        stop_event, stop_returned_at_us, stop_document = stop_results[0]
        stop_document = json.loads(stop_document)
        assert [item["type"] for item in stop_document["observations"]] == ["stop_confirmed"]
        activity_rows = query_state(deployment.state_db,
            "SELECT id,baseline_state,baseline_first_event_id,baseline_last_event_id FROM device_activities"
            " WHERE action_id=?", (action_id,))
        assert len(activity_rows) == 1 and activity_rows[0][2:] != (None, None)
        activity_id = activity_rows[0][0]
        result_rows = _results(deployment.state_db, action_id, enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)
        assert result_rows
        completed = []
        for _, _, encoded in result_rows:
            document = json.loads(encoded)
            wait = document["settlement"]["evidence"]["data"]["file_completion"]
            assert wait["method"] == "stop_return_and_wait" and type(wait["version"]) is int and wait["version"] == 1
            assert wait["activity_id"] == str(activity_id)
            assert type(wait["stop_result_event_id"]) is int and wait["stop_result_event_id"] == stop_event
            assert type(wait["required_wait_ms"]) is int and wait["required_wait_ms"] == 5000
            assert type(wait["observed_wait_ns"]) is int and type(wait["completed"]) is bool
            if wait["completed"]:
                assert wait["observed_wait_ns"] >= 5_000_000_000
                completed.append(wait)
            for observation in document["observations"]:
                assert observation["type"] == "result_files_listed" and observation["version"] == 2
                assert observation["data"]["completion_evidence"] is None
        assert completed
        page_rows = query_state(deployment.state_db,
            "SELECT id,body_json FROM history_events"
            " WHERE json_extract(body_json,'$.evidence.result_page.activity_id')=? ORDER BY id", (activity_id,))
        saved_pages = {event_id: json.loads(body)["evidence"]["result_page"] for event_id, body in page_rows}
        assert saved_pages
        products = query_state(deployment.state_db,
            "SELECT f.locator_json,f.size_bytes,f.completion_evidence_json FROM outputs o JOIN device_files f ON f.id=o.device_file_id"
            " WHERE o.source_action_id=?", (action_id,))
        assert [(json.loads(locator)["path"], size) for locator, size, _ in products] == [(_VIDEO_PATH, len(content))]
        evidence = json.loads(products[0][2])
        assert set(evidence) == {"basis", "observation", "activity_id", "result_page_event_id", "stop_result_event_id"}
        assert type(evidence["basis"]) is int and evidence["basis"] == 3
        assert type(evidence["activity_id"]) is int and evidence["activity_id"] == activity_id
        assert type(evidence["stop_result_event_id"]) is int and evidence["stop_result_event_id"] == stop_event
        assert type(evidence["result_page_event_id"]) is int
        current_page_id = evidence["result_page_event_id"]
        current_page = saved_pages[current_page_id]
        current_result = current_page["outcome"]["result"]
        current_data = current_result["settlement"]["evidence"]["data"]
        source_id = current_data["file_completion_source_page_event_id"]
        actual_wait_page_id = current_page_id if source_id is None else source_id
        assert type(actual_wait_page_id) is int and stop_event < actual_wait_page_id <= current_page_id
        if source_id is not None:
            assert source_id < current_page_id
        actual_wait_page = saved_pages[actual_wait_page_id]
        actual_wait_data = actual_wait_page["outcome"]["result"]["settlement"]["evidence"]["data"]
        assert actual_wait_data["file_completion_source_page_event_id"] is None
        assert actual_wait_page["activity_id"] == activity_id
        assert actual_wait_data["file_completion"] == current_data["file_completion"]
        assert actual_wait_data["file_completion"] in completed
        assert evidence["observation"] in [entry for observation in current_result["observations"]
            if observation["type"] == "result_files_listed" for entry in observation["data"]["entries"]]
        calls = _calls(calls_path)
        assert sum(call["kind"] == "start" for call in calls) == 1
        assert sum(call["kind"] == "stop" for call in calls) == 1
        assert not any(call.get("code") == "26" or "simulate_device" in call["script"] for call in calls)
        directory_after_stop = [call for call in calls if call["kind"] == "file"
                                and "find " in call["script"] and call["started_ns"] > stop_ns]
        assert directory_after_stop and directory_after_stop[0]["started_ns"] - stop_ns >= 5_000_000_000
        assert stop_returned_at_us > 0
        demo.claim()
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [report["report_id"]]
        obtain_plan = deployment.root / "obtain-plan.json"
        _run([str(package.python), str(generator), "obtain", "--delay-s", "1",
              "--source-action-id", str(action_id), "--last-report-id", report["report_id"],
              "--output", str(obtain_plan)], cwd=deployment.root, env=environment)
        demo.submit(obtain_plan)
        obtained = wait_for(lambda: _report_with_statuses(deployment.ready, {"obtain": "succeeded"}),
                            60, "内建驱动的独立取回未成功")
        delivered = [path for path in deployment.ready.iterdir()
                     if path.is_file() and not path.name.startswith("status-report-")]
        assert len(delivered) == 1 and delivered[0].read_bytes() == content
        checksum = hashlib.sha256(content).hexdigest()
        copy_rows = query_state(deployment.state_db,
            "SELECT source_size,source_sha256,committed_bytes,target_sha256 FROM file_copies")
        assert copy_rows == [(len(content), checksum, len(content), checksum)]
        delivery = _action(obtained, "obtain")["deliveries"][0]
        assert delivery["source_action_instance_id"] == str(action_id)
        assert (delivery["file_name"], delivery["size"], delivery["sha256"]) == (
            delivered[0].name, len(content), checksum)
        demo.claim()
        claimed = deployment.processing / delivered[0].name
        assert claimed.read_bytes() == content
        assert hashlib.sha256(claimed.read_bytes()).hexdigest() == checksum
        saved = deployment.import_reports_with_client(deployment.processing)
        assert obtained["report_id"] in saved["saved_report_ids"]
        ack_plan = deployment.root / "report-ack-plan.json"
        _run([str(package.python), str(generator), "report-ack", "--last-report-id", obtained["report_id"],
              "--output", str(ack_plan)], cwd=deployment.root, env=environment)
        demo.submit(ack_plan)
        wait_for(lambda: query_state(deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state")
            == [(int(obtained["report_id"]), obtained["to_wm"])], 60, "领取报告的累计确认未吸收")
        assert (remote_root / _VIDEO_PATH.lstrip("/")).read_bytes() == content
        assert (remote_root / _PREVIEW_PATH.lstrip("/")).is_file()
        assert old.read_bytes() == content
        calls = _calls(calls_path)
        assert sum(call["script"].startswith("dd ") for call in calls) == 1
        assert any("sha256sum" in call["script"] for call in calls)
    finally:
        demo.stop()
