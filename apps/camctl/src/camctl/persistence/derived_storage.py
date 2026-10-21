"""仅配置可重建、失败即删除的私有SQLite派生文件。"""
import sqlite3


def configure_derived_connection(connection):
    """保留磁盘语句回滚及有限页缓存，不承担业务持久化责任。"""
    if connection.in_transaction:
        raise sqlite3.OperationalError('派生连接须在事务外配置')
    cursor=connection.execute('PRAGMA journal_mode=PERSIST')
    try:
        if cursor.fetchone() != ('persist',):
            raise sqlite3.OperationalError('派生连接无法启用磁盘回滚日志')
    finally:
        cursor.close()
    for setting in ('synchronous=OFF','cache_size=-2048','temp_store=FILE',
                    'temp.cache_size=-2048'):
        connection.execute('PRAGMA '+setting).close()
