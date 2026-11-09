"""观察目标数据库只读连接向 Python 交付的行。"""


def observe_readonly_rows(monkeypatch, database_path, observe):
    import os
    import sqlite3
    from urllib.parse import parse_qs, urlsplit
    from urllib.request import url2pathname

    opened = []
    real_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            original_factory = self.row_factory

            def record(cursor, row):
                observe(cursor, row)
                return row if original_factory is None else original_factory(cursor, row)

            self.row_factory = record

    def connect(database, *args, **kwargs):
        if kwargs.get("uri") and str(database).startswith("file:"):
            parts = urlsplit(str(database))
            file_path = url2pathname(("//" + parts.netloc if parts.netloc else "") + parts.path)
            if (parse_qs(parts.query).get("mode") == ["ro"] and os.path.exists(file_path)
                    and os.path.samefile(file_path, database_path)):
                assert "factory" not in kwargs
                kwargs["factory"] = ObservedConnection
                connection = real_connect(database, *args, **kwargs)
                opened.append(connection)
                return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened
