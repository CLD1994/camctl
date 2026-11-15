"""从权威 SQL 核对 V10 新增的持久化结构。"""

import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import resource_bytes


def verify_format_v10_structure(connection) -> None:
    for resource, kind, name, pattern in (
        ("sql/history.sql", "table", "acceptance_receipts",
         r"CREATE TABLE acceptance_receipts \(.*?\) STRICT"),
        ("sql/operations.sql", "table", "baseline_read_calls",
         r"CREATE TABLE baseline_read_calls \(.*?\) STRICT"),
        ("sql/history.sql", "index", "report_changes_by_sequence",
         r"CREATE INDEX report_changes_by_sequence\b[^;]+"),
        ("sql/history.sql", "index", "report_changes_by_entity",
         r"CREATE INDEX report_changes_by_entity\b[^;]+"),
    ):
        authority = resource_bytes(resource).decode("utf-8")
        expected = re.search(pattern, authority, re.S)
        found = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
        ).fetchone()
        if expected is None or found is None or _tokens(found[0]) != _tokens(expected.group()):
            raise ValueError(f"结构与权威定义不一致: {name}")
