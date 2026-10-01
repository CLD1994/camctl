"""R7 报告字节保存与发布的组件集成测试。

确定字节保存（BYTES）→ 可靠发布（PUBLISH）：字节哈希落库、发布
计数递增、同报告补投字节一致；无字节不能发布。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.policy import (
    ReportingRepository,
    publish_report,
    record_report_bytes,
)

from ..persistence.test_runtime import _create_valid_database


@pytest.fixture()
def connection(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    yield owned.connection
    owned.connection.close()


def _freeze_report(tmp_path: Path, connection) -> int:
    """经真实受理 + 冻结建立一份可发布报告。"""
    import asyncio

    from .test_freeze import _submit

    async def scenario() -> None:
        from camctl.persistence.runtime import OwnedConnection

        owned = OwnedConnection(connection=connection, metadata=None)
        await _submit(owned, tmp_path, "1")

    asyncio.run(scenario())
    from camctl.persistence.runtime import OwnedConnection

    owned = OwnedConnection(connection=connection, metadata=None)
    outcome = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=1
    )
    assert outcome.kind.value == "completed"
    return outcome.value.report.report_id


class TestPublishFlow:
    def test_bytes_then_publish(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        payload = b'{"report_id" : "1", "from_wm" : 0, "to_wm" : 0}\n'
        saved = record_report_bytes(new_operation_key(), _owned(connection), report_id, payload)
        assert saved.kind.value == "completed"
        assert saved.value["sha256"] == hashlib.sha256(payload).hexdigest()
        assert saved.value["size_bytes"] == len(payload)
        row = connection.execute(
            "SELECT status, size_bytes, sha256 FROM reports WHERE id = 1"
        ).fetchone()
        assert row[0] == 2  # PREPARED
        assert row[2] == hashlib.sha256(payload).hexdigest()

        published = publish_report(
            new_operation_key(), _owned(connection), report_id
        )
        assert published.kind.value == "completed"
        assert published.value["publication_count"] == 1
        row = connection.execute(
            "SELECT status, publication_count, last_published_event_id FROM reports WHERE id = 1"
        ).fetchone()
        assert row[0] == 4  # PUBLISHED
        assert row[1] == 1
        assert row[2] is not None

    def test_publish_without_bytes_rejected(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        outcome = publish_report(new_operation_key(), _owned(connection), report_id)
        assert outcome.kind.value == "rolled_back"
        assert "确定字节" in str(outcome.error)

    def test_republish_increments_count(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        payload = b'{"report_id" : "1", "from_wm" : 0, "to_wm" : 0}\n'
        record_report_bytes(new_operation_key(), _owned(connection), report_id, payload)
        publish_report(new_operation_key(), _owned(connection), report_id)
        second = publish_report(new_operation_key(), _owned(connection), report_id)
        assert second.value["publication_count"] == 2
        # 同报告补投：字节保持一致（已发布后不重新准备字节，只重走发布）。
        row = connection.execute(
            "SELECT size_bytes, sha256 FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        assert row == (len(payload), hashlib.sha256(payload).hexdigest())
        # 已发布状态再保存不同字节按状态模型拒绝。
        changed = record_report_bytes(
            new_operation_key(), _owned(connection), report_id, b"other\n"
        )
        assert changed.kind.value == "rolled_back"

    def test_changed_bytes_for_same_report_rejected(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        record_report_bytes(
            new_operation_key(), _owned(connection), report_id, b"first\n"
        )
        outcome = record_report_bytes(
            new_operation_key(), _owned(connection), report_id, b"different\n"
        )
        assert outcome.kind.value == "rolled_back"


def _owned(connection):
    from camctl.persistence.runtime import OwnedConnection

    return OwnedConnection(connection=connection, metadata=None)
