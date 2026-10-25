"""主机正式源的取回名称沿用登记元信息。"""

import sqlite3
import pytest
from types import SimpleNamespace
from unittest.mock import create_autospec

from camctl.outputs.obtain_flow import _host_source_candidate
from camctl.persistence.runtime import OwnedConnection, DatabaseMetadata
from support.business_database import inline_business_database


@pytest.mark.asyncio
async def test_photo_candidate_keeps_original_readable_name():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    # 保存路径提供稳定扩展名；原工具名称提供可读名称。
    connection.execute.return_value = cursor
    cursor.fetchone.return_value = ("capture-originals/11.3fr", "vendor-frame.3fr", 1)
    runtime = SimpleNamespace(database=inline_business_database(OwnedConnection(connection,
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))))
    candidate = await _host_source_candidate(runtime, {"id": 31}, 101, 701, 11,
                                       1_750_000_000_000_000, "计划", "拍摄")
    assert candidate.delivery_display_name == "vendor-frame-计划-拍摄.3fr"
    assert candidate.source_intermediate_file_id == 11
    assert candidate.config is None
