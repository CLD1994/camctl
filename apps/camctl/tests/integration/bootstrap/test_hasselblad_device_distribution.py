"""安装包正式入口完成相机捕获、离线本地回收、重复原图和预览取回。"""
from camctl.contracts.enums import enum_for

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .test_distribution import (
    installed, _production_cli, _hasselblad_tool_source, _published_reports,
    _HASSELBLAD_ORIGINAL_BYTES, _HASSELBLAD_PREVIEW_BYTES,
)


def test_installed_device_photo_reclaims_then_downloads_both_sources_twice(installed, tmp_path):
    home = tmp_path / "camera-source"
    config = tmp_path / "device.toml"
    tool = tmp_path / "hb_cli"
    log = tmp_path / "calls.jsonl"
    tool.write_text(_hasselblad_tool_source(log, "SERIAL-1"))
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
    init = _production_cli(installed,"init","--config",str(config))
    assert init.returncode == 0, (init.stdout,init.stderr)
    def run_plan(number,actions):
        now=(datetime.now(timezone.utc)-timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        for action in actions:
            action["scheduled_at"]=now
        plan=tmp_path/f"plan-{number}.json"
        plan.write_text(json.dumps({"request_id":str(number),"created_at":now,"name":f"业务{number}","actions":actions}))
        result=_production_cli(installed,"run",str(plan),"--config",str(config))
        assert result.returncode==0,(result.stdout,result.stderr)
    run_plan(1,[{"name":"capture","type":"camera_take_photo","device_id":"hb0",
                 "params":{"type":"hasselblad_photo"},"policy":{"max_delay_ms":60000}}])
    database=home/"state.db"
    with closing(sqlite3.connect(database)) as c:
        assert c.execute("SELECT status FROM actions").fetchall()==[(3,)]
        sources=c.execute("SELECT id,size_bytes,sha256,locator_json FROM device_files ORDER BY id").fetchall()
        assert len(sources)==2
        outputs=c.execute("SELECT id,device_file_id,intermediate_file_id FROM outputs ORDER BY id").fetchall()
        assert [(r[1],r[2]) for r in outputs]==[(sources[0][0],None),(sources[1][0],None)]
        local=c.execute("SELECT relative_path,retention_state,cleanup_state FROM intermediate_files ORDER BY id").fetchall()
        assert len(local)==2 and all(r[1:]==(2,4) for r in local)
        assert all(not (home/"staging"/r[0]).exists() for r in local)
    expected=(_HASSELBLAD_ORIGINAL_BYTES,_HASSELBLAD_PREVIEW_BYTES)
    assert [(r[1],r[2]) for r in sources]==[(len(b),hashlib.sha256(b).hexdigest()) for b in expected]
    assert [json.loads(r[3])["variant"] for r in sources]==["original","thumbnail"]
    # 后续部署改变只影响未来新拍摄；原相机源仍按首次身份下载。
    config.write_text(config.read_text().replace('output_storage="device"','output_storage="host"'))
    delivery_ids=set()
    for number in (2,3):
        run_plan(number,[{"name":f"original-{number}","type":"obtain_action_outputs",
            "params":{"source":{"action_instance_id":"1"}}},
            {"name":f"preview-{number}","type":"obtain_action_outputs",
             "params":{"source":{"action_instance_id":"1"},"filter":"preview"}}])
        with closing(sqlite3.connect(database)) as c:
            rows=c.execute("SELECT d.id,d.output_id,d.file_name FROM deliveries d JOIN actions a ON a.id=d.action_id WHERE a.plan_id=? ORDER BY d.output_id",(number,)).fetchall()
            assert c.execute("SELECT status FROM actions WHERE plan_id=? ORDER BY id",(number,)).fetchall()==[(3,),(3,)]
        assert len(rows)==2
        for row,content in zip(rows,expected):
            assert row[0] not in delivery_ids
            delivery_ids.add(row[0])
            target=home/"ready"/row[2]
            assert target.read_bytes()==content
            target.unlink()
        assert not list((home/"staging"/"read-downloads").iterdir())
    assert len(delivery_ids)==4
    calls=[json.loads(line) for line in log.read_text().splitlines()]
    assert len([c for c in calls if c[3]=="capture"])==1
    assert [c[4:6] for c in calls if c[3]=="download"]==[["original","fixture_capture.3FR"],["thumbnail","fixture_capture.3FR"]]*2
    reports=_published_reports(home/"ready")
    photo=next(a for _,_,r in reports for p in r["plans"] for a in p["actions"] if a["name"]=="capture")
    pair={o["kind"]:o for o in photo["outputs"]}
    assert pair["preview"]["preview_of_output_id"]==pair["original"]["output_id"]
    for kind,content in zip(("original","preview"),expected):
        assert pair[kind]["checksum"]=={"status":"available","sha256":hashlib.sha256(content).hexdigest()}

    # 相机返回等长但内容不同的下载，固定源摘要不得被覆盖，也不得交付。
    tool.write_text(tool.read_text().replace(
        'write_bytes(RAW if args[3] == "original" else PREVIEW)',
        'write_bytes(b"x" * len(RAW) if args[3] == "original" else b"x" * len(PREVIEW))'))
    run_plan(4,[{"name":"corrupt-original","type":"obtain_action_outputs",
        "params":{"source":{"action_instance_id":"1"}}},
        {"name":"corrupt-preview","type":"obtain_action_outputs",
         "params":{"source":{"action_instance_id":"1"},"filter":"preview"}}])
    with closing(sqlite3.connect(database)) as c:
        assert c.execute("SELECT status FROM actions WHERE plan_id=4 ORDER BY id").fetchall()==[(4,),(4,)]
        assert c.execute("SELECT d.status FROM deliveries d JOIN actions a ON a.id=d.action_id WHERE a.plan_id=4").fetchall()==[(enum_for("deliveries.status").FAILED,)]*2
        assert c.execute("SELECT id,size_bytes,sha256,locator_json FROM device_files ORDER BY id").fetchall()==sources
    assert not list((home/"staging"/"read-downloads").iterdir())
    assert not list((home/"ready").glob("*.3fr"))
    assert not list((home/"ready").glob("*.jpg"))
