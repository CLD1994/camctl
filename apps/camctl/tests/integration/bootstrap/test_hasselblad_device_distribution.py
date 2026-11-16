"""安装版 device 源按原机内身份回收工作副本并重复取回两种格式。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from camctl.contracts.enums import enum_for
from apps.camctl.tests.integration.jpeg_fixture import JPEG_IMAGE

from .test_distribution import (
    _HASSELBLAD_PHOTO_SAMPLE_PARAMS,
    _assert_hasselblad_capture_trajectory,
    _hasselblad_capture_fixture,
    _hasselblad_tool_source,
    _production_cli,
    _published_reports,
    installed,
)


@pytest.mark.acceptance
@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
def test_installed_device_photo_reclaims_then_downloads_both_sources_twice(
        installed, tmp_path, capture_format):
    home = tmp_path / "camera-source"
    config = tmp_path / "device.toml"
    tool = tmp_path / "hb_cli"
    log = tmp_path / "calls.jsonl"
    tool.write_text(_hasselblad_tool_source(log, "SERIAL-1", installed_python=installed.python))
    tool.chmod(0o700)
    config.write_text(f'''[paths]
state_db="{home}/state.db"
log_file="{home}/camctl.log"
staging="{home}/staging"
ready="{home}/ready"
processing="{home}/processing"
[devices.hb0]
kind="camera"
driver="hasselblad_cfv100c"
[devices.hb0.hasselblad]
executable="{tool}"
serial="SERIAL-1"
output_storage="device"
[devices.hb0.copy]
max_read_attempts=1
retry_interval_s="0"
''')
    init = _production_cli(installed, "init", "--config", str(config))
    assert init.returncode == 0, (init.stdout, init.stderr)

    def run_plan(number, actions):
        now = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        for action in actions:
            action["scheduled_at"] = now
        plan = tmp_path / f"plan-{number}.json"
        plan.write_text(json.dumps({
            "request_id": str(number), "created_at": now,
            "name": f"业务{number}", "actions": actions,
        }), encoding="utf-8")
        result = _production_cli(installed, "run", str(plan), "--config", str(config))
        assert result.returncode == 0, (result.stdout, result.stderr)

    sample_params = deepcopy(_HASSELBLAD_PHOTO_SAMPLE_PARAMS)
    sample_params["capture_format"] = capture_format
    run_plan(1, [{
        "name": "capture", "type": "camera_take_photo", "device_id": "hb0",
        "params": sample_params, "policy": {"max_delay_ms": 60000},
    }])

    database = home / "state.db"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT status FROM actions").fetchall() == [(3,)]
        sources = connection.execute(
            "SELECT id,size_bytes,sha256,locator_json FROM device_files ORDER BY id"
        ).fetchall()
        assert len(sources) == 2
        outputs = connection.execute(
            "SELECT id,device_file_id,intermediate_file_id,original_name"
            " FROM outputs ORDER BY id"
        ).fetchall()
        assert [(row[1], row[2]) for row in outputs] == [
            (sources[0][0], None), (sources[1][0], None),
        ]
        assert outputs[0][0] != outputs[1][0]
        local = connection.execute(
            "SELECT relative_path,retention_state,cleanup_state"
            " FROM intermediate_files ORDER BY id"
        ).fetchall()
        assert len(local) == 2 and all(row[1:] == (2, 4) for row in local)
        assert all(not (home / "staging" / row[0]).exists() for row in local)

    expected_capture = _hasselblad_capture_fixture(capture_format)
    expected = (expected_capture["original_bytes"], expected_capture["preview_bytes"])
    assert expected_capture["camera_name"] != expected_capture["original_name"]
    assert [(row[1], row[2]) for row in sources] == [(None, None), (None, None)]
    locators = [json.loads(row[3]) for row in sources]
    assert [locator["variant"] for locator in locators] == ["original", "thumbnail"]
    assert all(locator["camera_path"] == expected_capture["camera_path"]
               for locator in locators)
    assert all(locator["camera_name"] == expected_capture["camera_name"]
               for locator in locators)
    assert [row[3] for row in outputs] == [
        expected_capture["camera_name"], expected_capture["camera_name"] + ".thumbnail.jpg",
    ]

    reports = _published_reports(home / "ready")
    photo = next(
        action for _, _, report in reports
        for plan in report["plans"] for action in plan["actions"]
        if action["name"] == "capture"
    )
    output_pair = {output["kind"]: output for output in photo["outputs"]}
    assert set(output_pair) == {"original", "preview"}
    assert output_pair["original"]["output_id"] != output_pair["preview"]["output_id"]
    assert output_pair["preview"]["preview_of_output_id"] == output_pair["original"]["output_id"]
    for kind in ("original", "preview"):
        assert "media_type" not in output_pair[kind]
        assert output_pair[kind]["checksum"] == {"status": "not_obtained"}

    output_ids = [int(row[0]) for row in outputs]
    assert [int(output_pair[kind]["output_id"]) for kind in ("original", "preview")] == output_ids
    config.write_text(config.read_text().replace(
        'output_storage="device"', 'output_storage="host"'))
    delivery_ids = set()
    target_file_ids = set()
    file_names = set()
    first_by_output = {}
    for number in (2, 3):
        run_plan(number, [
            {"name": f"original-{number}", "type": "obtain_action_outputs",
             "params": {"source": {"action_instance_id": "1"}}},
            {"name": f"preview-{number}", "type": "obtain_action_outputs",
             "params": {"source": {"action_instance_id": "1"}, "filter": "preview"}},
        ])
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT d.id,d.output_id,d.file_name,c.target_file_id,t.size_bytes,t.sha256"
                " FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id"
                " JOIN intermediate_files t ON t.id=c.target_file_id"
                " JOIN actions a ON a.id=d.action_id WHERE a.plan_id=? ORDER BY d.output_id",
                (number,),
            ).fetchall()
            statuses = connection.execute(
                "SELECT status FROM actions WHERE plan_id=? ORDER BY id", (number,),
            ).fetchall()
        assert statuses == [(3,), (3,)]
        assert len(rows) == 2
        for row, output_id, content in zip(rows, output_ids, expected):
            delivery_id, actual_output_id, file_name, target_id, size_bytes, sha256 = row
            digest = hashlib.sha256(content).hexdigest()
            assert actual_output_id == output_id
            assert delivery_id not in delivery_ids
            assert target_id not in target_file_ids
            assert file_name not in file_names
            assert size_bytes == len(content)
            assert sha256 == digest
            target = home / "ready" / file_name
            assert target.read_bytes() == content
            previous = first_by_output.get(output_id)
            if previous is None:
                first_by_output[output_id] = (delivery_id, target_id, file_name)
            else:
                assert delivery_id != previous[0]
                assert target_id != previous[1]
                assert file_name != previous[2]
            delivery_ids.add(delivery_id)
            target_file_ids.add(target_id)
            file_names.add(file_name)
            target.unlink()
        assert not list((home / "staging" / "read-downloads").iterdir())

    assert len(delivery_ids) == len(target_file_ids) == len(file_names) == 4
    assert len(first_by_output) == 2
    calls = _assert_hasselblad_capture_trajectory(log, capture_format)
    assert [call[4:6] for call in calls if len(call) > 3 and call[3] == "download"] == [
        ["original", expected_capture["camera_name"]],
        ["thumbnail", expected_capture["camera_name"]],
    ] * 2

    # 每次设备下载独立形成交付事实，不改写原来源身份。
    original_tool = tool.read_text()
    later_preview = JPEG_IMAGE
    assert later_preview != expected[1]
    marker = 'payload = original_bytes if args[3] == "original" else PREVIEW'
    assert marker in original_tool
    tool.write_text(original_tool.replace(
        marker,
        f'payload = original_bytes if args[3] == "original" else bytes.fromhex({later_preview.hex()!r})',
    ))
    run_plan(4, [
        {"name": "original-4", "type": "obtain_action_outputs",
         "params": {"source": {"action_instance_id": "1"}}},
        {"name": "changed-preview", "type": "obtain_action_outputs",
         "params": {"source": {"action_instance_id": "1"}, "filter": "preview"}},
    ])
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT status FROM actions WHERE plan_id=4 ORDER BY id"
        ).fetchall() == [(3,), (3,)]
        changed = connection.execute(
            "SELECT d.id,d.output_id,d.file_name,d.status,t.id,t.size_bytes,t.sha256"
            " FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id"
            " JOIN intermediate_files t ON t.id=c.target_file_id"
            " JOIN actions a ON a.id=d.action_id WHERE a.plan_id=4 ORDER BY d.output_id"
        ).fetchall()
        assert connection.execute(
            "SELECT id,size_bytes,sha256,locator_json FROM device_files ORDER BY id"
        ).fetchall() == sources
    assert len(changed) == 2
    for (delivery_id, output_id, file_name, status, target_id, size, sha256), expected_id, actual in zip(
        changed, output_ids, (expected[0], later_preview)
    ):
        assert output_id == expected_id and status == int(enum_for("deliveries.status").PUBLISHED)
        assert delivery_id not in delivery_ids and target_id not in target_file_ids and file_name not in file_names
        assert size == len(actual) and sha256 == hashlib.sha256(actual).hexdigest()
        assert (home / "ready" / file_name).read_bytes() == actual
    assert not list((home / "staging" / "read-downloads").iterdir())
    _assert_hasselblad_capture_trajectory(log, capture_format)
