"""显式软件恢复格式升级保留原业务历史及旧格式权限。"""
from pathlib import Path
import sqlite3
import hashlib

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.persistence.initialization import initialize_state, InitOutcome
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from .test_runtime import _create_valid_database
from .test_transactions import PlanCreateCommand, plan_guards


def config(tmp_path):
    return load_config({"paths": {"state_db": str(tmp_path / "state.db"),
        "staging": str(tmp_path / "staging"), "ready": str(tmp_path / "ready"),
        "processing": str(tmp_path / "processing"), "log_file": str(tmp_path / "run.log")}}, ConfigDefaults())


def legacy(tmp_path):
    cfg = config(tmp_path)
    path = Path(cfg.paths.state_db)
    _create_valid_database(path, format_version=2,
        directories=(cfg.paths.staging, cfg.paths.ready, cfg.paths.processing))
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE software_recoveries")
    return cfg, path


def test_ordinary_init_preserves_v2_and_explicit_upgrade_keeps_identity_and_backup(tmp_path):
    cfg, path = legacy(tmp_path)
    assert initialize_state(cfg, path).outcome is InitOutcome.VERIFIED_EXISTING
    with sqlite3.connect(path) as connection:
        metadata = connection.execute("SELECT * FROM database_metadata").fetchone()
        assert metadata[3] == 2
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='software_recoveries'").fetchone() is None
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.VERIFIED_EXISTING
    upgraded = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert upgraded.metadata.format_version == 3 and upgraded.metadata.instance_id == metadata[2]
        assert upgraded.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == (0,)
        assert upgraded.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.connection.close()
    with sqlite3.connect(str(path) + ".before-software-recovery-v3.sqlite") as backup:
        assert backup.execute("SELECT * FROM database_metadata").fetchone() == metadata
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.VERIFIED_EXISTING


def test_explicit_upgrade_rejects_combined_directory_switch(tmp_path):
    cfg, path = legacy(tmp_path)
    changed = load_config({"paths": {"state_db": str(path),
        "staging": str(tmp_path / "new-staging"), "ready": cfg.paths.ready,
        "processing": cfg.paths.processing, "log_file": cfg.paths.log_file}}, ConfigDefaults())
    assert initialize_state(changed, path, upgrade_software_recovery=True).outcome is InitOutcome.FAILED
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT format_version,staging_path FROM database_metadata").fetchone() == (2, cfg.paths.staging)


def test_upgrade_flag_on_new_database_creates_current_format_without_backup(tmp_path):
    cfg = config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.CREATED
    assert not Path(str(path) + ".before-software-recovery-v3.sqlite").exists()


def test_upgrade_publish_race_preserves_explicit_flag(tmp_path, monkeypatch):
    import camctl.persistence.initialization as module
    cfg = config(tmp_path)
    path = Path(cfg.paths.state_db)
    original_link = module.os.link
    def publish_other(source, destination):
        _create_valid_database(path, format_version=2,
            directories=(cfg.paths.staging, cfg.paths.ready, cfg.paths.processing))
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE software_recoveries")
        raise FileExistsError(str(destination))
    monkeypatch.setattr(module.os, "link", publish_other)
    result = initialize_state(cfg, path, upgrade_software_recovery=True)
    assert result.outcome is InitOutcome.VERIFIED_EXISTING
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT format_version FROM database_metadata").fetchone() == (3,)
    assert Path(str(path) + ".before-software-recovery-v3.sqlite").exists()


def _record_history(path):
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.transaction import commit_operation
    opened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert commit_operation(PlanCreateCommand((7,)), new_operation_key(), opened).kind == "completed"
        return opened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    finally:
        opened.connection.close()


def _intercept_upgrade_connection(monkeypatch, change):
    import camctl.persistence.initialization as module
    from dataclasses import replace
    original_open = module.open_existing
    class Connection:
        def __init__(self, actual): self.actual = actual
        def __getattr__(self, name): return getattr(self.actual, name)
        def execute(self, statement, *args):
            change("execute", self.actual, statement)
            return self.actual.execute(statement, *args)
        def commit(self):
            self.actual.commit()
            change("commit", self.actual, None)
    def opened(*args):
        owned = original_open(*args)
        return replace(owned, connection=Connection(owned.connection))
    monkeypatch.setattr(module, "open_existing", opened)


def test_upgrade_commit_ack_unknown_reopens_and_verifies_actual_v3(tmp_path, monkeypatch, plan_guards):
    cfg, path = legacy(tmp_path)
    original_history = _record_history(path)
    def fail_ack(stage, connection, statement):
        if stage == "commit":
            raise sqlite3.OperationalError("真实COMMIT完成，回执丢失")
    _intercept_upgrade_connection(monkeypatch, fail_ack)
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.VERIFIED_EXISTING
    opened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert opened.metadata.format_version == 3
        assert opened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == original_history
        assert opened.connection.execute("SELECT id,request_id FROM plans").fetchall() == [(7, 107)]
    finally:
        opened.connection.close()


def test_upgrade_before_commit_rolls_back_and_reuses_original_backup(tmp_path, monkeypatch, plan_guards):
    cfg, path = legacy(tmp_path)
    original_history = _record_history(path)
    def fail_create(stage, connection, statement):
        if stage == "execute" and statement.startswith("CREATE TABLE software_recoveries"):
            raise sqlite3.OperationalError("独立表创建失败")
    _intercept_upgrade_connection(monkeypatch, fail_create)
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.FAILED
    backup = Path(str(path) + ".before-software-recovery-v3.sqlite")
    backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    opened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert opened.metadata.format_version == 2
        assert opened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == original_history
        assert opened.connection.execute("SELECT 1 FROM sqlite_master WHERE name='software_recoveries'").fetchone() is None
    finally:
        opened.connection.close()
    monkeypatch.undo()
    assert initialize_state(cfg, path, upgrade_software_recovery=True).outcome is InitOutcome.VERIFIED_EXISTING
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_hash
