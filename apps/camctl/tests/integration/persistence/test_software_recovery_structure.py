"""V3 新责任表必须实际具备权威结构，旧格式不要求新表。"""
import re
import sqlite3

import pytest

from camctl.resources import resource_bytes
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig, DatabaseInvalidError
from .test_runtime import _create_valid_database


def create_statement():
    return re.search(r"CREATE TABLE software_recoveries \(.*?\) STRICT;",
                     resource_bytes("sql/operations.sql").decode(), re.S).group()


def malformed(statement, defect):
    if defect == "missing_column":
        return statement.replace("    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,\n", "")
    if defect == "wrong_type":
        return statement.replace("activity_id INTEGER", "activity_id TEXT")
    if defect == "nullable":
        return statement.replace("activity_id INTEGER NOT NULL", "activity_id INTEGER")
    if defect == "no_unique":
        return statement.replace("activity_id INTEGER NOT NULL UNIQUE", "activity_id INTEGER NOT NULL")
    if defect == "no_action_fk":
        return statement.replace(" REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED", "")
    if defect == "no_activity_fk":
        return statement.replace(" REFERENCES device_activities(id) DEFERRABLE INITIALLY DEFERRED", "")
    if defect == "wrong_fk_parent":
        return statement.replace("REFERENCES device_activities(id)", "REFERENCES actions(id)")
    if defect == "wrong_fk_column":
        return statement.replace("REFERENCES device_activities(id)", "REFERENCES device_activities(action_id)")
    if defect == "no_json_check":
        return statement.replace(" CHECK (json_valid(state_json) AND json_type(state_json) = 'object')", "")
    if defect == "not_strict":
        return statement.replace(") STRICT;", ");")
    raise AssertionError(defect)


@pytest.mark.parametrize("entrypoint", ["open-rw", "open-ro", "init", "upgrade"])
@pytest.mark.parametrize("defect", ["missing_column", "wrong_type", "nullable", "no_unique",
    "no_action_fk", "no_activity_fk", "wrong_fk_parent", "wrong_fk_column", "no_json_check", "not_strict"])
def test_open_v3_rejects_missing_real_recovery_constraints(tmp_path, defect, entrypoint):
    path = tmp_path / "state.db"
    _create_valid_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE software_recoveries")
        connection.execute(malformed(create_statement(), defect))
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        # 空表的数据核验不证明 FK、UNIQUE、CHECK 定义存在。
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    if entrypoint.startswith("open"):
        with pytest.raises(DatabaseInvalidError):
            open_existing(path, DbOpenMode.EXISTING_RW if entrypoint == "open-rw" else DbOpenMode.EXISTING_RO, DbConfig())
    else:
        from .test_software_recovery_upgrade import config
        from camctl.persistence.initialization import initialize_state, InitOutcome
        result = initialize_state(config(tmp_path), path, upgrade_software_recovery=entrypoint == "upgrade")
        assert result.outcome is InitOutcome.FAILED
        assert "软件恢复格式结构无效" in result.detail


def test_authoritative_v3_structure_is_accepted_and_legacy_v2_needs_no_new_table(tmp_path):
    path = tmp_path / "state.db"
    _create_valid_database(path)
    opened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    opened.connection.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE database_metadata SET format_version=2")
        connection.execute("DROP TABLE software_recoveries")
    opened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    assert opened.metadata.format_version == 2
    opened.connection.close()
