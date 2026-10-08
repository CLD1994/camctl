"""用权威 SQLite 结构校验完整投影行，不保存业务事实。

一次快照读取复用一个空内存结构。原始类型先核对，SQL 参数仅作
精确适配；每行在保存点内校验并回滚，调用者的原始映像保持不变。
独立对象的外键与同边界归属由历史读取者核对，不复制外部对象。
"""

from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from types import TracebackType
from typing import Any, Mapping

from camctl.contracts.json_values import is_json_integer
from camctl.history.events import SCHEMA_RESOURCES, load_event_registry
from camctl.persistence.transaction import encode_json_value, json_columns
from camctl.resources import resource_bytes


class ProjectionRowValidationError(ValueError):
    """完整投影行的类型、空值或列组合违反权威结构。"""


@dataclass(frozen=True)
class _UniqueIndex:
    name: str
    columns: tuple[str, ...]
    predicate: str
    cache_table: str


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@lru_cache(maxsize=1)
def _schema_scripts() -> tuple[str, ...]:
    return tuple(resource_bytes(name).decode("utf-8") for name in SCHEMA_RESOURCES)


class ProjectionRowValidator:
    """在一个快照读取范围内复用空结构和已查询的表元数据。"""

    def __init__(self) -> None:
        self._connection: sqlite3.Connection | None = None
        self._columns: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        self._indexes: dict[str, tuple[_UniqueIndex, ...]] = {}
        self._index_count = 0

    def __enter__(self) -> ProjectionRowValidator:
        if self._connection is not None:
            raise ProjectionRowValidationError("完整行校验器不能重复进入读取范围")
        connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            with closing(connection.execute("PRAGMA temp_store = MEMORY")):
                pass
            for script in _schema_scripts():
                with closing(connection.executescript(script)):
                    pass
        except BaseException as error:
            connection.close()
            if isinstance(error, sqlite3.Error):
                raise ProjectionRowValidationError("完整行校验的权威结构无法创建") from error
            raise
        self._connection = connection
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        connection, self._connection = self._connection, None
        self._columns.clear()
        self._indexes.clear()
        self._index_count = 0
        if connection is not None:
            connection.close()

    def validate(self, table: str, values: Mapping[str, Any]) -> None:
        """核对类型、SQL 约束与集合唯一键，仅保留必要索引键。"""
        connection = self._connection
        if connection is None:
            raise ProjectionRowValidationError("完整行校验要求已进入读取范围")
        if table not in load_event_registry()["tables"]:
            raise ProjectionRowValidationError(f"表 {table!r} 不是业务投影")
        columns = self._columns.get(table)
        if columns is None:
            with closing(connection.execute(f"PRAGMA table_xinfo({table})")) as cursor:
                metadata = cursor.fetchall()
            if not metadata or any(row[6] != 0 for row in metadata):
                raise ProjectionRowValidationError(f"表 {table} 的完整行结构无法解释")
            # 完整投影已具有物理主键，不使用 INSERT NULL 的自动编号。
            columns = tuple((row[1], row[2], bool(row[3] or row[5])) for row in metadata)
            self._columns[table] = columns
        if set(values) != {name for name, _, _ in columns}:
            raise ProjectionRowValidationError(f"表 {table} 的完整行列集合不符")
        json_fields = json_columns().get(table, frozenset())
        parameters = []
        for name, sql_type, required in columns:
            value = values[name]
            if value is None:
                if required:
                    raise ProjectionRowValidationError(f"{table}.{name} 不能为空")
                parameters.append(None)
            elif name in json_fields:
                parameters.append(encode_json_value(value))
            elif sql_type == "INTEGER":
                if not is_json_integer(value):
                    raise ProjectionRowValidationError(f"{table}.{name} 必须是精确整数")
                # Decimal 只用于 SQL 核验适配，返回映像中的原值保持。
                if not -(2**63) <= value <= 2**63 - 1:
                    raise ProjectionRowValidationError(f"{table}.{name} 超出 SQLite 整数范围")
                parameters.append(int(value))
            elif sql_type == "TEXT":
                if not isinstance(value, str):
                    raise ProjectionRowValidationError(f"{table}.{name} 必须是文本")
                parameters.append(value)
            else:
                raise ProjectionRowValidationError(f"{table}.{name} 的类型 {sql_type!r} 未定义")
        names = ", ".join(name for name, _, _ in columns)
        placeholders = ", ".join("?" for _ in columns)
        try:
            indexes = self._unique_indexes(table, columns)
        except sqlite3.Error as error:
            raise ProjectionRowValidationError(f"表 {table} 的唯一索引无法建立") from error
        keys = []
        with closing(connection.execute("SAVEPOINT validate_projection_row")):
            pass
        try:
            with closing(connection.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})", parameters)):
                pass
            for index in indexes:
                selected = ", ".join(_identifier(name) for name in index.columns)
                with closing(connection.execute(
                        f"SELECT {selected}, CASE WHEN {index.predicate} THEN 1 ELSE 0 END"
                        f" FROM {_identifier(table)}")) as cursor:
                    keys.append((index, cursor.fetchone()))
        except sqlite3.Error as error:
            raise ProjectionRowValidationError(
                f"{table}#{values.get('id')} 的完整行违反权威结构: {error}") from error
        finally:
            with closing(connection.execute("ROLLBACK TO validate_projection_row")):
                pass
            with closing(connection.execute("RELEASE validate_projection_row")):
                pass
        self._save_unique_keys(table, int(values["id"]), keys)

    def _unique_indexes(self, table, columns) -> tuple[_UniqueIndex, ...]:
        cached = self._indexes.get(table)
        if cached is not None:
            return cached
        connection = self._connection
        types = {name: sql_type for name, sql_type, _ in columns}
        with closing(connection.execute(f"PRAGMA index_list({_identifier(table)})")) as cursor:
            metadata = cursor.fetchall()
        indexes = []
        for entry in metadata:
            if not entry[2]:
                continue
            name = entry[1]
            with closing(connection.execute(f"PRAGMA index_xinfo({_identifier(name)})")) as cursor:
                fields = tuple(row for row in cursor.fetchall() if row[5])
            if not fields or any(row[1] < 0 or row[3] != 0 or row[4] != "BINARY" for row in fields):
                raise ProjectionRowValidationError(f"唯一索引 {name} 的表达式或排序规则无法解释")
            names = tuple(row[2] for row in fields)
            predicate = "1"
            if entry[4]:
                with closing(connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (name,))) as cursor:
                    definition = cursor.fetchone()[0]
                # 当前权威 DDL 使用普通列名和未引用的索引/表名。元数据
                # 已核对键列；只提取表达式，条件仍由 SQLite 精确执行。
                prefix = (rf"CREATE\s+UNIQUE\s+INDEX\s+{re.escape(name)}\s+ON\s+"
                          rf"{re.escape(table)}\s*\([^()]*\)\s+WHERE\s+(.+)")
                matched = re.fullmatch(prefix, definition, re.IGNORECASE | re.DOTALL)
                if matched is None:
                    raise ProjectionRowValidationError(f"部分唯一索引 {name} 的权威定义无法解释")
                predicate = matched[1].strip().removesuffix(";").strip()
            cache = f"projection_unique_{self._index_count}"
            self._index_count += 1
            key_fields = ", ".join(f"key_{position} {types[column]}"
                                   for position, column in enumerate(names))
            key_names = ", ".join(f"key_{position}" for position in range(len(names)))
            with closing(connection.execute(
                    f"CREATE TEMP TABLE {cache} (row_identity INTEGER PRIMARY KEY,"
                    f" {key_fields}, applies INTEGER NOT NULL) STRICT")):
                pass
            with closing(connection.execute(
                    f"CREATE UNIQUE INDEX {cache}_keys ON {cache} ({key_names}) WHERE applies = 1")):
                pass
            indexes.append(_UniqueIndex(name, names, predicate, cache))
        result = tuple(indexes)
        self._indexes[table] = result
        return result

    def _save_unique_keys(self, table, identity, keys) -> None:
        """仅键索引原子写入；非法行不能留下前面已检查索引的键。"""
        connection = self._connection
        with closing(connection.execute("SAVEPOINT validate_projection_keys")):
            pass
        succeeded = False
        try:
            for index, values in keys:
                names = ", ".join(f"key_{position}" for position in range(len(index.columns)))
                with closing(connection.execute(
                        f"SELECT {names}, applies FROM {index.cache_table} WHERE row_identity = ?",
                        (identity,))) as cursor:
                    previous = cursor.fetchone()
                if previous is not None:
                    if previous != values:
                        raise ProjectionRowValidationError(
                            f"{table}#{identity} 的唯一索引 {index.name} 在重复核验中改变")
                    continue
                parameters = (identity, *values)
                placeholders = ", ".join("?" for _ in parameters)
                with closing(connection.execute(
                        f"INSERT INTO {index.cache_table} VALUES ({placeholders})", parameters)):
                    pass
            succeeded = True
        except sqlite3.Error as error:
            raise ProjectionRowValidationError(
                f"{table}#{identity} 与对象内其他行的唯一键冲突: {error}") from error
        finally:
            if not succeeded:
                with closing(connection.execute("ROLLBACK TO validate_projection_keys")):
                    pass
            with closing(connection.execute("RELEASE validate_projection_keys")):
                pass
