"""只核对 V3 软件恢复表实际承诺的结构，签名从权威 SQL 派生。"""
from contextlib import closing
from functools import lru_cache
import re
import sqlite3

from camctl.resources import resource_bytes


_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*(?:\n|$)|/\*.*?\*/|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", re.S)


def _tokens(sql):
    # 引号分支先消费完整字面量；注释分支只可能从引号外开始匹配。
    return tuple(token if token.startswith(("'", '"')) else token.lower()
                 for token in _TOKENS.findall(sql)
                 if not token.startswith(("--", "/*")))


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
    # 完整 CREATE 词元共同约束每一个 FK 与 CHECK 的实际声明位置。
    # 不用全表关键字计数代替逐约束含义。
    return columns, foreign_keys, tuple(sorted(unique)), strict, _tokens(sql)



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
