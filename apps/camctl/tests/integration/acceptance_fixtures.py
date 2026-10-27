"""为已有状态库打开正式业务数据库端口并受理输入。"""

from pathlib import Path

from camctl.acceptance.service import accept_input
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing


async def accept_with_database(source, context, key, owned):
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    database = await BusinessDatabase.open(
        lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()))
    try:
        return await accept_input(source, context, key, database)
    finally:
        await database.close()
