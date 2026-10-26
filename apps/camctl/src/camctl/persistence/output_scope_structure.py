"""输出范围格式的操作流程约束只从权威 SQL 派生。"""

from contextlib import closing
import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import resource_bytes


def verify_output_scope_structure(connection) -> None:
    """拒绝保留旧操作种类或旧责任约束的 V6 状态库。"""
    authority = resource_bytes("sql/operations.sql").decode("utf-8")
    expected = re.search(r"CREATE TABLE operation_runs \(.*?\) STRICT", authority, re.S)
    if expected is None:
        raise ValueError("操作流程的权威 SQL 缺失")
    with closing(connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operation_runs'"
    )) as cursor:
        found = cursor.fetchone()
    if found is None or not isinstance(found[0], str):
        raise ValueError("操作流程的实际 CREATE TABLE 定义缺失")
    if _tokens(found[0]) != _tokens(expected.group()):
        raise ValueError("operation_runs 的实际列或结构约束与权威 V6 定义不一致")
