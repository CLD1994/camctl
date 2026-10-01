"""R7 报告字节保存与发布的组件集成测试。

确定字节保存（PREPARE）→ 独立发布意图（INTENT）→ 可靠发布（PUBLISH）：
字节依据落库、发布计数递增、同报告补投字节一致，失败及未知提交可核实。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import (
    CommandPlan, commit_operation, event_envelope, row_facts, update_change,
)
from camctl.history.validators import EventValidationError
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.reporting.models import ReportBytes
from camctl.reporting.policy import (
    ReportingRepository,
    publish_report,
    record_report_bytes,
    record_report_failure,
    record_report_publish_intent,
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
        saved = record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes(payload))
        assert saved.kind.value == "completed"
        assert saved.value.sha256 == hashlib.sha256(payload).hexdigest()
        assert saved.value.size_bytes == len(payload)
        row = connection.execute(
            "SELECT status, size_bytes, sha256 FROM reports WHERE id = 1"
        ).fetchone()
        assert row[0] == 2  # PREPARED
        assert row[2] == hashlib.sha256(payload).hexdigest()

        published = _publish(connection, report_id)
        assert published.kind.value == "completed"
        assert published.value.publication_count == 1
        row = connection.execute(
            "SELECT status, publication_count, last_published_event_id FROM reports WHERE id = 1"
        ).fetchone()
        assert row[0] == 4  # PUBLISHED
        assert row[1] == 1
        assert row[2] is not None

    def test_publish_without_bytes_rejected(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        outcome = publish_report(new_operation_key(), _owned(connection), report_id, _moved())
        assert outcome.kind.value == "rolled_back"
        assert outcome.error is not None

    def test_republish_increments_count(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        payload = b'{"report_id" : "1", "from_wm" : 0, "to_wm" : 0}\n'
        record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes(payload))
        _publish(connection, report_id)
        second = _publish(connection, report_id)
        assert second.value.publication_count == 2
        # 同报告补投：字节保持一致（已发布后不重新准备字节，只重走发布）。
        row = connection.execute(
            "SELECT size_bytes, sha256 FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        assert row == (len(payload), hashlib.sha256(payload).hexdigest())
        # 已发布状态再保存不同字节按状态模型拒绝。
        changed = record_report_bytes(
            new_operation_key(), _owned(connection), report_id, _bytes(b"other\n")
        )
        assert changed.kind.value == "rolled_back"

    def test_changed_bytes_for_same_report_rejected(self, connection, tmp_path: Path) -> None:
        report_id = _freeze_report(tmp_path, connection)
        record_report_bytes(
            new_operation_key(), _owned(connection), report_id, _bytes(b"first\n")
        )
        outcome = record_report_bytes(
            new_operation_key(), _owned(connection), report_id, _bytes(b"different\n")
        )
        assert outcome.kind.value == "rolled_back"


def _owned(connection):
    from camctl.persistence.runtime import OwnedConnection

    return OwnedConnection(connection=connection, metadata=None)


def _bytes(payload=b"first\n"):
    return ReportBytes(len(payload), hashlib.sha256(payload).hexdigest())


def _moved():
    return PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)


def _publish(connection, report_id):
    intent = record_report_publish_intent(new_operation_key(), _owned(connection), report_id)
    assert intent.kind.value == "completed", intent.error
    return publish_report(new_operation_key(), _owned(connection), report_id, _moved())


@dataclass
class ReportUpdate:
    """按正式事件规格提交更新，验证生产守卫而非普通入口的检查。"""

    report_id: int
    reason: int
    after: dict

    def plan(self, scope):
        facts = row_facts(scope.connection, "reports", self.report_id)
        if facts["last_error_json"] is not None:
            from camctl.contracts.json_values import parse_exact_json
            facts["last_error_json"] = parse_exact_json(facts["last_error_json"])
        before = {key: facts[key] for key in self.after}
        allocation = scope.allocate(1)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 28,
                               self.reason, (update_change("reports", self.report_id,
                                                          before, self.after),), 1)
        return CommandPlan(events=(event,), owners={("reports", self.report_id): ("report", self.report_id)},
                           state_rows={"reports": {self.report_id: facts}})


def _update(connection, report_id, reason, after):
    return commit_operation(ReportUpdate(report_id, reason, after), new_operation_key(), _owned(connection))


def _fail_report(connection, report_id):
    outcome = _update(connection, report_id, 5, {"status": 5, "last_error_json": {"stage": "file_sync", "reason": "disk error"}})
    assert outcome.kind == "completed", outcome.error


def _saved_state(connection):
    return (connection.execute("SELECT * FROM reports").fetchall(),
            connection.execute("SELECT * FROM history_transactions").fetchall(),
            connection.execute("SELECT * FROM history_events").fetchall())


def test_preparation_after_failure_uses_actual_error_before_value(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    _fail_report(connection, report_id)
    outcome = record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes())
    assert outcome.kind.value == "completed", outcome.error
    assert connection.execute("SELECT status, last_error_json FROM reports").fetchone() == (2, None)


def test_formal_prepare_cannot_replace_bytes_after_failure(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    assert record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes()).kind.value == "completed"
    _fail_report(connection, report_id)
    before = _saved_state(connection)
    receipt = _update(connection, report_id, 2, {"status": 2, "size_bytes": 10,
                      "sha256": "a" * 64, "last_error_json": None})
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert _saved_state(connection) == before


@pytest.mark.parametrize("bad_field", ["publication_count", "last_published_event_id"])
def test_formal_publish_rejects_invalid_success_fact(connection, tmp_path, bad_field):
    report_id = _freeze_report(tmp_path, connection)
    assert record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes()).kind.value == "completed"
    assert _update(connection, report_id, 3, {"status": 3, "last_error_json": None}).kind == "completed"
    next_event = connection.execute("SELECT MAX(id) + 1 FROM history_events").fetchone()[0]
    after = {"status": 4, "publication_count": 1, "last_published_event_id": next_event, "last_error_json": None}
    after[bad_field] = 2 if bad_field == "publication_count" else 1
    before = _saved_state(connection)
    receipt = _update(connection, report_id, 4, after)
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert _saved_state(connection) == before


def _stage(connection, report_id, stage):
    if stage != "registered":
        if stage != "failed_empty":
            assert record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes()).kind.value == "completed"
        if stage in ("publishing", "published", "failed_published"):
            assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"
        if stage in ("published", "failed_published"):
            assert publish_report(new_operation_key(), _owned(connection), report_id, _moved()).kind.value == "completed"
        if stage.startswith("failed"):
            assert record_report_failure(new_operation_key(), _owned(connection), report_id,
                                         {"stage": "move", "reason": "file error"}).kind.value == "completed"


@pytest.mark.parametrize("stage,want_status,adds_event", [
    ("registered", 2, True), ("prepared", 2, False), ("publishing", 3, False),
    ("published", 4, False), ("failed_empty", 2, True), ("failed_prepared", 2, True),
    ("failed_published", 2, True),
])
def test_same_bytes_preparation_preserves_phase_and_success_facts(connection, tmp_path, stage, want_status, adds_event):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, stage)
    before = _saved_state(connection)
    successful = connection.execute("SELECT publication_count, last_published_event_id FROM reports").fetchone()
    result = record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes())
    assert result.kind.value == "completed", result.error
    assert connection.execute("SELECT status, size_bytes, sha256 FROM reports").fetchone() == (want_status, 6, _bytes().sha256)
    assert connection.execute("SELECT publication_count, last_published_event_id FROM reports").fetchone() == successful
    assert len(_saved_state(connection)[2]) == len(before[2]) + int(adds_event)
    if not adds_event:
        assert _saved_state(connection) == before


@pytest.mark.parametrize("stage", ["prepared", "publishing", "published", "failed_prepared", "failed_published"])
def test_different_rebuilt_bytes_never_replace_fixed_bytes(connection, tmp_path, stage):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, stage)
    before = _saved_state(connection)
    result = record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes(b"other\n"))
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before


@pytest.mark.parametrize("stage,want", [
    ("registered", "rolled_back"), ("prepared", "completed"),
    ("publishing", "completed"), ("published", "completed"),
    ("failed_empty", "rolled_back"), ("failed_prepared", "completed"), ("failed_published", "completed"),
])
def test_publish_intent_uses_actual_bytes_and_retains_previous_success(connection, tmp_path, stage, want):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, stage)
    before = _saved_state(connection)
    successful = connection.execute("SELECT publication_count, last_published_event_id, last_error_json FROM reports").fetchone()
    result = record_report_publish_intent(new_operation_key(), _owned(connection), report_id)
    assert result.kind.value == want, result.error
    assert connection.execute("SELECT publication_count, last_published_event_id, last_error_json FROM reports").fetchone() == successful
    if want == "rolled_back" or stage == "publishing":
        assert _saved_state(connection) == before
    else:
        assert connection.execute("SELECT status FROM reports").fetchone() == (3,)
        assert len(_saved_state(connection)[2]) == len(before[2]) + 1


def test_publish_intent_and_success_are_separate_history_transactions(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes())
    intent_key = new_operation_key()
    assert record_report_publish_intent(intent_key, _owned(connection), report_id).kind.value == "completed"
    assert connection.execute("SELECT status, publication_count FROM reports").fetchone() == (3, 0)
    published = publish_report(new_operation_key(), _owned(connection), report_id, _moved())
    assert published.kind.value == "completed"
    txn = connection.execute("SELECT first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?", (str(intent_key),)).fetchone()
    assert txn[0] == txn[1] < published.value.published_event_id


@pytest.mark.parametrize("directory", [DirectorySyncStage.FAILED, DirectorySyncStage.NOT_ATTEMPTED])
def test_moved_file_without_completed_directory_sync_cannot_record_success(connection, tmp_path, directory):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "publishing")
    before = _saved_state(connection)
    result = publish_report(new_operation_key(), _owned(connection), report_id,
                            PublishResult(PublishStage.MOVED, directory, True, None))
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before


@pytest.mark.parametrize("stage", ["registered", "prepared", "published", "failed_empty", "failed_prepared", "failed_published"])
def test_success_requires_current_publish_intent(connection, tmp_path, stage):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, stage)
    before = _saved_state(connection)
    result = publish_report(new_operation_key(), _owned(connection), report_id, _moved())
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before


def test_failed_republication_keeps_previous_success_until_next_publish(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "published")
    original = connection.execute("SELECT publication_count, last_published_event_id FROM reports").fetchone()
    assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"
    error = {"stage": "directory_sync", "reason": "failed after move"}
    assert record_report_failure(new_operation_key(), _owned(connection), report_id, error).kind.value == "completed"
    assert connection.execute("SELECT status, publication_count, last_published_event_id FROM reports").fetchone() == (5, *original)
    published = _publish(connection, report_id)
    assert published.kind.value == "completed", published.error
    assert published.value.publication_count == 2
    assert connection.execute("SELECT status, last_error_json FROM reports").fetchone() == (4, None)


def test_real_handoff_result_can_be_saved_after_receiver_deletes_file(connection, tmp_path):
    """组合 F5 通用移动结果与报告仓储；报告专属引用和协调器仍另行实现。"""
    import asyncio
    import os
    from camctl.host_files.handoff import HandoffDirectories, ReadyName, publish_file
    from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef

    report_id = _freeze_report(tmp_path, connection)
    staging, ready = tmp_path / "staging", tmp_path / "ready"
    (staging / "deliveries").mkdir(parents=True)
    ready.mkdir()
    payload = b"first\n"
    source = staging / "deliveries" / "1.json"
    with source.open("wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    assert record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes(payload)).kind.value == "completed"
    assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"
    name = f"status-report-{report_id}-{_bytes(payload).sha256}.json"
    result = asyncio.run(publish_file(FileRef(1, FilePurpose.DELIVERY_COPY, "deliveries/1.json", staging),
                                     BoundDirectories(staging), HandoffDirectories(staging, ready), ReadyName(name)))
    assert result.stage is PublishStage.MOVED
    (ready / name).unlink()
    saved = publish_report(new_operation_key(), _owned(connection), report_id, result)
    assert saved.kind.value == "completed", saved.error
    assert saved.value.publication_count == 1


def _operation(connection, report_id, operation, key=None):
    key = new_operation_key() if key is None else key
    owned = _owned(connection)
    if operation == "prepare":
        return record_report_bytes(key, owned, report_id, _bytes())
    if operation == "intent":
        return record_report_publish_intent(key, owned, report_id)
    if operation == "publish":
        return publish_report(key, owned, report_id, _moved())
    if operation == "failure":
        return record_report_failure(key, owned, report_id, {"reason": "file failure"})
    raise AssertionError(operation)


def _before_operation(connection, report_id, operation):
    if operation in ("intent", "publish"):
        assert record_report_bytes(new_operation_key(), _owned(connection), report_id, _bytes()).kind.value == "completed"
    if operation == "publish":
        assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"


@pytest.mark.parametrize("operation", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("prefix", ["SELECT * FROM reports", "SELECT event.id, event.event_type", "INSERT INTO history_events", "UPDATE reports"])
def test_management_read_or_write_failure_preserves_all_facts(connection, tmp_path, operation, prefix):
    from ..persistence.test_transactions import FailingConnection
    report_id = _freeze_report(tmp_path, connection)
    _before_operation(connection, report_id, operation)
    before = _saved_state(connection)
    outcome = _operation(FailingConnection(connection, prefix), report_id, operation)
    assert outcome.kind.value == "rolled_back"
    assert isinstance(outcome.error, sqlite3.OperationalError)
    assert _saved_state(connection) == before


@pytest.mark.parametrize("operation", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("commit_reached_database", [False, True])
def test_unknown_management_commit_reuses_original_key_without_duplicate_fact(connection, tmp_path, operation, commit_reached_database):
    report_id = _freeze_report(tmp_path, connection)
    _before_operation(connection, report_id, operation)
    before = _saved_state(connection)
    key = new_operation_key()

    class LostCommitResponse:
        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    connection.execute(statement, parameters)
                raise sqlite3.OperationalError("未取得提交响应")
            return connection.execute(statement, parameters)

    result = _operation(LostCommitResponse(), report_id, operation, key)
    assert result.kind.value == "unknown"
    assert result.value is None
    if connection.in_transaction:
        connection.rollback()
    with sqlite3.connect(tmp_path / "state.db") as check:
        assert (check.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),)).fetchone()
                is not None) is commit_reached_database
    retried = _operation(connection, report_id, operation, key)
    assert retried.kind.value == "completed", retried.error
    after = _saved_state(connection)
    assert len(after[2]) == len(before[2]) + 1
    assert _operation(connection, report_id, operation, key).kind.value == "completed"
    assert _saved_state(connection) == after


@pytest.mark.parametrize("operation", ["prepare", "intent", "publish", "failure"])
def test_invalid_fixed_report_basis_prevents_management_updates(connection, tmp_path, operation):
    report_id = _freeze_report(tmp_path, connection)
    _before_operation(connection, report_id, operation)
    connection.execute("UPDATE reports SET to_wm = 3")
    before = _saved_state(connection)
    outcome = _operation(connection, report_id, operation)
    assert outcome.kind.value == "rolled_back"
    assert _saved_state(connection) == before


@pytest.mark.parametrize("changed_input", ["bytes", "branch", "target"])
def test_operation_key_cannot_be_rebound(connection, tmp_path, changed_input):
    report_id = _freeze_report(tmp_path, connection)
    key = new_operation_key()
    assert record_report_bytes(key, _owned(connection), report_id, _bytes()).kind.value == "completed"
    if changed_input == "target":
        from ..acceptance.test_atomicity import _process
        from .test_freeze import _plan_body
        assert _process(_plan_body("2"), connection).kind.value == "completed"
        report_id = ReportingRepository().freeze_report(new_operation_key(), _owned(connection)).value.report.report_id
        assert report_id == 2
    before = _saved_state(connection)
    if changed_input == "branch":
        result = record_report_failure(key, _owned(connection), report_id, {"reason": "failed"})
    else:
        result = record_report_bytes(key, _owned(connection), report_id,
                                     _bytes(b"other\n") if changed_input == "bytes" else _bytes())
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before


def test_old_publish_key_returns_original_success_after_later_republication(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "publishing")
    original_key = new_operation_key()
    first = publish_report(original_key, _owned(connection), report_id, _moved())
    assert first.kind.value == "completed"
    second = _publish(connection, report_id)
    assert second.value.publication_count == 2
    before = _saved_state(connection)
    reused = publish_report(original_key, _owned(connection), report_id, _moved())
    assert reused.kind.value == "completed"
    assert reused.value == first.value
    assert _saved_state(connection) == before


def test_management_history_replays_success_failure_and_recovery(connection, tmp_path):
    from copy import deepcopy
    from camctl.contracts.enums import load_registry
    from camctl.contracts.history_values import HistoryBoundary, TransactionRange, ReadOrder, ReadScope
    from camctl.history.replay import EntityImage, apply_forward, apply_reverse
    from camctl.history.validators import EventContext, validate_event
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.contracts.json_values import parse_exact_json

    report_id = _freeze_report(tmp_path, connection)
    initial = row_facts(connection, "reports", report_id)
    assert _operation(connection, report_id, "prepare").kind.value == "completed"
    first = _publish(connection, report_id)
    assert first.kind.value == "completed"
    assert _operation(connection, report_id, "failure").kind.value == "completed"
    second = _publish(connection, report_id)
    assert second.value.publication_count == 2
    txn, last = connection.execute("SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1").fetchone()
    page = HistoryRepository(tmp_path / "state.db").read_events(
        ReadScope(ReadOrder.ASCENDING, None, initial["last_event_id"] + 1, last, 16, lambda position: position),
        HistoryBoundary(txn, last),
    )
    assert [event.reason for event in page.items] == [2, 3, 4, 5, 3, 4]
    entity_type = load_registry()["history_objects"]["report"]["id"]
    # 镜像只放业务列；派生历史列由查询投影独立维护。
    business = {k: v for k, v in initial.items() if k not in ("id", "created_event_id", "last_event_id")}
    image = EntityImage(entity_type, report_id, True, {("reports", report_id): business}, initial["last_event_id"], 0)
    original = deepcopy(image)
    states = {"reports": {report_id: deepcopy(business)}}
    validated = []
    for event in page.items:
        value = validate_event(event, EventContext(TransactionRange(event.transaction_id, event.event_id, event.event_id),
                                                   {("reports", report_id): ("report", report_id)}, states))
        validated.append(value)
        image = apply_forward(image, value)
        states["reports"][report_id].update(event.rows[0].after.values)
    current = row_facts(connection, "reports", report_id)
    if current["last_error_json"] is not None:
        current["last_error_json"] = parse_exact_json(current["last_error_json"])
    assert image.rows[("reports", report_id)] == {k: current[k] for k in business}
    for event in reversed(validated):
        image = apply_reverse(image, event)
    assert image.rows == original.rows
    assert connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone() == (2,)


@pytest.mark.parametrize("operation", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("mutation", ["reference", "count"])
def test_management_reader_requires_real_previous_publication(connection, tmp_path, operation, mutation):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "published")
    if operation == "publish":
        assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"
    if mutation == "reference":
        connection.execute("UPDATE reports SET last_published_event_id = created_event_id")
    else:
        connection.execute("UPDATE reports SET publication_count = 2")
    before = _saved_state(connection)
    result = _operation(connection, report_id, operation)
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before


@pytest.mark.parametrize("old,new", [(True, 1), (False, 0), (1, True), (0, False)])
@pytest.mark.parametrize("same_key", [False, True])
def test_failure_json_type_changes_are_distinct_inputs(connection, tmp_path, old, new, same_key):
    from camctl.contracts.json_values import parse_exact_json
    report_id = _freeze_report(tmp_path, connection)
    first_key = new_operation_key()
    old_error = {"reason": "file error", "context": [{"value": old}]}
    new_error = {"reason": "file error", "context": [{"value": new}]}
    assert record_report_failure(first_key, _owned(connection), report_id, old_error).kind.value == "completed"
    before = _saved_state(connection)
    key = first_key if same_key else new_operation_key()
    result = record_report_failure(key, _owned(connection), report_id, new_error)
    if same_key:
        assert result.kind.value == "rolled_back"
        assert _saved_state(connection) == before
    else:
        assert result.kind.value == "completed", result.error
        assert len(_saved_state(connection)[2]) == len(before[2]) + 1
        actual = parse_exact_json(connection.execute("SELECT last_error_json FROM reports").fetchone()[0])
        assert type(actual["context"][0]["value"]) is type(new)
        assert actual["context"][0]["value"] == new


def test_equal_report_error_with_new_key_does_not_create_a_new_fact(connection, tmp_path):
    from decimal import Decimal

    report_id = _freeze_report(tmp_path, connection)
    error = {"reason": "file error", "context": {"count": 1, "ready": True}}
    first = record_report_failure(new_operation_key(), _owned(connection), report_id, error)
    assert first.kind.value == "completed"
    before = _saved_state(connection)
    same_error = {"context": {"ready": True, "count": Decimal("1.0")}, "reason": "file error"}
    second = record_report_failure(new_operation_key(), _owned(connection), report_id, same_error)
    assert second.kind.value == "completed", second.error
    assert _saved_state(connection) == before


def test_non_persistable_formal_error_rolls_back_transaction(connection, tmp_path):
    report_id = _freeze_report(tmp_path, connection)
    before = _saved_state(connection)
    error = {}
    error["context"] = error
    receipt = _update(connection, report_id, 5, {"status": 5, "last_error_json": error})
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert connection.in_transaction is False
    assert _saved_state(connection) == before


def test_formal_intent_comparison_error_rolls_back_transaction(connection, tmp_path, monkeypatch):
    from unittest.mock import create_autospec
    from camctl.reporting import policy

    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "failed_prepared")
    before = _saved_state(connection)
    comparator = create_autospec(policy.json_equal, side_effect=RecursionError("comparison depth"))
    monkeypatch.setattr(policy, "json_equal", comparator)
    receipt = _update(connection, report_id, 3, {
        "status": 3, "last_error_json": {"stage": "move", "reason": "file error"},
    })
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert isinstance(receipt.error.__cause__, RecursionError)
    assert connection.in_transaction is False
    assert _saved_state(connection) == before


@pytest.mark.parametrize("operation", ["prepare", "intent", "publish", "failure"])
@pytest.mark.parametrize("mutation", ["older", "zero"])
def test_reader_cannot_accept_outdated_previous_publication_pair(connection, tmp_path, operation, mutation):
    report_id = _freeze_report(tmp_path, connection)
    _stage(connection, report_id, "published")
    original = connection.execute("SELECT publication_count, last_published_event_id FROM reports").fetchone()
    assert _publish(connection, report_id).value.publication_count == 2
    assert record_report_publish_intent(new_operation_key(), _owned(connection), report_id).kind.value == "completed"
    pair = original if mutation == "older" else (0, None)
    connection.execute("UPDATE reports SET publication_count = ?, last_published_event_id = ?", pair)
    before = _saved_state(connection)
    result = _operation(connection, report_id, operation)
    assert result.kind.value == "rolled_back"
    assert _saved_state(connection) == before
