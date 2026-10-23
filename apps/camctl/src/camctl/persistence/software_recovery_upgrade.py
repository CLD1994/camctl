"""显式 init --upgrade-software-recovery 的事务式 V3 格式升级。"""
import os
from pathlib import Path
import re
import sqlite3

from camctl.resources import resource_bytes
from camctl.persistence.runtime import StateDatabaseError


def upgrade_software_recovery(owned, state_db):
    """先保留 SQLite 一致备份，再原子创建新责任表与版本声明。"""
    if owned.metadata.format_version >= 3:
        if owned.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateDatabaseError("既有 V3 状态库外键核验失败")
        if owned.connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise StateDatabaseError("既有 V3 状态库完整性核验失败")
        return None
    # V1 的主机照片用途约束不能由本次升级隐式扩大。
    if owned.metadata.format_version != 2:
        raise StateDatabaseError("软件恢复升级要求先部署格式 V2；V1 保留原有全部业务资格")
    backup = Path(str(state_db) + ".before-software-recovery-v3.sqlite")
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        target = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if (target.execute("PRAGMA quick_check").fetchone() != ("ok",)
                    or target.execute("PRAGMA foreign_key_check").fetchone() is not None
                    or target.execute("SELECT * FROM database_metadata").fetchall()
                        != owned.connection.execute("SELECT * FROM database_metadata").fetchall()
                    or target.execute("SELECT MAX(id), COUNT(*) FROM history_events").fetchone()
                        != owned.connection.execute("SELECT MAX(id), COUNT(*) FROM history_events").fetchone()):
                raise StateDatabaseError("已有升级备份不等于本库原完整历史边界，保留备份并停止升级")
        finally:
            target.close()
    else:
        os.close(descriptor)
        target = sqlite3.connect(backup)
        try:
            owned.connection.backup(target)
        finally:
            target.close()
    descriptor = os.open(backup, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(backup.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        metadata = connection.execute("SELECT * FROM database_metadata WHERE id=1").fetchone()
        sql = resource_bytes("sql/reports.sql").decode()
        create = re.search(r"CREATE TABLE database_metadata \(.*?\) STRICT;", sql, re.S).group()
        connection.execute("ALTER TABLE database_metadata RENAME TO software_recovery_previous_metadata")
        connection.execute(create)
        connection.execute("INSERT INTO database_metadata VALUES (?,?,?,?,?,?,?)",
                           (*metadata[:3], 3, *metadata[4:]))
        connection.execute("DROP TABLE software_recovery_previous_metadata")
        sql = resource_bytes("sql/operations.sql").decode()
        create = re.search(r"CREATE TABLE software_recoveries \(.*?\) STRICT;", sql, re.S).group()
        connection.execute(create)
        connection.execute("CREATE INDEX recoveries_action ON software_recoveries(action_id,id)")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateDatabaseError("升级后的外键核验失败")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    return backup
