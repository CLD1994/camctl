"""只核对 V3 软件恢复表实际承诺的结构，签名从权威 SQL 派生。"""
from contextlib import closing
from functools import lru_cache
import re
import sqlite3

from camctl.resources import resource_bytes


_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", re.S)


def _tokens(sql):
    return tuple(token if token.startswith("'") else token.strip('"').lower()
                 for token in _TOKENS.findall(sql))


def _checks(sql):
    tokens = _tokens(sql)
    checks = set()
    for start, token in enumerate(tokens):
        if token != "check" or start + 1 >= len(tokens) or tokens[start + 1] != "(":
            continue
        depth = 1
        end = start + 2
        while end < len(tokens) and depth:
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            end += 1
        if depth:
            raise ValueError("恢复表 CHECK 定义括号未闭合")
        checks.add(tokens[start + 2:end - 1])
    return frozenset(checks)


def _signature(connection):
    with closing(connection.execute("PRAGMA table_xinfo(software_recoveries)")) as cursor:
        columns = tuple(tuple(row[1:]) for row in cursor.fetchall())
    with closing(connection.execute("PRAGMA foreign_key_list(software_recoveries)")) as cursor:
        foreign_keys = tuple(sorted(tuple(row[1:]) for row in cursor.fetchall()))
    with closing(connection.execute("PRAGMA index_list(software_recoveries)")) as cursor:
        indexes = cursor.fetchall()
    unique = []
    for index in indexes:
        if index[2] != 1 or index[4] != 0:
            continue
        # 名称来自 SQLite，不来自配置或外部输入；以参数形 PRAGMA 函数读取。
        with closing(connection.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],))) as cursor:
            unique.append(tuple(row[0] for row in cursor.fetchall()))
    with closing(connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='software_recoveries'")) as cursor:
        found = cursor.fetchone()
    if found is None or not isinstance(found[0], str):
        raise ValueError("恢复表实际 CREATE TABLE 定义缺失")
    sql = found[0]
    with closing(connection.execute("SELECT strict FROM pragma_table_list WHERE schema='main' AND name='software_recoveries'")) as cursor:
        strict = cursor.fetchone()
    tokens = _tokens(sql)
    deferred = sum(tokens[index:index + 3] == ("deferrable", "initially", "deferred") for index in range(len(tokens)))
    not_deferred = sum(tokens[index:index + 2] == ("not", "deferrable") for index in range(len(tokens)))
    return columns, foreign_keys, tuple(sorted(unique)), _checks(sql), strict, deferred, not_deferred


@lru_cache(maxsize=1)
def _expected():
    sql = resource_bytes("sql/operations.sql").decode("utf-8")
    create = re.search(r"CREATE TABLE software_recoveries \(.*?\) STRICT;", sql, re.S)
    if create is None:
        raise ValueError("软件恢复表权威 SQL 缺失")
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.execute(create.group())
        return _signature(reference)


def verify_recovery_structure(connection):
    """校验列、类型、空值、主键、唯一性、FK、CHECK 与 STRICT 定义。"""
    if _signature(connection) != _expected():
        raise ValueError("software_recoveries 的实际列或结构约束与权威 V3 定义不一致")
