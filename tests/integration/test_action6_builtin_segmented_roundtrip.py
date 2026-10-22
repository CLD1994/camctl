"""安装版 Action6 分段结果经 CLI、SQLite、C host 与客户端完整交接。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from decimal import Decimal, ROUND_FLOOR

import pytest

from _wsl_host_demo import HostDemo, query_state, wait_for
from _action6_segmented_files import create_segmented_file_manifest
from _osmo_builtin_clock import format_utc, read_clock, set_running
from camctl.contracts.enums import enum_for
from camctl_fixtures import Deployment
from test_action6_builtin_demo_roundtrip import action6_package, _run
from test_action6_builtin_timelapse_roundtrip import _tools
from test_camctl_c_module_roundtrip import _report_with_statuses
from test_osmo_builtin_timelapse_roundtrip import _action, _records


@pytest.mark.skipif(os.name != "posix", reason="安装版 POSIX C host 联合链")
@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "native-timelapse"])
def test_installed_action6_segmented_default_scope_obtain_and_client_ack(
    tmp_path, host_demo, action6_package, monkeypatch, native,
):
    package = action6_package
    deployment = Deployment(tmp_path / ("action6-native" if native else "action6-record"),
                            devices=False)
    resources_result = _run(
        [str(package.python), "-c", (
            "import json; from camctl.resources import resource_bytes; "
            "print(json.dumps({name:resource_bytes('examples/camera-demo/'+name).decode() "
            "for name in ('config.toml','action6-record.json','action6-timelapse.json')}))"
        )],
        cwd=deployment.root,
        env=package.environment,
    )
    resources = json.loads(resources_result.stdout)
    template = json.loads(resources["action6-timelapse.json"] if native
                          else resources["action6-record.json"])
    params = dict(template["actions"][0]["params"])
    if native:
        params["outputs"] = "video_jpeg"

    with deployment.config_path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + resources["config.toml"].replace(
            "REPLACE_ACTION6_SERIAL", "action6-test-serial"))
        stream.write('\n[devices.action6.video]\nresult_mode = "segmented"\n')

    environment, camctl, clock_file, remote, _ = _tools(package, deployment, params)
    if not native:
        environment["CAMCTL_ACTION6_TEST_CAPTURE"] = "record"
    manifest, contents = create_segmented_file_manifest(tmp_path, include_jpeg=native)
    environment["CAMCTL_ACTION6_TEST_FILES"] = str(manifest)
    initial = read_clock(clock_file)
    adb_log = deployment.root / "adb-calls.jsonl"

    for command in ("init", "describe"):
        completed = _run(
            [str(camctl), command, "--config", str(deployment.config_path)],
            cwd=deployment.root,
            env=environment,
        )
    origin_result = _run(
        [str(package.python), "-c", "import camctl; print(camctl.__file__)"],
        cwd=deployment.root,
        env=environment,
    )
    package_origin = Path(origin_result.stdout.strip())
    assert package.python.parent.parent in package_origin.parents
    store = deployment.root / "client-store"
    store.mkdir()
    (store / "device-capabilities.json").write_text(
        completed.stdout, encoding="utf-8")
    action_type = "camera_timelapse" if native else "camera_record"
    body = {
        "name": "Action6分段原生延时" if native else "Action6分段普通录像",
        "actions": [{
            "name": "capture",
            "type": action_type,
            "device_id": "action6",
            "scheduled_at": format_utc(initial["utc_us"]),
            "params": params,
            "policy": {"max_delay_ms": 30000},
        }],
    }
    plan, _ = deployment.export_plan_with_client(body)
    demo = HostDemo(deployment, host_demo, launcher=str(camctl))
    monkeypatch.chdir(deployment.root)
    try:
        demo.start(env=environment)
        demo.submit(plan)
        started = wait_for(
            lambda: query_state(
                deployment.state_db,
                "SELECT a.id FROM actions a JOIN operation_runs r ON r.action_id=a.id "
                "JOIN operation_attempts t ON t.run_id=r.id "
                "WHERE r.kind=1 AND t.result_event_id IS NOT NULL",
            ),
            90,
            "Action6 START 未可靠保存",
        )
        assert len(started) == 1
        action_id = started[0][0]
        release_s = 330 if native else 11
        set_running(
            clock_file,
            utc_us=initial["utc_us"] + release_s * 1_000_000,
            monotonic_ns=initial["monotonic_ns"] + release_s * 1_000_000_000,
        )
        report = wait_for(
            lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
            180,
            "Action6 分段拍摄未发布成功来源报告",
        )
        source = _action(report, "capture")
        assert int(source["action_instance_id"]) == action_id
        coverage = source["result"]["coverage"]

        rows = query_state(
            deployment.state_db,
            "SELECT f.id,json_extract(f.locator_json,'$.path'),f.original_device_file_id,"
            "o.id,o.kind FROM device_files f JOIN outputs o ON o.device_file_id=f.id "
            "WHERE o.source_action_id=? ORDER BY o.id",
            (action_id,),
        )
        by_path = {row[1]: row for row in rows}
        assert set(by_path) == set(contents)
        assert {str(row[3]) for row in rows} == {
            item["output_id"] for item in source["outputs"]
        }
        original_kind = int(enum_for("outputs.kind").ORIGINAL)
        preview_kind = int(enum_for("outputs.kind").PREVIEW)
        original_ids = {
            str(row[3]) for row in rows if row[4] == original_kind
        }
        preview_ids = {
            str(row[3]) for row in rows if row[4] == preview_kind
        }
        video_ids = {
            str(by_path[path][3]) for path in contents if path.endswith(".MP4")
        }
        preview_paths = {path for path in contents if path.endswith(".LRF")}
        photo_paths = {path for path in contents if path.endswith(".JPG")}
        assert len(video_ids) == 2 and len(preview_ids) == 2
        assert original_ids == video_ids | {str(by_path[path][3]) for path in photo_paths}
        assert preview_ids == {str(by_path[path][3]) for path in preview_paths}
        for path in preview_paths:
            paired_video = path.removesuffix(".LRF") + ".MP4"
            assert by_path[path][2] == by_path[paired_video][0]
        for path in photo_paths:
            assert by_path[path][2] is None

        scope_ids = {
            str(row[0]) for row in query_state(
                deployment.state_db,
                "SELECT output_id FROM segment_scope_members WHERE output_id IS NOT NULL",
            )
        }
        assert scope_ids == video_ids
        assert coverage["metric"] == ("frames" if native else "duration_s")
        assert coverage["status"] == "not_performed"
        assert coverage["confirmed"] is None
        assert coverage["checked_output_ids"] == []
        assert coverage["default_scope"]["state"] == "nonempty"
        assert coverage["default_scope"]["member_count"] == 2
        assert set(coverage["default_scope"]["output_ids"]) == video_ids
        assert query_state(deployment.state_db, "SELECT COUNT(*) FROM file_copies") == [(0,)]
        assert query_state(
            deployment.state_db, "SELECT COUNT(*) FROM intermediate_files") == [(0,)]
        assessment_rows = query_state(
            deployment.state_db,
            "SELECT assessment_json FROM segment_sets WHERE published_event_id IS NOT NULL",
        )
        assert len(assessment_rows) == 1
        assessment = json.loads(assessment_rows[0][0])
        assert assessment["decision_type"] == "video_coverage_v1"
        assert assessment["coverage_status"] == "not_performed"
        assert assessment["checked_count"] == 0

        calls = _records(adb_log)
        assert len([call for call in calls if call.get("kind") == "start"]) == 1
        assert len([call for call in calls if call.get("kind") == "stop"]) == (
            0 if native else 1
        )
        if native:
            assert params["outputs"] == "video_jpeg"
            expected_frames = int((
                Decimal(str(params["duration_s"]))
                / Decimal(str(params["interval_s"]))
            ).to_integral_value(rounding=ROUND_FLOOR)) + 1
            assert int(assessment["target"]) == expected_frames
            assert coverage["target"] == expected_frames
        else:
            assert Decimal(str(coverage["target"])) == Decimal(str(params["duration_s"]))

        demo.claim()
        saved_source = deployment.import_reports_with_client(deployment.processing)
        assert report["report_id"] in saved_source["saved_report_ids"]
        selections = [
            ("obtain-default", {"filter": "default"}, original_ids),
            ("obtain-preview", {"filter": "preview"}, preview_ids),
        ]
        final_report = report
        all_delivery_ids = []
        delivery_evidence = []
        imported_report_ids = list(saved_source["saved_report_ids"])
        for name, selection, expected_ids in selections:
            instant = format_utc(read_clock(clock_file)["utc_us"])
            obtain_plan, _ = deployment.export_plan_with_client({
                "name": name,
                "actions": [{
                    "name": name,
                    "type": "obtain_action_outputs",
                    "scheduled_at": instant,
                    "params": {
                        "source": {"action_instance_id": str(action_id)},
                        "purpose": "manual",
                        **selection,
                    },
                }],
            })
            demo.submit(obtain_plan)
            final_report = wait_for(
                lambda action_name=name: _report_with_statuses(
                    deployment.ready, {action_name: "succeeded"}),
                120,
                f"{name} 未成功",
            )
            obtained = _action(final_report, name)
            deliveries = obtained["deliveries"]
            assert {item["output_id"] for item in deliveries} == expected_ids
            assert len(deliveries) == len(expected_ids)
            assert len({item["delivery_id"] for item in deliveries}) == len(deliveries)
            demo.claim()
            saved = deployment.import_reports_with_client(deployment.processing)
            assert final_report["report_id"] in saved["saved_report_ids"]
            imported_report_ids.extend(saved["saved_report_ids"])
            for delivery in deliveries:
                path = next(
                    candidate for candidate, row in by_path.items()
                    if str(row[3]) == delivery["output_id"]
                )
                claimed = deployment.processing / delivery["file_name"]
                data = claimed.read_bytes()
                expected = contents[path]
                digest = hashlib.sha256(expected).hexdigest()
                assert data == expected
                assert delivery["size"] == len(expected)
                assert delivery["sha256"] == digest
                media_path = deployment.root / f"client-media-{delivery['delivery_id']}.json"
                media = deployment.run_client_driver(
                    "import-media",
                    str(store),
                    str(claimed),
                    output=media_path,
                )
                assert media["delivery_id"] == delivery["delivery_id"]
                assert media["output_id"] == delivery["output_id"]
                assert media["video"]["status"] == "verified"
                assert media["video"]["sha256"] == digest
                all_delivery_ids.append(delivery["delivery_id"])
                delivery_evidence.append({
                    "output_id": delivery["output_id"],
                    "delivery_id": delivery["delivery_id"],
                    "file_name": delivery["file_name"],
                    "size": delivery["size"],
                    "sha256": delivery["sha256"],
                    "client_import_receipt": str(media_path),
                })

        assert len(set(all_delivery_ids)) == len(all_delivery_ids)
        for path, expected in contents.items():
            assert (remote / path.lstrip("/")).read_bytes() == expected
        ack, receipt = deployment.export_plan_with_client({
            "name": "ack",
            "actions": [{
                "name": "ack",
                "type": "report_status",
                "scheduled_at": format_utc(read_clock(clock_file)["utc_us"]),
                "params": {"scope": "full"},
            }],
        })
        assert receipt["last_report_id"] == final_report["report_id"]
        demo.submit(ack)
        wait_for(
            lambda: query_state(
                deployment.state_db,
                "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state",
            ) == [(int(final_report["report_id"]), final_report["to_wm"])],
            120,
            "客户端累计 ACK 未吸收",
        )
        ack_state = query_state(
            deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state",
        )[0]
        wheel_root = package.python.parent.parent.parent
        wheels = sorted((wheel_root / "dist").glob("camctl-*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        evidence = {
            "result_mode": "segmented",
            "action_type": action_type,
            "action_id": str(action_id),
            "source_report_id": report["report_id"],
            "coverage": coverage,
            "outputs": [
                {
                    "output_id": str(row[3]),
                    "device_path": row[1],
                    "kind": "preview" if row[4] == preview_kind else "original",
                }
                for row in rows
            ],
            "deliveries": delivery_evidence,
            "client_imported_report_ids": sorted(set(imported_report_ids)),
            "client_ack": receipt,
            "acknowledged_state": {
                "report_id": ack_state[0],
                "watermark": ack_state[1],
            },
            "wheel": {
                "path": str(wheel),
                "size_bytes": wheel.stat().st_size,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "installed_package_origin": str(package_origin),
            },
        }
        (deployment.root / "action6-segmented-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        demo.stop()
