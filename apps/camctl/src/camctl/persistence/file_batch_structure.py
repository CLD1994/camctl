"""按权威 SQL 核对文件批次表及同机唯一占用索引。"""

import re

from camctl.persistence.recovery_structure import _tokens
from camctl.resources import resource_bytes


def verify_file_batch_structure(connection):
    authority = resource_bytes("sql/files.sql").decode("utf-8")
    for kind, name, pattern in (
        ("table", "device_file_batches", r"CREATE TABLE device_file_batches \(.*?\) STRICT"),
        ("index", "one_device_file_batch", r"CREATE UNIQUE INDEX one_device_file_batch\b[^;]+"),
    ):
        expected = re.search(pattern, authority, re.S)
        found = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
        if expected is None or found is None or _tokens(found[0]) != _tokens(expected.group()):
            raise ValueError(f"同机文件批次结构与权威定义不一致: {name}")
