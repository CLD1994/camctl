"""HBF 测试验证原写连接关闭及主库真实只读权限。"""

import sqlite3
import uuid


class TrackedConnection(sqlite3.Connection):
    """由实际连接所属线程执行 close，并留存结束事实。"""

    closed_by_owner = False

    def close(self):
        super().close()
        self.closed_by_owner = True


def tracked_connect_factory(original_connect):
    def connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackedConnection)
        return original_connect(*args, **kwargs)
    return connect


def main_database_readonly(connection):
    """供无活动事务的 HBF 核实入口使用，在回滚保存点内核对主库写权限。

    仅 READONLY 及其扩展码表示只读；不承诺保持已有读事务的锁状态。
    """
    name = "hbf_readonly_probe_" + uuid.uuid4().hex
    connection.execute("SAVEPOINT hbf_readonly_probe")
    try:
        try:
            connection.execute(f'CREATE TABLE main."{name}" (value INTEGER)')
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None or code & 0xff != sqlite3.SQLITE_READONLY:
                raise
            return True
        return False
    finally:
        connection.execute("ROLLBACK TO hbf_readonly_probe")
        connection.execute("RELEASE hbf_readonly_probe")
