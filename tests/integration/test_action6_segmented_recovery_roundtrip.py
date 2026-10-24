"""安装版配置冻结 Action6 与 OSMO 两类设备的视频结果模式。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
from decimal import Decimal

import pytest

from _action6_segmented_files import create_segmented_media_manifest
from _osmo_builtin_clock import format_utc, read_clock, set_frozen, set_running, whole_second_utc_us
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl.contracts.enums import enum_for
from camctl_fixtures import Deployment
from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES
from test_action6_builtin_demo_roundtrip import action6_package, _run
from test_camctl_c_module_roundtrip import _report_with_statuses
from test_osmo_builtin_timelapse_roundtrip import _action, _records


_INTEGRATION = Path(__file__).resolve().parent


def _install_action6_tools(
        package, deployment, params, manifest, *, capture: str,
        observer_stage: str | None = None,
        read_gate: Path | None = None,
        local_read_gate: Path | None = None,
        fail_ffprobe_index: int | None = None,
):
    tools = deployment.root / "tools"
    tools.mkdir()
    for name in (
        "_osmo_builtin_clock.py", "_osmo_builtin_clock_entry.py",
        "_action6_builtin_adb.py", "_action6_segmented_entry.py",
        "_action6_segmented_observer.py",
    ):
        shutil.copyfile(_INTEGRATION / name, tools / name)
    for command, helper in (
        ("adb", "_action6_builtin_adb.py"),
        ("camctl", "_action6_segmented_entry.py"),
    ):
        path = tools / command
        path.write_text(
            "#!/bin/sh\nexec " + str(package.python) + " "
            + str(tools / helper) + ' "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    real_ffprobe = shutil.which("ffprobe")
    if real_ffprobe is None:
        raise RuntimeError("Action6 异常检查安装链需要真实 ffprobe")
    gate_script = tools / "ffprobe-gate.py"
    gate_script.write_text(
        "from __future__ import annotations\n"
        "import fcntl, hashlib, json, os, subprocess, sys, time\n"
        "log = os.environ['CAMCTL_ACTION6_TEST_FFPROBE_LOG']\n"
        "with open(log + '.lock', 'a+') as lock:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "    counter = log + '.count'\n"
        "    try:\n"
        "        index = int(open(counter, encoding='utf-8').read()) + 1\n"
        "    except FileNotFoundError:\n"
        "        index = 1\n"
        "    with open(counter, 'w', encoding='utf-8') as stream:\n"
        "        stream.write(str(index))\n"
        "input_path = sys.argv[-1]\n"
        "input_size = os.path.getsize(input_path)\n"
        "digest = hashlib.sha256()\n"
        "with open(input_path, 'rb') as source:\n"
        "    for block in iter(lambda: source.read(1024 * 1024), b''):\n"
        "        digest.update(block)\n"
        "failure = os.environ.get('CAMCTL_ACTION6_TEST_FAIL_FFPROBE_INDEX')\n"
        "real_tool = failure is None or index != int(failure)\n"
        "returncode = (subprocess.run([os.environ['CAMCTL_ACTION6_REAL_FFPROBE'], *sys.argv[1:]]).returncode\n"
        "             if real_tool else 86)\n"
        "record = {'index': index, 'pid': os.getpid(), 'argv': sys.argv[1:], 'path': input_path,\n"
        "          'input_size_bytes': input_size, 'input_sha256': digest.hexdigest(),\n"
        "          'real_tool': real_tool, 'returncode': returncode, 'utc_ns': time.time_ns()}\n"
        "with open(log, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(record) + '\\n')\n"
        "raise SystemExit(returncode)\n",
        encoding="utf-8",
    )
    ffprobe = tools / "ffprobe"
    ffprobe.write_text(
        "#!/bin/sh\nexec " + str(package.python) + " "
        + str(gate_script) + ' "$@"\n',
        encoding="utf-8",
    )
    ffprobe.chmod(0o755)

    clock = deployment.root / "clock.json"
    set_frozen(clock, utc_us=whole_second_utc_us(), monotonic_ns=time.monotonic_ns())
    remote = deployment.root / "camera-files"
    (remote / "mnt/media_rw/emulated/DCIM").mkdir(parents=True)
    environment = dict(package.environment)
    environment.update({
        "PATH": str(tools) + os.pathsep + environment.get("PATH", ""),
        "CAMCTL_ACTION6_ADB_ROOT": str(remote),
        "CAMCTL_ACTION6_TEST_CAPTURE": capture,
        "CAMCTL_ACTION6_TEST_PARAMS": json.dumps(params),
        "CAMCTL_ACTION6_TEST_FILES": str(manifest),
        "CAMCTL_ACTION6_TEST_FFPROBE_LOG": str(deployment.root / "ffprobe.jsonl"),
        "CAMCTL_ACTION6_REAL_FFPROBE": real_ffprobe,
        "CAMCTL_OSMO_TEST_CLOCK_FILE": str(clock),
        "CAMCTL_OSMO_TEST_CLOCK_LOG": str(deployment.root / "clock-reads.jsonl"),
        "CAMCTL_OSMO_TEST_PROCESS_LOG": str(deployment.root / "cli-processes.jsonl"),
    })
    if observer_stage is not None:
        environment["CAMCTL_ACTION6_TEST_OBSERVER_STAGE"] = observer_stage
        environment["CAMCTL_ACTION6_TEST_OBSERVER_MARKER"] = str(
            deployment.root / "action6-observer-marker.json"
        )
    if read_gate is not None:
        read_gate.mkdir(parents=True)
        environment["CAMCTL_ACTION6_TEST_READ_GATE_DIR"] = str(read_gate)
    if local_read_gate is not None:
        local_read_gate.mkdir(parents=True)
        environment["CAMCTL_ACTION6_TEST_LOCAL_READ_GATE_DIR"] = str(local_read_gate)
    if fail_ffprobe_index is not None:
        environment["CAMCTL_ACTION6_TEST_FAIL_FFPROBE_INDEX"] = str(fail_ffprobe_index)
    return environment, tools / "camctl", clock, remote


def _prepare_installed_action6(
        tmp_path, package, *, native: bool, params_override: dict | None = None,
        include_jpeg: bool = False, values: tuple[int, ...] | None = None,
        observer_stage: str | None = None, read_gate: Path | None = None,
        local_read_gate: Path | None = None,
        fail_ffprobe_index: int | None = None,
):
    deployment = Deployment(tmp_path / ("action6-native" if native else "action6-record"),
                            devices=False)
    template_name = "action6-timelapse.json" if native else "action6-record.json"
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
    template = json.loads(resources[template_name])
    params = dict(template["actions"][0]["params"])
    if native:
        params.update(duration_s=300, interval_s=2, outputs="video_jpeg")
    else:
        params["duration_s"] = 60
    if params_override:
        params.update(params_override)
    base_config = resources["config.toml"].replace(
        "REPLACE_ACTION6_SERIAL", "action6-test-serial"
    )
    # 普通录像异常矩阵使用 62 秒修复门槛；与 75 秒停止目标配合时，
    # 真实 STOP 耗时证据应使生产 handler 进入检查。本测试不合成该证据。
    recovery_config = (
        "\n[devices.action6.recording]\nrepair_margin_s = 2\n"
        if not native else ""
    )
    # The real deployment default is 128 MiB. This recovery fixture needs a
    # durable partial-copy boundary inside its deliberately small media files.
    copy_config = '\n[copy]\nsegment_size = "1 MiB"\n'
    deployment.config_path.write_text(
        deployment.config_path.read_text(encoding="utf-8")
        + "\n" + base_config + copy_config + recovery_config
        + '\n[devices.action6.video]\nresult_mode = "segmented"\n',
        encoding="utf-8",
    )
    metric = "frames" if native else "duration_s"
    manifest, contents, facts = create_segmented_media_manifest(
        tmp_path, metric=metric, include_jpeg=include_jpeg, values=values,
    )
    environment, camctl, clock, remote = _install_action6_tools(
        package, deployment, params, manifest, capture="timelapse" if native else "record",
        observer_stage=observer_stage, read_gate=read_gate,
        local_read_gate=local_read_gate,
        fail_ffprobe_index=fail_ffprobe_index,
    )
    initial = read_clock(clock)
    initialized = _run(
        [str(camctl), "init", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=environment,
    )
    assert initialized.returncode == 0, initialized.stderr
    described = _run(
        [str(camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=environment,
    )
    assert described.returncode == 0, described.stderr
    store = deployment.root / "client-store"
    store.mkdir()
    (store / "device-capabilities.json").write_text(
        described.stdout, encoding="utf-8"
    )
    action_type = "camera_timelapse" if native else "camera_record"
    name = "capture"
    plan, _ = deployment.export_plan_with_client({
        "name": "Action6 segmented native" if native else "Action6 segmented record",
        "actions": [{
            "name": name,
            "type": action_type,
            "device_id": "action6",
            "scheduled_at": format_utc(initial["utc_us"]),
            "params": params,
            "policy": {"max_delay_ms": 30000},
        }],
    })
    return {
        "deployment": deployment,
        "environment": environment,
        "camctl": camctl,
        "clock": clock,
        "initial": initial,
        "remote": remote,
        "manifest": manifest,
        "contents": contents,
        "facts": facts,
        "params": params,
        "action_type": action_type,
        "action_name": name,
        "plan": plan,
        "store": store,
    }


def _start_installed_capture(host_demo, package, scenario, monkeypatch):
    deployment = scenario["deployment"]
    demo = HostDemo(deployment, host_demo, launcher=str(scenario["camctl"]))
    monkeypatch.chdir(package.python.parents[2])
    demo.start(env=scenario["environment"])
    demo.submit(scenario["plan"])
    starts = wait_for(
        lambda: query_state(
            deployment.state_db,
            "SELECT a.id FROM actions a JOIN operation_runs r ON r.action_id=a.id "
            "JOIN operation_attempts t ON t.run_id=r.id "
            "WHERE a.name='capture' AND r.kind=1 AND t.result_event_id IS NOT NULL",
        ),
        90,
        "Action6 START 未可靠保存",
    )
    assert len(starts) == 1
    return demo, int(starts[0][0])


def _submit_obtain(demo, deployment, *, action_name, source_id, selection):
    instant = format_utc(read_clock(deployment.root / "clock.json")["utc_us"])
    plan, _ = deployment.export_plan_with_client({
        "name": action_name,
        "actions": [{
            "name": action_name,
            "type": "obtain_action_outputs",
            "scheduled_at": instant,
            "params": {
                "source": {"action_instance_id": str(source_id)},
                "purpose": "manual",
                **selection,
            },
        }],
    })
    demo.submit(plan)
    action = wait_for(
        lambda: query_state(
            deployment.state_db,
            "SELECT id,status FROM actions WHERE name=? ORDER BY id DESC LIMIT 1",
            (action_name,),
        ),
        60,
        f"取回动作未受理: {action_name}",
    )
    assert len(action) == 1
    return int(action[0][0])


def _wait_for_waiting_obtains(deployment, source_id, names):
    expected_action_status = int(enum_for("actions.status").RUNNING)
    expected_selection_status = int(enum_for("obtain_source_selections.status").PENDING)

    def rows():
        return query_state(
            deployment.state_db,
            "SELECT a.name,a.status,s.status FROM actions a "
            "JOIN action_dependencies d ON d.action_id=a.id "
            "JOIN obtain_source_selections s ON s.dependency_id=d.id "
            "WHERE d.depends_on_action_id=? ORDER BY a.name",
            (source_id,),
        )

    def complete():
        found = rows()
        if (
            len(found) == len(names)
            and {row[0] for row in found} == set(names)
            and all(row[1] == expected_action_status for row in found)
            and all(row[2] == expected_selection_status for row in found)
        ):
            return found
        return None

    waiting = wait_for(
        complete,
        90,
        "两个正式取回没有同时进入 RUNNING/PENDING 等待来源",
    )
    assert len(waiting) == 2
    return waiting


def _assert_process_group_exited(pgid: int) -> None:
    def gone():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return None

    assert wait_for(
        gone, 10, f"注入恢复边界的旧 host 进程组仍存活: pgid={pgid}"
    )


def _source_rows(deployment, source_id):
    rows = query_state(
        deployment.state_db,
        "SELECT o.id,o.kind,d.id,d.original_device_file_id,d.locator_json,d.size_bytes,d.sha256 "
        "FROM outputs o JOIN device_files d ON d.id=o.device_file_id "
        "WHERE o.source_action_id=? ORDER BY o.id",
        (source_id,),
    )
    return {
        json.loads(row[4])["path"]: {
            "output_id": str(row[0]), "kind": row[1], "device_file_id": row[2],
            "paired_file_id": row[3], "size": row[5], "sha256": row[6],
        }
        for row in rows
    }


def _assert_source_manifest_facts(scenario, output_rows):
    expected_sha_by_output_id = {}
    for path, row in output_rows.items():
        content = scenario["contents"][path]
        digest = hashlib.sha256(content).hexdigest()
        assert row["size"] == len(content)
        if row["sha256"] is not None:
            assert row["sha256"] == digest
        expected_sha_by_output_id[row["output_id"]] = digest
    return expected_sha_by_output_id


def _assert_inspection_copy_facts(deployment, scenario, source_id):
    rows = query_state(
        deployment.state_db,
        "SELECT c.source_device_file_id,c.committed_bytes,c.source_size,c.target_sha256,d.locator_json "
        "FROM file_copies c JOIN segment_inspections i ON i.id=c.inspection_id "
        "JOIN device_files d ON d.id=c.source_device_file_id WHERE i.action_id=? ORDER BY c.id",
        (source_id,),
    )
    assert rows
    for _device_file_id, committed, size, target_sha256, locator_json in rows:
        path = json.loads(locator_json)["path"]
        content = scenario["contents"][path]
        assert committed == size == len(content)
        if target_sha256 is not None:
            assert target_sha256 == hashlib.sha256(content).hexdigest()


def _coverage_rows(deployment, source_id):
    return query_state(
        deployment.state_db,
        "SELECT m.ordinal,m.output_id,d.locator_json FROM segment_scope_members m "
        "JOIN segment_sets s ON s.id=m.set_id "
        "LEFT JOIN outputs o ON o.id=m.output_id "
        "LEFT JOIN device_files d ON d.id=o.device_file_id "
        "WHERE s.action_id=? AND s.published_event_id IS NOT NULL ORDER BY m.ordinal",
        (source_id,),
    )


def _inspection_copy_state(deployment, source_id):
    return query_state(
        deployment.state_db,
        "SELECT c.id,c.source_device_file_id,c.source_intermediate_file_id,c.target_file_id,"
        "c.round,c.recopies_used,c.max_recopies_used,c.committed_bytes,c.source_size,"
        "r.id,r.attempts_used,r.max_attempts_used,"
        "(SELECT t.copy_round FROM operation_attempts t WHERE t.run_id=r.id "
        "ORDER BY t.attempt_no DESC LIMIT 1) "
        "FROM file_copies c JOIN segment_inspections i ON i.id=c.inspection_id "
        "JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3 "
        "WHERE i.action_id=? ORDER BY c.id LIMIT 1",
        (source_id,),
    )


def _ffprobe_records(deployment):
    path = deployment.root / "ffprobe.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_probe_input_facts(probes, facts, contents):
    """Compare bytes observed by the ffprobe wrapper with the fixture sequence.

    The wrapper records these facts while the media input is still held by the
    inspection operation, so this remains valid after result cleanup removes
    the staging copy.
    """
    assert len(probes) == len(facts)
    for probe, fact in zip(probes, facts, strict=True):
        expected = contents[fact["device_path"]]
        assert probe["input_size_bytes"] == len(expected)
        assert probe["input_sha256"] == hashlib.sha256(expected).hexdigest()


def _action_status_report(deployment, action_name, expected_status):
    return wait_for(
        lambda: _report_with_statuses(deployment.ready, {action_name: expected_status}),
        180,
        f"{action_name} 未进入预期报告终态 {expected_status}",
    )


def _save_report_snapshot(deployment, report, name: str) -> Path:
    target = deployment.root / f"{name}.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _deliveries_from_report(report, action_name):
    action = _action(report, action_name)
    return action.get("deliveries", [])


def _assert_client_delivery_files(
        deployment, store, report, action_name, output_paths,
):
    deliveries = _deliveries_from_report(report, action_name)
    expected_ids = {output_paths[path]["output_id"] for path in output_paths}
    assert {item["output_id"] for item in deliveries} == expected_ids
    assert len({item["delivery_id"] for item in deliveries}) == len(deliveries)
    evidence = []
    for delivery in deliveries:
        source_path = next(
            path for path, item in output_paths.items()
            if item["output_id"] == delivery["output_id"]
        )
        claimed = deployment.processing / delivery["file_name"]
        data = claimed.read_bytes()
        expected = output_paths[source_path]["content"]
        digest = hashlib.sha256(expected).hexdigest()
        assert data == expected
        assert delivery["size"] == len(expected)
        assert delivery["sha256"] == digest
        receipt = deployment.root / f"client-media-{delivery['delivery_id']}.json"
        imported = deployment.run_client_driver(
            "import-media", str(store), str(claimed), output=receipt
        )
        assert imported["delivery_id"] == delivery["delivery_id"]
        assert imported["output_id"] == delivery["output_id"]
        assert imported["video"]["status"] == "verified"
        assert imported["video"]["sha256"] == digest
        evidence.append({
            "output_id": delivery["output_id"],
            "delivery_id": delivery["delivery_id"],
            "file_name": delivery["file_name"],
            "size": delivery["size"],
            "sha256": digest,
            "client_import_receipt": str(receipt),
        })
    return evidence


def _assert_ack(demo, scenario, report):
    deployment = scenario["deployment"]
    ack, receipt = deployment.export_plan_with_client({
        "name": "ack",
        "actions": [{
            "name": "ack",
            "type": "report_status",
            "scheduled_at": format_utc(read_clock(scenario["clock"])["utc_us"]),
            "params": {"scope": "full"},
        }],
    })
    assert receipt["last_report_id"] == report["report_id"]
    demo.submit(ack)
    acked = wait_for(
        lambda: query_state(
            deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state",
        ) == [(int(report["report_id"]), report["to_wm"])],
        120,
        "实际客户端累计 ACK 未吸收",
    )
    assert acked
    return receipt


def _release_clock(scenario, seconds: int):
    initial = scenario["initial"]
    return set_running(
        scenario["clock"],
        utc_us=initial["utc_us"] + seconds * 1_000_000,
        monotonic_ns=initial["monotonic_ns"] + seconds * 1_000_000_000,
    )


def _cleanup_inspection_inputs(deployment, source_id, failure):
    purpose = int(enum_for("intermediate_files.purpose").RECORDING_INPUT)
    released = int(enum_for("intermediate_files.retention_state").RELEASABLE)
    completed = int(enum_for("intermediate_files.cleanup_state").COMPLETED)

    def rows():
        return query_state(
            deployment.state_db,
            "SELECT id,relative_path,retention_state,cleanup_state FROM intermediate_files "
            "WHERE owner_action_id=? AND purpose=? ORDER BY id",
            (source_id, purpose),
        )

    retained = wait_for(
        lambda: current if (current := rows())
        and all(row[2] == released and row[3] == completed for row in current)
        else None,
        180,
        "全部绑定取回终态后，Action6 检查副本仍未完成清理",
        failure=failure,
    )
    assert retained
    for _identity, relative_path, _retention, _cleanup in retained:
        assert not (deployment.staging / relative_path).exists()
    return retained


@pytest.mark.skipif(os.name != "posix", reason="安装版 POSIX C host 恢复链")
@pytest.mark.parametrize(
    (
        "case", "boundary", "values", "fail_ffprobe_index", "source_status",
        "coverage_status", "confirmed", "checked_count",
    ),
    [
        ("reached", "copy_progress", None, None, "succeeded", "reached", 67, 3),
        ("reached", "inspection_saved", None, None, "succeeded", "reached", 67, 3),
        ("reached", "source_committed", None, None, "succeeded", "reached", 67, 3),
        ("reached", "local_read", None, None, "succeeded", "reached", 67, 3),
        ("unknown", "none", None, 3, "succeeded", "unconfirmed", 45, 2),
        ("short", "none", (18, 20, 12), None, "failed", "confirmed_short", 50, 3),
    ],
    ids=(
        "reached-copy-progress", "reached-inspection-saved",
        "reached-source-commit-receipt", "reached-local-read",
        "unknown-prefix", "complete-short-sequence",
    ),
)
def test_installed_action6_ordinary_recovery_keeps_scope_waiters_and_copy_ownership(
        tmp_path, host_demo, action6_package, monkeypatch, case, boundary,
        values, fail_ffprobe_index, source_status, coverage_status, confirmed,
        checked_count,
):
    package = action6_package
    adb_gate = tmp_path / "adb-read-gate" if boundary == "copy_progress" else None
    local_gate = tmp_path / "local-read-gate" if boundary == "local_read" else None
    observer = boundary if boundary in ("inspection_saved", "source_committed", "local_read") else None
    scenario = _prepare_installed_action6(
        tmp_path, package, native=False,
        values=values,
        observer_stage=observer,
        read_gate=adb_gate,
        local_read_gate=local_gate,
        fail_ffprobe_index=fail_ffprobe_index,
    )
    deployment = scenario["deployment"]
    demo, source_id = _start_installed_capture(host_demo, package, scenario, monkeypatch)
    waiter_names = ("pre-default-a", "pre-default-b")
    waiter_ids = [
        _submit_obtain(
            demo, deployment, action_name=name, source_id=source_id,
            selection={"filter": "default"},
        )
        for name in waiter_names
    ]
    _wait_for_waiting_obtains(deployment, source_id, waiter_names)
    failure = demo.watch_failures()
    try:
        _release_clock(scenario, 75)
        gate_marker = None
        boundary_pgid = None
        resume_copy_before = None
        resume_path_argument = None
        resume_request_offset_bytes = None
        resume_bytes_forwarded = None
        resume_call_count = None
        resume_after_ns = None
        resume_evidence = None
        post_h_waiter_id = None
        post_h_waiter_name = None
        post_h_submit_ns = None
        post_h_dependency = None
        if boundary == "copy_progress":
            gate_marker = wait_for(
                lambda: adb_gate / "read-entered.json"
                if (adb_gate / "read-entered.json").is_file() else None,
                180,
                "Action6 检查副本未在真实首个进度段字节流出后暂停",
            )
            observed = json.loads(gate_marker.read_text(encoding="utf-8"))
            assert observed["stage"] == "after_first_copy_segment_bytes_streamed"
            assert observed["request_skip"] == 0
            assert observed["request_offset_bytes"] == 0
            assert observed["bytes_forwarded"] == DEFAULT_CHUNK_SIZE_BYTES
            resume_path_argument = observed["path_argument"]
            resume_request_offset_bytes = observed["request_offset_bytes"]
            resume_bytes_forwarded = observed["bytes_forwarded"]
            first_copies = wait_for(
                lambda: rows if len(rows := _inspection_copy_state(
                    deployment, source_id
                )) == 1 and DEFAULT_CHUNK_SIZE_BYTES <= rows[0][7] < rows[0][8]
                else None,
                30,
                "首块已转发后，检查副本没有提交对应的部分字节",
            )
            assert len(first_copies) == 1
            resume_copy_before = first_copies[0]
            assert resume_copy_before[1] is not None and resume_copy_before[2] is None
            assert (DEFAULT_CHUNK_SIZE_BYTES <= resume_copy_before[7]
                    < resume_copy_before[8])
            assert resume_copy_before[12] == resume_copy_before[4]
            boundary_pgid = observed["pgid"]
        elif boundary in ("inspection_saved", "source_committed"):
            marker = deployment.root / "action6-observer-marker.json"
            gate_marker = wait_for(
                lambda: marker if marker.is_file() else None,
                180,
                f"Action6 未到达持久化同步点: {boundary}",
            )
            snapshot = json.loads(gate_marker.read_text(encoding="utf-8"))
            assert snapshot["stage"] == boundary
            boundary_pgid = snapshot["pgid"]
            if boundary == "inspection_saved":
                saved = [row for row in snapshot["state"]["inspections"] if row[2] != 1]
                assert len(saved) == 1 and saved[0][3] is not None
                assert len(snapshot["state"]["copies"]) == 1
                assert snapshot["state"]["copies"][0][4] == snapshot["state"]["copies"][0][5]
            else:
                published = [row for row in snapshot["state"]["sets"] if row[4] is not None]
                assert len(published) == 1
                at_commit = snapshot["state"]["waiters"]
                assert {row[0] for row in at_commit} == set(waiter_ids)
                assert all(
                    row[1] == int(enum_for("actions.status").RUNNING)
                    and row[3] == int(enum_for("obtain_source_selections.status").PENDING)
                    for row in at_commit if row[0] in waiter_ids
                )
                post_h_waiter_name = "post-h-default"
                post_h_submit_ns = time.monotonic_ns()
                post_h_waiter_id = _submit_obtain(
                    demo, deployment, action_name=post_h_waiter_name,
                    source_id=source_id, selection={"filter": "default"},
                )
                assert post_h_waiter_id not in {row[0] for row in at_commit}
        elif boundary == "local_read":
            marker = local_gate / "local-read-entered.json"
            gate_marker = wait_for(
                lambda: marker if marker.is_file() else None,
                180,
                "绑定取回未在真实本地检查副本读取边界暂停",
            )
            read = json.loads(gate_marker.read_text(encoding="utf-8"))
            assert read["bytes_read"] > 0 and read["position"] >= read["bytes_read"]
            assert read["initial_offset"] == 0 and Path(read["source_path"]).is_file()
            boundary_pgid = read["pgid"]
            delivery_created = int(enum_for("obtain_items.status").DELIVERY_CREATED)
            delivery_prepared = int(enum_for("deliveries.status").PREPARED)
            first_complete_one_waiting = wait_for(
                lambda: rows if len(rows := query_state(
                    deployment.state_db,
                    "SELECT d.action_id,COUNT(i.id),"
                    "SUM(CASE WHEN i.status=? THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN d.status=? THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN c.committed_bytes=c.source_size THEN 1 ELSE 0 END) "
                    "FROM action_dependencies a "
                    "JOIN obtain_source_selections s ON s.dependency_id=a.id "
                    "JOIN obtain_items i ON i.selection_id=s.id "
                    "JOIN deliveries d ON d.id=i.delivery_id "
                    "JOIN file_copies c ON c.delivery_id=d.id "
                    "WHERE a.depends_on_action_id=? AND a.action_id IN (?,?) "
                    "GROUP BY d.action_id ORDER BY d.action_id",
                    (delivery_created, delivery_prepared, source_id, *waiter_ids),
                )) == 2 and rows[0][0] == waiter_ids[0]
                and rows[0][1:] == (checked_count, checked_count, checked_count, checked_count)
                and rows[1][0] == waiter_ids[1]
                and rows[1][1:] == (checked_count, checked_count, 0, 0) else None,
                120,
                "一组绑定取回的副本未全部准备、另一组仍未开始本地读取",
            )
            assert len(first_complete_one_waiting) == 2
            required = int(enum_for("intermediate_files.retention_state").REQUIRED)
            not_needed = int(enum_for("intermediate_files.cleanup_state").NOT_NEEDED)
            held = query_state(
                deployment.state_db,
                "SELECT relative_path,retention_state,cleanup_state FROM intermediate_files "
                "WHERE owner_action_id=? AND purpose=? ORDER BY id",
                (source_id, int(enum_for("intermediate_files.purpose").RECORDING_INPUT)),
            )
            assert held and all(row[1:] == (required, not_needed) for row in held)
            assert all((deployment.staging / row[0]).is_file() for row in held)
            assert Path(read["source_path"]).resolve() in {
                (deployment.staging / row[0]).resolve() for row in held
            }

        if boundary != "none":
            assert boundary_pgid is not None
            demo.stop()
            _assert_process_group_exited(boundary_pgid)
            release_gate = adb_gate if boundary == "copy_progress" else local_gate
            if release_gate is not None:
                (release_gate / "release-read").touch()
                (release_gate / "release-local-read").touch()
            if boundary == "copy_progress":
                resume_call_count = len(_records(deployment.root / "adb-calls.jsonl"))
                resume_after_ns = time.monotonic_ns()
            demo = HostDemo(
                scenario["deployment"], host_demo, launcher=str(scenario["camctl"])
            )
            failure = demo.watch_failures()
            monkeypatch.chdir(package.python.parents[2])
            demo.start(env=scenario["environment"])
            demo.submit(scenario["plan"])
            if post_h_waiter_id is not None:
                post_h_dependency = wait_for(
                    lambda: rows if len(rows := query_state(
                        deployment.state_db,
                        "SELECT d.action_id,d.depends_on_action_id,s.id,s.status "
                        "FROM action_dependencies d "
                        "JOIN obtain_source_selections s ON s.dependency_id=d.id "
                        "WHERE d.action_id=? AND d.depends_on_action_id=?",
                        (post_h_waiter_id, source_id),
                    )) == 1 else None,
                    120,
                    "重启调度后 post-H 取回未保存来源依赖与选择行",
                )
                assert post_h_dependency[0][:2] == (post_h_waiter_id, source_id)
            if boundary == "copy_progress":
                assert resume_path_argument is not None and resume_copy_before is not None
                assert resume_call_count is not None and resume_after_ns is not None
                resumed_reads = wait_for(
                    lambda: reads if (reads := [
                        call for call in _records(deployment.root / "adb-calls.jsonl")[resume_call_count:]
                        if call.get("kind") == "file"
                        and call.get("file_kind") == "read"
                        and call.get("started_ns", 0) >= resume_after_ns
                        and resume_path_argument in call.get("script", "")
                    ]) else None,
                    120,
                    "重启后没有对同一源文件发起续偏移设备读取",
                )
                first_resumed_read = min(resumed_reads, key=lambda call: call["started_ns"])
                skip_match = re.search(r"(?:^| )skip=([0-9]+)$", first_resumed_read["script"])
                assert skip_match is not None
                resumed_skip = int(skip_match[1])
                assert resumed_skip == resume_copy_before[7] // 65_536
                after_copy_rows = wait_for(
                    lambda: state if len(state := _inspection_copy_state(deployment, source_id)) == 1 else None,
                    30,
                    "恢复后的分段检查读取流程未保留原拷贝状态",
                )
                after_copy = after_copy_rows[0]
                assert after_copy[:7] == resume_copy_before[:7]
                assert after_copy[9] == resume_copy_before[9]
                assert after_copy[10] >= resume_copy_before[10]
                assert after_copy[11] == resume_copy_before[11]
                assert after_copy[12] == after_copy[4] == resume_copy_before[4]
                assert after_copy[10] <= after_copy[11]
                resume_evidence = {
                    "copy_id": after_copy[0],
                    "source_path_argument": resume_path_argument,
                    "copy_progress_gate_initial_request_offset_bytes": resume_request_offset_bytes,
                    "copy_progress_gate_bytes_forwarded": resume_bytes_forwarded,
                    "source_device_file_id": after_copy[1],
                    "target_file_id": after_copy[3],
                    "round": after_copy[4],
                    "committed_bytes_before_restart": resume_copy_before[7],
                    "source_size": resume_copy_before[8],
                    "resume_skip": resumed_skip,
                    "adb_call_count_before_restart": resume_call_count,
                    "resume_boundary_monotonic_ns": resume_after_ns,
                    "read_started_monotonic_ns": first_resumed_read["started_ns"],
                    "operation_run_id": after_copy[9],
                    "attempts_used_before_restart": resume_copy_before[10],
                    "attempts_used_after_resume": after_copy[10],
                    "max_attempts_used": after_copy[11],
                }
        expected_report_statuses = {
            "capture": source_status,
            waiter_names[0]: "succeeded",
            waiter_names[1]: "succeeded",
        }
        if post_h_waiter_name is not None:
            expected_report_statuses[post_h_waiter_name] = "succeeded"
        report = wait_for(
            lambda: _report_with_statuses(deployment.ready, expected_report_statuses),
            240,
            f"Action6 来源及等待取回没有在 {boundary} 恢复后成功",
        )
        source_report_path = _save_report_snapshot(
            deployment, report, f"action6-ordinary-{case}-source-report"
        )
        source = _action(report, "capture")
        coverage = source["result"]["coverage"]
        assert coverage["metric"] == "duration_s"
        assert coverage["status"] == coverage_status
        assert Decimal(str(coverage["target"])) == Decimal("60")
        assert Decimal(str(coverage["confirmed"])) == Decimal(str(confirmed))
        assert len(coverage["checked_output_ids"]) == checked_count
        assert coverage["default_scope"]["member_count"] == checked_count
        assert coverage["default_scope"]["output_ids"] == coverage["checked_output_ids"]
        expected_reason = {
            "unknown": {"code": "probe_error"},
            "short": {"code": "invalid_outputs"},
        }.get(case)
        assert coverage.get("reason") == expected_reason

        output_rows = _source_rows(deployment, source_id)
        expected_sha_by_output_id = _assert_source_manifest_facts(scenario, output_rows)
        expected_size_by_output_id = {
            row["output_id"]: row["size"] for row in output_rows.values()
        }
        _assert_inspection_copy_facts(deployment, scenario, source_id)
        video_paths = [item["device_path"] for item in scenario["facts"]]
        expected_scope = [output_rows[path]["output_id"] for path in video_paths[:checked_count]]
        assert coverage["checked_output_ids"] == expected_scope
        scopes = _coverage_rows(deployment, source_id)
        assert [str(row[1]) for row in scopes] == expected_scope
        assert len([path for path in output_rows if path.endswith(".MP4")]) == len(video_paths)
        assert len([path for path in output_rows if path.endswith(".LRF")]) == len(video_paths)
        probes = _ffprobe_records(deployment)
        assert len(probes) == (3 if case == "unknown" else checked_count)
        successful_probes = [item for item in probes if item["real_tool"] and item["returncode"] == 0]
        expected_successful_probes = 2 if case == "unknown" else checked_count
        assert len(successful_probes) == expected_successful_probes
        _assert_probe_input_facts(probes, scenario["facts"][:len(probes)], scenario["contents"])
        if case == "unknown":
            assert probes[-1]["index"] == 3 and not probes[-1]["real_tool"]

        published_sets = query_state(
            deployment.state_db,
            "SELECT id,version FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL",
            (source_id,),
        )
        assert len(published_sets) == 1 and published_sets[0][1] == 1

        calls = _records(deployment.root / "adb-calls.jsonl")
        assert len([call for call in calls if call.get("kind") == "start"]) == 1
        assert len([call for call in calls if call.get("kind") == "stop"]) == 1
        if boundary == "inspection_saved":
            probed = [item["path"] for item in _ffprobe_records(deployment)]
            assert len(probed) == len(set(probed)) == 3

        demo.claim()
        deployment.import_reports_with_client(deployment.processing)
        delivery_map = {
            path: {
                **output_rows[path],
                "content": scenario["contents"][path],
            }
            for path in video_paths[:checked_count]
        }
        delivery_evidence = []
        for waiter_name in waiter_names:
            delivery_evidence.extend(_assert_client_delivery_files(
                deployment, scenario["store"], report, waiter_name, delivery_map
            ))
        verified = {
            int(enum_for("file_copies.verification_state").MATCHED),
            int(enum_for("file_copies.verification_state").SOURCE_CHECKSUM_UNAVAILABLE),
        }
        post_h_evidence = []
        if post_h_waiter_id is not None:
            assert post_h_waiter_name is not None and post_h_submit_ns is not None
            assert post_h_dependency is not None
            post_h_evidence = _assert_client_delivery_files(
                deployment, scenario["store"], report,
                post_h_waiter_name, delivery_map,
            )
            post_h_copies = query_state(
                deployment.state_db,
                "SELECT d.output_id,d.id,c.source_device_file_id,c.source_intermediate_file_id,"
                "c.verification_state,c.committed_bytes,c.source_size,c.target_sha256 "
                "FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id "
                "WHERE d.action_id=? ORDER BY d.output_id",
                (post_h_waiter_id,),
            )
            expected_post_h = {
                output_rows[path]["output_id"]: output_rows[path]["device_file_id"]
                for path in video_paths[:checked_count]
            }
            assert len(post_h_copies) == len(expected_post_h) == checked_count
            assert len({row[1] for row in post_h_copies}) == checked_count
            assert {str(row[0]): row[2] for row in post_h_copies} == expected_post_h
            assert all(
                row[3] is None
                and row[4] in verified
                and row[5] == row[6] == expected_size_by_output_id[str(row[0])]
                and row[7] == expected_sha_by_output_id[str(row[0])]
                for row in post_h_copies
            )
            post_h_reads = [
                call for call in _records(deployment.root / "adb-calls.jsonl")
                if call.get("kind") == "file"
                and call.get("file_kind") == "read"
                and call.get("started_ns", 0) >= post_h_submit_ns
            ]
            assert all(
                any(path in call.get("script", "") for call in post_h_reads)
                for path in video_paths[:checked_count]
            )

        bound_copies = query_state(
            deployment.state_db,
            "SELECT d.action_id,d.output_id,d.id,c.source_device_file_id,c.source_intermediate_file_id,"
            "c.verification_state,c.committed_bytes,c.source_size,c.target_sha256 "
            "FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id "
            "WHERE d.action_id IN (?,?) ORDER BY d.action_id,d.output_id",
            tuple(waiter_ids),
        )
        assert len(bound_copies) == checked_count * 2
        assert all(row[3] is None and row[4] is not None for row in bound_copies)
        assert all(
            row[5] in verified
            and row[6] == row[7] == expected_size_by_output_id[str(row[1])]
            and row[8] == expected_sha_by_output_id[str(row[1])]
            for row in bound_copies
        )
        assert len({row[2] for row in bound_copies}) == checked_count * 2
        _cleanup_inspection_inputs(deployment, source_id, failure)

        final_report = report
        late_evidence = []
        late_id = None
        late_name = None
        late_report_path = source_report_path
        if len(video_paths) > checked_count:
            late_path = video_paths[-1]
            late_name = "late-explicit"
            late_id = _submit_obtain(
                demo, deployment, action_name=late_name, source_id=source_id,
                selection={"output_ids": [output_rows[late_path]["output_id"]]},
            )
            final_report = _action_status_report(deployment, late_name, "succeeded")
            late_report_path = _save_report_snapshot(
                deployment, final_report, "action6-ordinary-late-obtain-report"
            )
            demo.claim()
            deployment.import_reports_with_client(deployment.processing)
            late_evidence = _assert_client_delivery_files(
                deployment, scenario["store"], final_report, late_name,
                {late_path: {**output_rows[late_path], "content": scenario["contents"][late_path]}},
            )
            late_copies = query_state(
                deployment.state_db,
                "SELECT d.output_id,c.source_device_file_id,c.source_intermediate_file_id,"
                "c.committed_bytes,c.source_size,c.target_sha256 "
                "FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id WHERE d.action_id=?",
                (late_id,),
            )
            assert len(late_copies) == 1
            assert late_copies[0][1] is not None and late_copies[0][2] is None
            assert late_copies[0][3] == late_copies[0][4] == expected_size_by_output_id[str(late_copies[0][0])]
            assert late_copies[0][5] == expected_sha_by_output_id[str(late_copies[0][0])]
            assert any(
                call.get("file_kind") == "read"
                and late_path in call.get("script", "")
                for call in _records(deployment.root / "adb-calls.jsonl")
            )
        late_report_receipt = _assert_ack(demo, scenario, final_report)
        evidence = {
            "scenario": f"ordinary-{case}-recovery",
            "boundary": boundary,
            "source_action_id": str(source_id),
            "coverage": coverage,
            "source_report_json": str(source_report_path),
            "final_report_json": (
                str(late_report_path) if late_id is not None else str(source_report_path)
            ),
            "scope_output_ids": expected_scope,
            "prepublication_waiter_action_ids": list(map(str, waiter_ids)),
            "bound_delivery_copies": len(bound_copies),
            "late_obtain_action_id": None if late_id is None else str(late_id),
            "post_h_waiter_was_in_commit_snapshot": (
                False if post_h_waiter_id is not None else None
            ),
            "resume_copy": resume_evidence,
            "delivery_evidence": delivery_evidence + post_h_evidence + late_evidence,
            "post_h_waiter_action_id": (
                None if post_h_waiter_id is None else str(post_h_waiter_id)
            ),
            "post_h_dependency": post_h_dependency,
            "post_h_delivery_evidence": post_h_evidence,
            "ffprobe": probes,
            "ack": late_report_receipt,
        }
        (deployment.root / f"action6-recovery-{boundary}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        demo.stop()


@pytest.mark.skipif(os.name != "posix", reason="安装版 POSIX C host 恢复链")
def test_installed_action6_native_required_photo_failure_keeps_reached_video_scope_and_waiters(
        tmp_path, host_demo, action6_package, monkeypatch,
):
    package = action6_package
    scenario = _prepare_installed_action6(
        tmp_path, package, native=True, include_jpeg=False,
    )
    deployment = scenario["deployment"]
    demo, source_id = _start_installed_capture(host_demo, package, scenario, monkeypatch)
    waiter_names = ("native-prefix-a", "native-prefix-b")
    waiter_ids = [
        _submit_obtain(
            demo, deployment, action_name=name, source_id=source_id,
            selection={"filter": "default"},
        )
        for name in waiter_names
    ]
    _wait_for_waiting_obtains(deployment, source_id, waiter_names)
    failure = demo.watch_failures()
    try:
        _release_clock(scenario, 330)
        report = wait_for(
            lambda: _report_with_statuses(
                deployment.ready,
                {"capture": "failed", waiter_names[0]: "succeeded", waiter_names[1]: "succeeded"},
            ),
            240,
            "Action6 native 必需照片缺失后未按真实处理器完成前缀检查和独立取回",
        )
        report_path = _save_report_snapshot(
            deployment, report, "action6-native-photo-failure-report"
        )
        source = _action(report, "capture")
        coverage = source["result"]["coverage"]
        assert coverage["metric"] == "frames"
        assert coverage["status"] == "reached"
        assert coverage["target"] == 151
        assert coverage["confirmed"] == 165
        assert len(coverage["checked_output_ids"]) == 3
        assert coverage["default_scope"]["member_count"] == 3
        assert coverage["default_scope"]["output_ids"] == coverage["checked_output_ids"]

        output_rows = _source_rows(deployment, source_id)
        expected_sha_by_output_id = _assert_source_manifest_facts(scenario, output_rows)
        expected_size_by_output_id = {
            row["output_id"]: row["size"] for row in output_rows.values()
        }
        _assert_inspection_copy_facts(deployment, scenario, source_id)
        video_paths = [item["device_path"] for item in scenario["facts"]]
        expected_scope = [output_rows[path]["output_id"] for path in video_paths[:3]]
        assert coverage["checked_output_ids"] == expected_scope
        assert [str(row[1]) for row in _coverage_rows(deployment, source_id)] == expected_scope
        assert len([path for path in output_rows if path.endswith(".MP4")]) == 4
        assert len([path for path in output_rows if path.endswith(".LRF")]) == 4
        assert not any(path.endswith(".JPG") for path in output_rows)

        probes = _ffprobe_records(deployment)
        assert len(probes) == 3
        assert all(item["real_tool"] and item["returncode"] == 0 for item in probes)
        assert all("-count_frames" in item["argv"] for item in probes)
        _assert_probe_input_facts(probes, scenario["facts"][:3], scenario["contents"])
        calls = _records(deployment.root / "adb-calls.jsonl")
        assert len([call for call in calls if call.get("kind") == "start"]) == 1
        assert len([call for call in calls if call.get("kind") == "stop"]) == 0

        demo.claim()
        deployment.import_reports_with_client(deployment.processing)
        delivery_map = {
            path: {
                **output_rows[path],
                "content": scenario["contents"][path],
            }
            for path in video_paths[:3]
        }
        delivery_evidence = []
        for waiter_name in waiter_names:
            delivery_evidence.extend(_assert_client_delivery_files(
                deployment, scenario["store"], report, waiter_name, delivery_map
            ))
        bound_copies = query_state(
            deployment.state_db,
            "SELECT d.action_id,d.output_id,d.id,c.source_device_file_id,c.source_intermediate_file_id,"
            "c.verification_state,c.committed_bytes,c.source_size,c.target_sha256 "
            "FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id "
            "WHERE d.action_id IN (?,?) ORDER BY d.action_id,d.output_id",
            tuple(waiter_ids),
        )
        assert len(bound_copies) == 6
        assert all(row[3] is None and row[4] is not None for row in bound_copies)
        assert len({row[2] for row in bound_copies}) == 6
        verified = {
            int(enum_for("file_copies.verification_state").MATCHED),
            int(enum_for("file_copies.verification_state").SOURCE_CHECKSUM_UNAVAILABLE),
        }
        assert all(row[5] in verified for row in bound_copies)
        assert all(
            row[6] == row[7] == expected_size_by_output_id[str(row[1])]
            and row[8] == expected_sha_by_output_id[str(row[1])]
            for row in bound_copies
        )
        _cleanup_inspection_inputs(deployment, source_id, failure)
        ack = _assert_ack(demo, scenario, report)
        (deployment.root / "action6-native-photo-failure.json").write_text(
            json.dumps({
                "scenario": "native-required-photo-failure",
                "source_action_id": str(source_id),
                "coverage": coverage,
                "report_json": str(report_path),
                "scope_output_ids": expected_scope,
                "prepublication_waiter_action_ids": list(map(str, waiter_ids)),
                "delivery_evidence": delivery_evidence,
                "ffprobe": probes,
                "ack": ack,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        demo.stop()


def _read_video_templates(package, deployment):
    result = _run(
        [
            str(package.python), "-c",
            "import json; from camctl.resources import resource_bytes; "
            "names=('action6-record.json','action6-timelapse.json',"
            "'osmo360ii-record.json','osmo360ii-timelapse.json'); "
            "print(json.dumps({name:resource_bytes('examples/camera-demo/'+name).decode() "
            "for name in names}))",
        ],
        cwd=deployment.root,
        env=package.environment,
    )
    return {
        name: json.loads(contents)
        for name, contents in json.loads(result.stdout).items()
    }


def _write_modes(
        deployment, base_config: str, modes: dict[str, str], *,
        explicit_single: bool = False,
) -> None:
    rows = [base_config.rstrip(), ""]
    for device_id, driver, serial in (
        ("action6_single", "dji-action6", "action6-single-serial"),
        ("action6_segmented", "dji-action6", "action6-segmented-serial"),
        ("osmo_single", "dji-osmo360-ii", "osmo-single-serial"),
        ("osmo_segmented", "dji-osmo360-ii", "osmo-segmented-serial"),
    ):
        rows.extend((
            f"[devices.{device_id}]",
            'kind = "camera"',
            f'driver = "{driver}"',
            f"[devices.{device_id}.adb]",
            f'serial = "{serial}"',
        ))
        if modes[device_id] != "single_file" or explicit_single:
            rows.extend((
                f"[devices.{device_id}.video]",
                f'result_mode = "{modes[device_id]}"',
            ))
        rows.append("")
    deployment.config_path.write_text("\n".join(rows), encoding="utf-8")


def _matrix_actions(templates, phase: str, scheduled_at: str) -> list[dict]:
    devices = (
        ("action6_single", "action6-record.json", "camera_record"),
        ("action6_single", "action6-timelapse.json", "camera_timelapse"),
        ("action6_segmented", "action6-record.json", "camera_record"),
        ("action6_segmented", "action6-timelapse.json", "camera_timelapse"),
        ("osmo_single", "osmo360ii-record.json", "camera_record"),
        ("osmo_single", "osmo360ii-timelapse.json", "camera_timelapse"),
        ("osmo_segmented", "osmo360ii-record.json", "camera_record"),
        ("osmo_segmented", "osmo360ii-timelapse.json", "camera_timelapse"),
    )
    actions = []
    for device_id, template_name, action_type in devices:
        template_action = templates[template_name]["actions"][0]
        name = f"{phase}-{device_id}-{action_type}"
        actions.append({
            "name": name,
            "type": action_type,
            "device_id": device_id,
            "scheduled_at": scheduled_at,
            "params": template_action["params"],
            "policy": {"max_delay_ms": 30000},
        })
    return actions


@pytest.mark.skipif(os.name != "posix", reason="安装版 POSIX 测试链")
def test_installed_video_modes_freeze_for_both_cameras_and_both_video_actions(
    tmp_path, action6_package,
):
    package = action6_package
    deployment = Deployment(tmp_path / "video-result-mode-matrix", devices=False)
    templates = _read_video_templates(package, deployment)
    base_config = deployment.config_path.read_text(encoding="utf-8")
    initial_modes = {
        "action6_single": "single_file",
        "action6_segmented": "segmented",
        "osmo_single": "single_file",
        "osmo_segmented": "segmented",
    }
    _write_modes(deployment, base_config, initial_modes)
    initialized = _run(
        [str(package.camctl), "init", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=package.environment,
    )
    described = _run(
        [str(package.camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=package.environment,
    )
    assert initialized.returncode == 0 and described.returncode == 0
    client_store = deployment.root / "client-store"
    client_store.mkdir()
    (client_store / "device-capabilities.json").write_text(
        described.stdout, encoding="utf-8",
    )

    def accept(phase: str, modes: dict[str, str]) -> None:
        now = datetime.now(timezone.utc)
        scheduled_at = (now + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "name": f"installed-mode-{phase}",
            "actions": _matrix_actions(templates, phase, scheduled_at),
        }
        plan, _ = deployment.export_plan_with_client(body)
        submitted = _run(
            [str(package.camctl), "submit", str(plan), "--config", str(deployment.config_path)],
            cwd=deployment.root,
            env=package.environment,
        )
        assert submitted.returncode == 0, submitted.stderr
        rows = query_state(
            deployment.state_db,
            "SELECT name,execution_spec_json FROM actions WHERE name LIKE ? ORDER BY name",
            (f"{phase}-%",),
        )
        assert len(rows) == 8
        for name, encoded_spec in rows:
            _phase, device_id, action_type = name.split("-", 2)
            assert action_type in {"camera_record", "camera_timelapse"}
            spec = json.loads(encoded_spec)
            assert spec["video_result_mode"] == modes[device_id]
            assert ("result_policy" in spec) is (
                modes[device_id] == "segmented"
            )

    accept("before", initial_modes)

    switched_modes = {
        "action6_single": "segmented",
        "action6_segmented": "single_file",
        "osmo_single": "segmented",
        "osmo_segmented": "single_file",
    }
    _write_modes(deployment, base_config, switched_modes, explicit_single=True)
    switched_description = _run(
        [str(package.camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root,
        env=package.environment,
    )
    assert switched_description.returncode == 0
    (client_store / "device-capabilities.json").write_text(
        switched_description.stdout, encoding="utf-8",
    )
    accept("after", switched_modes)

    old_rows = query_state(
        deployment.state_db,
        "SELECT name,execution_spec_json FROM actions WHERE name LIKE 'before-%' ORDER BY name",
    )
    assert len(old_rows) == 8
    for name, encoded_spec in old_rows:
        _phase, device_id, _action_type = name.split("-", 2)
        assert json.loads(encoded_spec)["video_result_mode"] == initial_modes[device_id]
