"""安装 wheel 的分段来源经真实 C host、客户端和累计 ACK 交付。"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

import pytest
import test_osmo_builtin_timelapse_roundtrip as osmo_roundtrip
from camctl_memory_plugin import finalize_receipts

from _client_session import ClientSession
from _osmo_builtin_clock import format_utc, read_clock, set_running
from _osmo_copy_observation import assert_copy_resumed_before_data
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl_fixtures import Deployment
from test_camctl_c_module_roundtrip import _report_with_statuses
from test_osmo_builtin_timelapse_roundtrip import (
    _action,
    _capture_body,
    _capture_params,
    _describe,
    _install_osmo_process_tools,
    _osmo_config,
    _records,
    _run,
    _assert_cli_process_provenance,
    _assert_adb_process_provenance,
)

osmo_package = osmo_roundtrip.osmo_package


def _report_or_error(deployment, statuses):
    report = _report_with_statuses(deployment.ready, statuses)
    if report is None:
        log = (deployment.root / "module.log").read_text(encoding="utf-8")
        assert "result=error" not in log, log[-6000:]
    return report


def _resume_marker(demo, deployment, host_demo, camctl, environment, marker, request):
    wait_for(marker.is_file, 150, "安装版提交同步点尚未到达")
    observed = json.loads(marker.read_text(encoding="utf-8"))
    assert observed["key"] and observed["pid"] > 0
    demo.stop()
    for table, columns, key in (
        ("outputs", "id,source_action_id,device_file_id,kind", "outputs"),
        ("segment_scope_members", "id,set_id,ordinal,output_id", "scope"),
        ("obtain_items", "id,selection_id,requested_output_id,output_id", "items"),
    ):
        assert [
            list(row)
            for row in query_state(
                deployment.state_db, f"SELECT {columns} FROM {table} ORDER BY id"
            )
        ] == observed[key]
    demo = HostDemo(deployment, host_demo, launcher=str(camctl))
    demo.start(env=environment)
    demo.submit(request)
    return demo, observed


@pytest.mark.skipif(os.name != "posix", reason="安装版 C host 链在 Linux 验证")
@pytest.mark.parametrize(
    "scenario,indexes,default_count,status",
    [
        ("normal", (7, 8), 2, "succeeded"),
        ("overrun", (7, 8, 9, 10, 11, 12), 3, "succeeded"),
        ("short-set", (7, 8), 2, "succeeded"),
        ("gap", (7, 8, 10), 2, "failed"),
        ("zero-gap", (7, 8, 9, 10), 2, "failed"),
        ("empty", (7, 8, 9), 0, "failed"),
        ("unknown", (7,), 0, "failed"),
        ("native-tail", (7, 8, 9), 2, "succeeded"),
    ],
)
def test_installed_segment_source_default_explicit_delivery_and_ack(
    tmp_path,
    host_demo,
    osmo_package,
    monkeypatch,
    scenario,
    indexes,
    default_count,
    status,
):
    package = osmo_package
    deployment = Deployment(tmp_path / "world", devices=False)
    _osmo_config(deployment.config_path, result_mode="segmented")
    native = scenario == "native-tail"
    action_type = "camera_timelapse" if native else "camera_record"
    params = _capture_params(action_type)
    if not native:
        params["duration_s"] = 12
    scratch = package.root / "runtime" / scenario
    environment, camctl, clock_file, remote = _install_osmo_process_tools(
        package, deployment, action_type=action_type, params=params, scratch=scratch
    )
    entries = []
    contents = {}
    for index in reversed(indexes):
        for suffix in ("OSV", "LRF"):
            name = (
                f"CAM_20261022100000_{index:04d}_D.{suffix}"
                if scenario != "unknown"
                else f"UNKNOWN.{suffix}"
            )
            path = f"/mnt/media_rw/emulated/DCIM/CAM_001/{name}"
            # 独立身份特意使用相同内容，核对每份交付都继续保留。
            content = (
                b"osmo-independent-segment\x00" * 8000
                if suffix == "OSV"
                else b"preview\x00" * 2000
            )
            if scenario == "short-set" and index == 8:
                content = b"whole-tail-content\x00"
            if ((scenario == "empty" and index == 7)
                    or (scenario == "zero-gap" and index == 9 and suffix == "OSV")
                    or (native and index == 9)):
                content = b""
            sample = scratch / name
            sample.write_bytes(content)
            entries.append({"path": path, "source": str(sample)})
            contents[path] = content
    manifest = scratch / "files.json"
    manifest.write_text(json.dumps(entries), encoding="utf-8")
    environment["CAMCTL_OSMO_TEST_FILES"] = str(manifest)
    stage = {
        "normal": "scan",
        "overrun": "finish",
        "gap": "selection",
        "empty": "report",
    }.get(scenario)
    marker = scratch / "segment-marker.json"
    if stage:
        shutil.copyfile(
            Path(__file__).with_name("_osmo_segment_observer.py"),
            scratch / "tools" / "_osmo_segment_observer.py",
        )
        environment.update(
            CAMCTL_OSMO_TEST_SEGMENT_FAULT=stage,
            CAMCTL_OSMO_TEST_SEGMENT_MARKER=str(marker),
        )
    initial = read_clock(clock_file)
    _run(
        [str(camctl), "init", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=environment,
        log_dir=package.log_dir,
        camctl=True,
    )
    described = _describe(
        [str(camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=environment,
        log_dir=package.log_dir,
    )
    store = deployment.root / "client-store"
    store.mkdir()
    (store / "device-capabilities.json").write_text(
        json.dumps(described), encoding="utf-8"
    )
    client = None
    demo = None
    try:
        client = ClientSession(
            ["node", "--import", "tsx", str(Path(__file__).with_name("client_driver.ts"))],
            cwd=Path(__file__).resolve().parents[2] / "apps/client",
            env=package.environment,
            root=deployment.root,
            timeout=120,
        )
        receipts = deployment.root / "client-receipts"
        receipts.mkdir()
        receipt_number = 0

        def client_driver(*args, output):
            nonlocal receipt_number
            receipt_number += 1
            return client.call(
                *args,
                output=receipts / f"{receipt_number:03d}-{output.name}",
            )

        deployment.run_client_driver = client_driver
        plan, _ = deployment.export_plan_with_client(
            _capture_body(action_type, params, scheduled_at=format_utc(initial["utc_us"]))
        )
        demo = HostDemo(deployment, host_demo, launcher=str(camctl))
        failure = demo.watch_failures()
        monkeypatch.chdir(package.root)
        demo.start(env=environment)
        demo.submit(plan)
        wait_for(
            lambda: query_state(
                deployment.state_db,
                "SELECT t.id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.kind=1 AND t.result_event_id IS NOT NULL",
            ),
            90,
            "START 尚未可靠保存",
            failure=failure,
        )
        if scenario == "overrun":
            demo.stop()
            demo = HostDemo(deployment, host_demo, launcher=str(camctl))
            demo.start(env=environment)
            demo.submit(plan)
        release = 330 if native else 24 if scenario == "short-set" else 22 if scenario == "overrun" else 12
        set_running(
            clock_file,
            utc_us=initial["utc_us"] + release * 1000000,
            monotonic_ns=initial["monotonic_ns"] + release * 1000000000,
        )
        captured = None
        if stage in ("scan", "finish", "report"):
            demo, captured = _resume_marker(
                demo, deployment, host_demo, camctl, environment, marker, plan
            )
        report = wait_for(
            lambda: _report_or_error(deployment, {"capture": status}),
            150,
            "分段拍摄未产生预期报告",
        )
        source = _action(report, "capture")
        aid = int(source["action_instance_id"])
        outputs = source["outputs"]
        originals = [output for output in outputs if output["kind"] == "original"]
        retained = len(indexes) - (1 if scenario in ("empty", "zero-gap", "native-tail") else 0)
        assert len(originals) == retained and len(outputs) == retained * 2
        source_rows = query_state(
            deployment.state_db,
            "SELECT o.id,o.kind,f.locator_json FROM outputs o JOIN device_files f ON f.id=o.device_file_id ORDER BY o.id",
        )
        paths = {
            str(identity): json.loads(locator)["path"]
            for identity, _, locator in source_rows
        }
        original_ids = [str(identity) for identity, kind, _ in source_rows if kind == 1]
        preview_ids = [str(identity) for identity, kind, _ in source_rows if kind == 3]
        assert (
            len(query_state(deployment.state_db, "SELECT id FROM output_origins"))
            == retained
        )
        assert query_state(deployment.state_db, "SELECT COUNT(*) FROM file_copies") == [
            (0,)
        ]
        assert query_state(
            deployment.state_db, "SELECT COUNT(*) FROM intermediate_files"
        ) == [(0,)]
        assert query_state(
            deployment.state_db,
            "SELECT COUNT(*) FROM segment_sets WHERE published_event_id IS NOT NULL",
        ) == [(1,)]
        if scenario == "short-set":
            saved_assessment = query_state(deployment.state_db,
                "SELECT assessment_json FROM segment_sets WHERE published_event_id IS NOT NULL")[0][0]
            assert json.loads(saved_assessment)["overrecord"] == 1
        if scenario in ("gap", "zero-gap"):
            assert source["error"]["code"] == "capture_segment_gap"
            assert (
                source["error"]["details"]["default_scope"]["output_ids"]
                == original_ids[:2]
            )
            assert source["error"]["details"]["gaps"][0]["start_key"]["index"] == 9
            assert source["error"]["details"]["gaps"][0]["kind"] == (
                "zero_byte_non_tail" if scenario == "zero-gap" else "missing_position"
            )
        adb_log = Path(environment["CAMCTL_OSMO_TEST_ADB_LOG"])
        assert len([call for call in _records(adb_log) if call["kind"] == "start"]) == 1
        assert len([call for call in _records(adb_log) if call["kind"] == "stop"]) == (
            0 if native else 1
        )
        demo.claim()
        deployment.import_reports_with_client(deployment.processing)
        all_deliveries = []
        latest = report
        requests = [
            ("original", {"filter": "default"}, original_ids[:default_count]),
            ("preview", {"filter": "preview"}, preview_ids[:default_count]),
            ("explicit", {"output_ids": original_ids[-1:]}, original_ids[-1:]),
        ]
        for name, selection, expected in requests:
            instant = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            request, _ = deployment.export_plan_with_client(
                {
                    "name": name,
                    "actions": [
                        {
                            "name": name,
                            "type": "obtain_action_outputs",
                            "scheduled_at": instant,
                            "params": {
                                "source": {"action_instance_id": str(aid)},
                                **selection,
                            },
                        }
                    ],
                }
            )
            copy_gate = Path(environment["CAMCTL_OSMO_TEST_READ_GATE"])
            if scenario == "native-tail" and name == "original":
                copy_gate.unlink()
                read_log_start = len(_records(adb_log))
            demo.submit(request)
            if stage == "selection" and name == "original":
                demo, observed = _resume_marker(
                    demo, deployment, host_demo, camctl, environment, marker, request
                )
                assert observed["items"]
            if scenario == "native-tail" and name == "original":
                interrupted_read = wait_for(
                    lambda: next((
                        call for call in _records(adb_log)[read_log_start:]
                        if call["kind"] == "read_blocked"
                        and call.get("forwarded_bytes", 0) >= 65536
                    ), None),
                    90,
                    "取回尚未到达实际数据块同步点",
                )
                saved_copy = wait_for(
                    lambda: query_state(
                        deployment.state_db,
                        "SELECT id,delivery_id,committed_bytes FROM file_copies WHERE committed_bytes>=65536",
                    ),
                    90,
                    "读取尚未提交第一块进度",
                )
                assert len(saved_copy) == 1
                request_id = int(json.loads(request.read_text(encoding="utf-8"))["request_id"])
                delivery_query = (
                    "SELECT d.id,d.output_id FROM deliveries d"
                    " JOIN actions a ON a.id=d.action_id JOIN plans p ON p.id=a.plan_id"
                    " WHERE p.request_id=? ORDER BY d.id"
                )
                saved_deliveries = query_state(
                    deployment.state_db, delivery_query, (request_id,)
                )
                assert saved_copy[0][1] in [identity for identity, _ in saved_deliveries]
                demo.stop()
                assert (
                    query_state(
                        deployment.state_db,
                        "SELECT id,delivery_id,committed_bytes FROM file_copies WHERE committed_bytes>=65536",
                    )
                    == saved_copy
                )
                assert not any(
                    call["kind"] == "read"
                    and call["started_ns"] == interrupted_read["started_ns"]
                    for call in _records(adb_log)
                )
                # 恢复的真实读取先暂停，再核对原身份和可靠偏移。
                environment["CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES"] = "0"
                demo = HostDemo(deployment, host_demo, launcher=str(camctl))
                demo.start(env=environment)
                resumed_failure = demo.watch_failures(
                    request_id=request_id, expected_statuses={name: "SUCCEEDED"}
                )
                demo.submit(request)
                resumed_read = wait_for(
                    lambda: next((
                        call for call in _records(adb_log)
                        if call["kind"] == "read_started"
                        and call["started_ns"] > interrupted_read["started_ns"]
                    ), None),
                    120,
                    "原请求恢复尚未开始读取原片",
                    failure=resumed_failure,
                )
                resumed_gate = wait_for(
                    lambda: next((
                        call for call in _records(adb_log)
                        if call["kind"] == "read_blocked"
                        and call["started_ns"] == resumed_read["started_ns"]
                    ), None),
                    30,
                    "恢复读取尚未在转发新字节前暂停",
                    failure=resumed_failure,
                )
                assert_copy_resumed_before_data(
                    interrupted_read=interrupted_read,
                    resumed_read=resumed_read,
                    resumed_gate=resumed_gate,
                    saved_copy=saved_copy[0],
                    resumed_copies=query_state(
                        deployment.state_db,
                        "SELECT id,delivery_id,committed_bytes FROM file_copies WHERE delivery_id=?",
                        (saved_copy[0][1],),
                    ),
                    saved_deliveries=saved_deliveries,
                    resumed_deliveries=query_state(
                        deployment.state_db, delivery_query, (request_id,)
                    ),
                )
                copy_gate.touch()
            expected_status = "succeeded" if expected else "failed"
            latest = wait_for(
                lambda: _report_or_error(deployment, {name: expected_status}),
                120,
                "分段取回尚未完成",
            )
            obtained = _action(latest, name)
            deliveries = obtained.get("deliveries", [])
            assert [delivery["output_id"] for delivery in deliveries] == expected
            if scenario == "native-tail" and name == "original":
                assert [
                    (int(delivery["delivery_id"]), int(delivery["output_id"]))
                    for delivery in deliveries
                ] == saved_deliveries
                assert query_state(
                    deployment.state_db,
                    "SELECT id,delivery_id FROM file_copies WHERE delivery_id=?",
                    (saved_copy[0][1],),
                ) == [saved_copy[0][:2]]
            if not expected:
                failure = obtained["result"]["failures"][0]["error"]
                assert failure["code"] == (
                    "default_segment_scope_empty"
                    if scenario == "empty"
                    else "default_segment_scope_unconfirmed"
                )
            demo.claim()
            saved = deployment.import_reports_with_client(deployment.processing)
            assert latest["report_id"] in saved["saved_report_ids"]
            # 报告已完整应用；媒体留待下方逐份导入，先发送累计 ACK。
            ack, receipt = deployment.export_plan_with_client(
                {
                    "name": "ack",
                    "actions": [
                        {
                            "name": "ack",
                            "type": "report_status",
                            "params": {"scope": "full"},
                        }
                    ],
                }
            )
            assert receipt["last_report_id"] == latest["report_id"]
            demo.submit(ack)
            wait_for(
                lambda: (
                    query_state(
                        deployment.state_db,
                        "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state",
                    )
                    == [(int(latest["report_id"]), latest["to_wm"])]
                ),
                90,
                "报告覆盖尚未吸收 ACK",
            )
            for delivery in reversed(deliveries):
                row = query_state(
                    deployment.state_db,
                    "SELECT file_name FROM deliveries WHERE id=?",
                    (int(delivery["delivery_id"]),),
                )[0]
                file = deployment.processing / row[0]
                content = contents[paths[delivery["output_id"]]]
                assert file.read_bytes() == content
                assert (
                    delivery["size"] == len(content)
                    and delivery["sha256"] == hashlib.sha256(content).hexdigest()
                )
                media = deployment.run_client_driver(
                    "import-media",
                    str(store),
                    str(file),
                    output=deployment.root / f"media-{delivery['delivery_id']}.json",
                )
                assert media["delivery_id"] == delivery["delivery_id"]
                assert media["output_id"] == delivery["output_id"]
                assert media["video"]["status"] == "verified"
                all_deliveries.append(delivery["delivery_id"])
        assert len(set(all_deliveries)) == len(all_deliveries)
        if captured is not None and stage == "finish":
            assert [
                list(row)
                for row in query_state(
                    deployment.state_db,
                    "SELECT id,source_action_id,device_file_id,kind FROM outputs ORDER BY id",
                )
            ] == captured["outputs"]
        if captured is not None and stage == "report":
            original = next(
                row
                for row in captured["reports"]
                if row[0] == captured["request"]["report_id"]
            )
            assert query_state(
                deployment.state_db,
                "SELECT id,frozen_event_id,size_bytes,sha256 FROM reports WHERE id=?",
                (original[0],),
            ) == [tuple(original)]
        before = query_state(
            deployment.state_db, "SELECT id,output_id FROM deliveries ORDER BY id"
        )
        log_start = len(demo.module_log())
        demo.submit(request)
        submitted = wait_for(
            lambda: re.search(
                rf"call=(\d+) submit accepted path={re.escape(str(request))}(?:\n|$)",
                demo.module_log()[log_start:],
            ),
            30,
            "原请求重送尚未由 host 接收",
        )
        wait_for(
            lambda: re.search(
                rf"call={submitted[1]} command=submit .*result=success .*exit=0",
                demo.module_log()[log_start:],
            ),
            90,
            "原请求重送尚未成功完成",
        )
        assert (
            query_state(
                deployment.state_db, "SELECT id,output_id FROM deliveries ORDER BY id"
            )
            == before
        )
        assert len([call for call in _records(adb_log) if call["kind"] == "start"]) == 1
        assert all(
            (remote / path.lstrip("/")).read_bytes() == content
            for path, content in contents.items()
        )
        _assert_cli_process_provenance(
            package, Path(environment["CAMCTL_OSMO_TEST_PROCESS_LOG"])
        )
        _assert_adb_process_provenance(package, adb_log)
    finally:
        primary = sys.exception()
        errors = []
        if demo is not None:
            try:
                demo.stop()
            except BaseException as error:
                errors.append(error)
        if client is not None:
            try:
                client.close()
            except BaseException as error:
                errors.append(error)
        try:
            finalize_receipts(deployment.root)
        except BaseException as error:
            errors.append(error)
        try:
            archive = osmo_roundtrip._archive_logs(package.root, package.log_dir)
            shutil.copytree(
                deployment.root, archive / "cases" / scenario, dirs_exist_ok=True
            )
        except BaseException as error:
            errors.append(error)
        if errors:
            raise BaseExceptionGroup(
                "OSMO 分段世界操作或收场失败",
                ([primary] if primary is not None else []) + errors,
            )
