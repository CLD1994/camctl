"""核对按需照片来源所需的文件表结构。"""

from contextlib import closing
import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import resource_bytes


def verify_photo_source_structure(connection) -> None:
    """V7 不接受仍将未知来源长度约束为非空的旧表。"""
    authority = resource_bytes("sql/files.sql").decode("utf-8")
    for table in ("device_files", "file_copies"):
        expected = re.search(rf"CREATE TABLE {table} \(.*?\) STRICT", authority, re.S)
        if expected is None:
            raise ValueError(f"{table} 的权威 SQL 缺失")
        with closing(connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))) as cursor:
            found = cursor.fetchone()
        if found is None or not isinstance(found[0], str):
            raise ValueError(f"{table} 的实际 CREATE TABLE 定义缺失")
        if _tokens(found[0]) != _tokens(expected.group()):
            raise ValueError(f"{table} 的实际结构与按需照片来源格式不一致")
