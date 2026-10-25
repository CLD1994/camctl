"""独立报告格式升级只扩展 reports 约束，保留全部旧事实。"""
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from camctl.persistence.initialization import initialize_state, InitOutcome
from camctl.persistence.runtime import canonical_directory_binding, DbConfig, DbOpenMode, open_existing, report_format_versions
from camctl.reporting.policy import ReportingRepository
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from .test_software_recovery_upgrade import config
from . import test_runtime
from ..reporting.test_status_history import admit


def legacy(tmp_path, monkeypatch, format_version=5):
    cfg = config(tmp_path)
    path = Path(cfg.paths.state_db)
    original = test_runtime.resource_bytes
    def resource(name):
        value = original(name)
        if name == "sql/reports.sql":
            value = value.replace(b"format_version IN (1,2)", b"format_version = 1", 1)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(test_runtime, "resource_bytes", resource)
        test_runtime._create_valid_database(path, format_version=format_version,
            directories=tuple(canonical_directory_binding(Path(value)) for value in (cfg.paths.staging, cfg.paths.ready, cfg.paths.processing)))
    return cfg, path


def contents(path):
    with closing(sqlite3.connect(path)) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in names}


@pytest.mark.parametrize("format_version", [1, 5])
def test_explicit_upgrade_preserves_metadata_history_rows_and_backup(tmp_path, monkeypatch, format_version):
    cfg, path = legacy(tmp_path, monkeypatch, format_version)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        admit(owned)
        outcome = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=3_000_000)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert "init --upgrade-report-format" in str(outcome.error)
    finally:
        owned.connection.close()
    before = contents(path)
    assert initialize_state(cfg, path).outcome is InitOutcome.VERIFIED_EXISTING
    assert contents(path) == before
    result = initialize_state(cfg, path, upgrade_report_format=True)
    assert result.outcome is InitOutcome.VERIFIED_EXISTING, result
    assert contents(path) == before
    backups = list(path.parent.glob(path.name + ".before-report-format-*.sqlite"))
    assert len(backups) == 1 and contents(backups[0]) == before
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert report_format_versions(owned.connection) == {1, 2}
        outcome = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=3_000_000)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.report.format_version == 2
    finally:
        owned.connection.close()
    assert initialize_state(cfg, path, upgrade_report_format=True).outcome is InitOutcome.VERIFIED_EXISTING
    assert len(list(path.parent.glob(path.name + ".before-report-format-*.sqlite"))) == 1


def test_upgrade_options_are_mutually_exclusive(tmp_path):
    cfg = config(tmp_path)
    result = initialize_state(cfg, Path(cfg.paths.state_db), upgrade_report_format=True, upgrade_capture_preparation=True)
    assert result.outcome is InitOutcome.FAILED
    assert not Path(cfg.paths.state_db).exists()




def test_existing_frozen_report_keeps_bytes_sha_identity_and_replay_after_upgrade(tmp_path, monkeypatch):
    from camctl.reporting import models
    from camctl.reporting.models import ReportBytes
    from camctl.reporting.policy import record_report_bytes
    from ..reporting.test_status_history import generate
    import hashlib
    cfg, path = legacy(tmp_path, monkeypatch)
    key = new_operation_key()
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        admit(owned)
        with monkeypatch.context() as patch:
            patch.setattr(models, "CURRENT_REPORT_FORMAT", 1)
            report = ReportingRepository().freeze_report(key, owned, occurred_at=3_000_000).value.report
        before_bytes = generate(tmp_path, report)
        digest = hashlib.sha256(before_bytes).hexdigest()
        outcome = record_report_bytes(new_operation_key(), owned, report.report_id, ReportBytes(len(before_bytes), digest), occurred_at=4_000_000)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    finally:
        owned.connection.close()
    before = contents(path)
    result = initialize_state(cfg, path, upgrade_report_format=True)
    assert result.outcome is InitOutcome.VERIFIED_EXISTING, result
    assert contents(path) == before
    assert generate(tmp_path, report) == before_bytes
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        replay = ReportingRepository().freeze_report(key, owned, occurred_at=3_000_000)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value.report == report
        assert owned.connection.execute("SELECT id,format_version,size_bytes,sha256 FROM reports").fetchone() == (report.report_id, 1, len(before_bytes), digest)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("published_before_error", [False, True])
def test_upgrade_reconciles_actual_replace_result_and_keeps_backup(tmp_path, monkeypatch, published_before_error):
    from camctl.persistence import report_format_upgrade as module
    cfg, path = legacy(tmp_path, monkeypatch)
    before = contents(path)
    actual = module.os.replace
    def replace(source, target):
        if published_before_error:
            actual(source, target)
        raise OSError("replace result unknown")
    monkeypatch.setattr(module.os, "replace", replace)
    result = initialize_state(cfg, path, upgrade_report_format=True)
    assert result.outcome is (InitOutcome.VERIFIED_EXISTING if published_before_error else InitOutcome.FAILED)
    assert contents(path) == before
    backups = list(path.parent.glob(path.name + ".before-report-format-*.sqlite"))
    assert len(backups) == 1 and contents(backups[0]) == before
    owned = open_existing(path, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        assert report_format_versions(owned.connection) == ({1, 2} if published_before_error else {1})
    finally:
        owned.connection.close()
