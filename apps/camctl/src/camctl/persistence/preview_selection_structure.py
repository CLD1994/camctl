"""核对可分别保存预览缺席与未确认的取回表。"""

from contextlib import closing
import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import resource_bytes


def verify_preview_selection_structure(connection) -> None:
    authority = resource_bytes("sql/workflows.sql").decode("utf-8")
    expected = re.search(r"CREATE TABLE obtain_items \(.*?\) STRICT", authority, re.S)
    if expected is None:
        raise ValueError("obtain_items 的权威 SQL 缺失")
    with closing(connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='obtain_items'")) as cursor:
        found = cursor.fetchone()
    if found is None or not isinstance(found[0], str):
        raise ValueError("obtain_items 的实际 CREATE TABLE 定义缺失")
    if _tokens(found[0]) != _tokens(expected.group()):
        raise ValueError("obtain_items 的实际结构与预览选择格式不一致")
