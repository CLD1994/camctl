"""通过 sqlite3 的连接和关闭接口观察测试数据库的会话连接。"""

import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from urllib.request import url2pathname


def _tracked_open_connections(monkeypatch, database: Path):
    opened = []
    original_connect = sqlite3.connect
    database_path = os.path.normcase(os.path.realpath(database))

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            self.unusable_after_close = False

        def close(self):
            result = super().close()
            self.close_calls += 1
            try:
                super().execute("SELECT 1")
            except sqlite3.ProgrammingError:
                self.unusable_after_close = True
            return result

    def connect(path, *args, **kwargs):
        uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
        if not uri or not isinstance(path, (str, bytes, os.PathLike)):
            return original_connect(path, *args, **kwargs)
        try:
            parts = urlsplit(os.fsdecode(path))
            matches = (parts.scheme == "file" and parts.netloc in ("", "localhost")
                       and parse_qs(parts.query).get("mode") == ["rw"]
                       and os.path.normcase(os.path.realpath(url2pathname(parts.path))) == database_path)
        except (OSError, TypeError, ValueError):
            return original_connect(path, *args, **kwargs)
        if not matches:
            return original_connect(path, *args, **kwargs)
        factory = kwargs.get("factory", args[4] if len(args) > 4 else sqlite3.Connection)
        if factory is sqlite3.Connection:
            if len(args) > 4:
                args = (*args[:4], TrackedConnection, *args[5:])
            else:
                kwargs["factory"] = TrackedConnection
        connection = original_connect(path, *args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened
