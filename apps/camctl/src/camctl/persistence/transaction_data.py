"""状态库 SQL 行及精确 JSON 值的共享读取。"""

from contextlib import closing
from functools import lru_cache
import re
import sqlite3
from typing import Any

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.history.events import SCHEMA_RESOURCES
from camctl.persistence.atomic import TransactionError
from camctl.persistence.row_cache import read_cached_row
from camctl.resources import resource_bytes


@lru_cache(maxsize=1)
def json_columns() -> dict[str, frozenset[str]]:
    """从权威 SQL 收集按结构化值保存的 JSON 列。"""
    collected: dict[str, set[str]] = {}
    for name in SCHEMA_RESOURCES:
        sql = resource_bytes(name).decode("utf-8")
        for block in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\)\s*STRICT", sql, re.DOTALL):
            table = block.group(1)
            body = block.group(2)
            for match in re.finditer(r"json_valid\(\s*(\w+)\s*\)", body):
                collected.setdefault(table, set()).add(match.group(1))
    return {table: frozenset(names) for table, names in collected.items()}


def encode_json_value(value: Any) -> str:
    """事件正文使用公共精确编码器，并保留原事务错误分类。"""
    from camctl.contracts.json_encoding import encode_exact_json
    return encode_exact_json(value, error_type=TransactionError)


def _read_sql_value(table: str, column: str, value: Any) -> Any:
    if value is not None and column in json_columns().get(table, frozenset()):
        try:
            return parse_exact_json(value)
        except ValueError as error:
            raise ConsistencyError(f"{table}.{column} 不是有效精确 JSON") from error
    return value


def row_facts(connection: sqlite3.Connection, table: str, row_id: int) -> dict | None:
    """读取完整行事实，按权威 SQL 的 JSON 列分类恢复精确结构化值。"""
    return read_cached_row(connection, table, row_id,
                           lambda: _load_row_facts(connection, table, row_id))


def _load_row_facts(connection: sqlite3.Connection, table: str, row_id: int) -> dict | None:
    with closing(connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))) as cursor:
        found = cursor.fetchone()
        if found is None:
            return None
        description = cursor.description
    return {
        column[0]: _read_sql_value(table, column[0], value)
        for column, value in zip(description, found)
    }


def next_row_id(connection: sqlite3.Connection, table: str) -> int:
    """按已提交最大 ID 分配下一行编号。"""
    with closing(connection.execute(f"SELECT MAX(id) FROM {table}")) as cursor:
        row = cursor.fetchone()
    return (int(row[0]) if row[0] is not None else 0) + 1
