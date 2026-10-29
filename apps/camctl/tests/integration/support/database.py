"""从权威存储格式准备状态库，并使用真实业务数据库线程。"""

from contextlib import asynccontextmanager, closing
from pathlib import Path
import sqlite3
import uuid

from camctl.persistence.business import BusinessDatabase
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION, DbConfig, DbOpenMode, OwnedConnection,
    canonical_directory_binding, open_existing,
)
from camctl.resources import resource_bytes


SCHEMA_RESOURCES = [
    "sql/core.sql", "sql/workflows.sql", "sql/files.sql",
    "sql/operations.sql", "sql/reports.sql", "sql/history.sql",
]
DIRECTORIES = tuple(canonical_directory_binding(Path(value)) for value in (
    "/srv/camctl/staging", "/srv/camctl/ready", "/srv/camctl/processing",
))


def create_test_database(path: Path, *, format_version=CURRENT_FORMAT_VERSION,
                         directories=DIRECTORIES) -> None:
    """按权威 SQL 建立夹具；仅准备连接使用 OFF，业务连接仍采用正式配置。"""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA journal_mode=wal")
        connection.execute("BEGIN IMMEDIATE")
        for name in SCHEMA_RESOURCES:
            connection.executescript(resource_bytes(name).decode("utf-8"))
        connection.execute(
            "INSERT INTO database_metadata (id, application_id, instance_id, format_version,"
            " staging_path, ready_path, processing_path) VALUES (1, 'camctl', ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, format_version, *directories),
        )
        connection.execute("INSERT INTO runtime_state (id, acknowledged_wm) VALUES (1, 0)")
        connection.commit()


def database_path(owned: OwnedConnection) -> Path:
    """取得准备连接所属文件；尚未提交的准备不得交给另一条业务连接。"""
    if owned.connection.in_transaction:
        raise ValueError("须先提交测试准备，再打开业务数据库")
    with closing(owned.connection.execute("PRAGMA database_list")) as cursor:
        rows = cursor.fetchall()
    path = next((row[2] for row in rows if row[1] == "main"), None)
    if not path:
        raise ValueError("真实业务线程测试须使用独立磁盘状态库")
    return Path(path)


@asynccontextmanager
async def business_database(path: Path, *, config=None, **executor_options):
    """连接在实际 worker 中创建；正常、错误及取消出口都关闭原执行器。"""
    options = DbConfig() if config is None else config
    database = await BusinessDatabase.open(
        lambda: open_existing(Path(path), DbOpenMode.EXISTING_RW, options),
        **executor_options,
    )
    try:
        yield database
    finally:
        await database.close()
