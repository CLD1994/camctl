"""一次报告动作的交付关联与失败片段；仅在私有磁盘文件暂存。"""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.persistence.derived_storage import configure_derived_connection
from camctl.persistence.transaction import encode_json_value


class FailureDeliveryIndex:
    """先核对 H 的全部关联，再按依赖和交付整数 ID 分页返回失败。"""

    def __init__(self, *, batch_size=128):
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError("失败索引行批次必须为1至128")
        self.batch_size = batch_size
        self._temporary = TemporaryDirectory(prefix="camctl-report-failures-")
        self._db = None
        self._ready = False
        try:
            self._db = sqlite3.connect(
                str(Path(self._temporary.name) / "failures.db"), isolation_level=None)
            configure_derived_connection(self._db)
            self._db.execute(
                "CREATE TABLE refs (delivery INTEGER PRIMARY KEY, dependency INTEGER NOT NULL,"
                " output INTEGER NOT NULL, source INTEGER NOT NULL, seen INTEGER NOT NULL)"
                " WITHOUT ROWID").close()
            self._db.execute(
                "CREATE TABLE failures (dependency INTEGER NOT NULL, delivery INTEGER NOT NULL,"
                " body TEXT NOT NULL, PRIMARY KEY(dependency,delivery)) WITHOUT ROWID").close()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        db, self._db = self._db, None
        try:
            if db is not None:
                db.close()
        finally:
            self._temporary.cleanup()

    def _one(self, sql, parameters=()):
        with closing(self._db.execute(sql, parameters)) as cursor:
            return cursor.fetchone()

    def add_reference(self, identity, dependency, output, source):
        try:
            self._db.execute("INSERT INTO refs VALUES (?,?,?,?,0)",
                             (identity, dependency, output, source)).close()
        except sqlite3.IntegrityError as error:
            raise ConsistencyError("同 H 的交付被多个取回项引用") from error

    def bind_delivery(self, identity, output):
        reference = self._one(
            "SELECT dependency,output,source,seen FROM refs WHERE delivery=?", (identity,))
        if reference is None or reference[3] or reference[1] != output:
            raise ConsistencyError("同 H 的交付缺少唯一且一致的取回项")
        self._db.execute("UPDATE refs SET seen=1 WHERE delivery=?", (identity,)).close()
        return reference[0], reference[2]

    def add_failure(self, dependency, identity, value):
        self._db.execute("INSERT INTO failures VALUES (?,?,?)",
                         (dependency, identity, encode_json_value(value))).close()

    def finish(self):
        if self._one("SELECT delivery FROM refs WHERE seen=0 LIMIT 1") is not None:
            raise ConsistencyError("同 H 的取回项引用了缺失的交付")
        self._ready = True

    def failures(self, dependency):
        if not self._ready or self._db is None:
            raise ConsistencyError("交付失败索引尚未完整核验或已经关闭")
        previous = 0
        while True:
            with closing(self._db.execute(
                "SELECT delivery,body FROM failures WHERE dependency=? AND delivery>?"
                " ORDER BY delivery LIMIT ?", (dependency, previous, self.batch_size),
            )) as cursor:
                selected = cursor.fetchmany(self.batch_size)
            if not selected:
                return
            for identity, body in selected:
                yield identity, parse_exact_json(body)
            previous = selected[-1][0]
