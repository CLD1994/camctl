"""拍摄单元执行真实业务队列分类，不创建状态库或后台线程。"""
import sqlite3
from unittest.mock import create_autospec

from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


def capture_database(owned=None):
    if owned is None:
        owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
            DatabaseMetadata('camctl', 'a' * 32, 5, '/staging', '/ready', '/processing'))
    return inline_business_database(owned)
