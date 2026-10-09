"""显式目录切换保留真实业务历史，并在进程中断后核验整套绑定。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.host_files.handoff import HandoffDirectories
from camctl.persistence.initialization import InitOutcome, _canonical_binding, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.models import SyncMode
from camctl.reporting.policy import ReportingRepository, start_sync
from camctl.reporting.publication import DbPublicationSession, DeliveryOutcome, deliver_report

from ..acceptance.test_atomicity import _process
from ..history.test_report_scope import _finish_action, _submit
from .test_initialization import _config, _dump


async def _completed_history(tmp_path: Path):
    cfg = _config(tmp_path)
    db_path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db_path).outcome is InitOutcome.CREATED
    owned = open_existing(db_path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        accepted = _process({
            "request_id": "2", "created_at": "2026-01-15 08:00:00",
            "name": "同步", "actions": [{
                "name": "同步", "type": "report_status", "params": {"scope": "full"},
            }],
        }, owned.connection)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        started = start_sync(new_operation_key(), owned, action_id=2,
                             mode=SyncMode.FULL, occurred_at=2)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        frozen = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=1)
        assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
        report = frozen.value.report
        spec = GenerationSpec(
            db_path=db_path, report_id=report.report_id,
            from_wm=report.from_wm, to_wm=report.to_wm,
            frozen_event_id=report.boundary.last_event_id,
            staging_path=tmp_path / "original-report.json")
        generated = generate_report_file(spec)
        original = generated.path.read_bytes()
        document = json.loads(original)
        action = document["plans"][0]["actions"][0]
        assert action["action_instance_id"] == "1"
        assert action["status"] == "succeeded"
        assert action["outputs"][0]["output_id"] == "1"
        assert action["outputs"][0]["availability"] == "available"
        publication = await deliver_report(
            report.report_id, original,
            HandoffDirectories(Path(cfg.paths.staging), Path(cfg.paths.ready)),
            DbPublicationSession(owned), local_actions=(2,))
        assert publication.outcome is DeliveryOutcome.PUBLISHED, publication.error
        acknowledged = _process(
            {"request_id": "1", "last_report_id": str(report.report_id)}, owned.connection)
        assert acknowledged.kind is DbOutcomeKind.COMPLETED, acknowledged.error
        assert owned.connection.execute(
            "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
        ).fetchone() == (report.to_wm, report.report_id)
        assert owned.connection.execute(
            "SELECT action_id, status, local_report_id, ack_report_id FROM state_syncs"
        ).fetchone() == (2, 2, report.report_id, report.report_id)
    finally:
        owned.connection.close()
    # 模拟主程序完成领取与传输，只移除它拥有的已发布副本。
    files = list(Path(cfg.paths.ready).iterdir())
    assert len(files) == 1
    claimed = Path(cfg.paths.processing) / files[0].name
    files[0].rename(claimed)
    claimed.unlink()
    return cfg, report, original


def _assert_business_unchanged(before, after, moved):
    for table, rows in before.items():
        if table != "database_metadata":
            assert after[table] == rows, table
    old_meta, new_meta = before["database_metadata"][0], after["database_metadata"][0]
    assert new_meta[:4] == old_meta[:4]
    assert new_meta[4:7] == tuple(_canonical_binding(Path(value)) for value in (
        moved.paths.staging, moved.paths.ready, moved.paths.processing))
    assert new_meta[7:] == old_meta[7:]
    assert before["reports"] and before["device_files"] and before["outputs"]
    assert before["state_syncs"]


@pytest.mark.asyncio
async def test_switch_preserves_report_bytes_ack_and_file_history(tmp_path):
    cfg, report, original = await _completed_history(tmp_path)
    db_path = Path(cfg.paths.state_db)
    repository = HistoryRepository(db_path)
    image = repository.restore_entity("action", 1, report.boundary)
    before = _dump(db_path)
    moved = _config(tmp_path, prefix="new-")

    assert initialize_state(moved, db_path).outcome is InitOutcome.SWITCHED

    _assert_business_unchanged(before, _dump(db_path), moved)
    assert repository.restore_entity("action", 1, report.boundary) == image
    rebuilt = generate_report_file(GenerationSpec(
        db_path=db_path, report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=Path(moved.paths.staging) / "reports" / "rebuilt.json",
        entity_batch_size=1, event_batch_size=1))
    assert rebuilt.path.read_bytes() == original
    rebuilt.path.unlink()
    _assert_business_unchanged(before, _dump(db_path), moved)
    assert initialize_state(moved, db_path).outcome is InitOutcome.VERIFIED_EXISTING


_INTERRUPTED_INIT = """
import json, os, sys
from dataclasses import replace
from pathlib import Path
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.persistence import initialization

phase, paths = sys.argv[1], json.loads(sys.argv[2])
original_open = initialization.open_existing
class InterruptedConnection:
    def __init__(self, inner): self.inner = inner
    def execute(self, sql, *args):
        if sql.strip().upper() == "COMMIT":
            if phase == "before": os._exit(79)
            self.inner.execute(sql, *args)
            os._exit(79)
        return self.inner.execute(sql, *args)
    def close(self): self.inner.close()
def interrupted_open(*args, **kwargs):
    owned = original_open(*args, **kwargs)
    return replace(owned, connection=InterruptedConnection(owned.connection))
initialization.open_existing = interrupted_open
config = load_config({"paths": paths}, ConfigDefaults())
initialization.initialize_state(config, Path(paths["state_db"]))
raise RuntimeError("未到达注入边界")
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_process_exit_preserves_whole_binding_and_business(tmp_path, phase):
    cfg, _, _ = await _completed_history(tmp_path)
    db_path = Path(cfg.paths.state_db)
    before = _dump(db_path)
    moved = _config(tmp_path, prefix="new-")
    paths = {name: getattr(moved.paths, name) for name in (
        "state_db", "log_file", "staging", "ready", "processing")}
    child = subprocess.run(
        [sys.executable, "-c", _INTERRUPTED_INIT, phase, json.dumps(paths)],
        capture_output=True, timeout=30)
    assert child.returncode == 79, child.stderr.decode()
    expected = moved if phase == "after" else cfg
    _assert_business_unchanged(before, _dump(db_path), expected)
    # 进程退出释放同一状态库的会话锁；新进程可从整套绑定继续。
    retry = initialize_state(moved, db_path)
    assert retry.outcome is (
        InitOutcome.VERIFIED_EXISTING if phase == "after" else InitOutcome.SWITCHED)
    _assert_business_unchanged(before, _dump(db_path), moved)
