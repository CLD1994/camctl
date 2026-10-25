"""真实受理、冻结和报告生成共同保留状态时间及冻结字节。"""
import json

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import ReportingRepository
from .test_generation import environment
from .test_freeze import Catalog


def admit(owned, request="1", occurred_at=2_900_000):
    body = {"request_id": request, "created_at": "2026-01-15 08:00:00", "name": "时间",
            "actions": [{"name": "错误动作", "type": "report_status", "scheduled_at": "bad",
                         "params": {"scope": "full"}}]}
    result = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("input.json", body), Catalog(), CommandMode.RUN, occurred_at),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error


def freeze(owned):
    result = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=3_000_000)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value.report


def generate(path, report, **overrides):
    values = dict(db_path=path / "state.db", report_id=report.report_id,
                  from_wm=report.from_wm, to_wm=report.to_wm,
                  frozen_event_id=report.boundary.last_event_id,
                  staging_path=path / "staging" / "generated.json")
    values.update(overrides)
    return generate_report_file(GenerationSpec(**values)).path.read_bytes()


@pytest.mark.asyncio
async def test_new_report_has_complete_terminal_creation_times(environment):
    owned, path = environment
    admit(owned)
    report = freeze(owned)
    payload = generate(path, report)
    plan = json.loads(payload)["plans"][0]
    assert plan["status_history"] == [{"status": "completed", "changed_at": "1970-01-01 00:00:02", "clock_status": "unchecked"}]
    assert plan["actions"][0]["status_history"] == [{"status": "failed", "changed_at": "1970-01-01 00:00:02", "clock_status": "unchecked"}]
    admit(owned, "2", occurred_at=9_000_000)
    assert generate(path, report, event_batch_size=1, entity_batch_size=1) == payload


@pytest.mark.asyncio
async def test_full_history_survives_incremental_window_and_local_completion(environment):
    from camctl.reporting import policy
    from camctl.reporting.models import ReportBytes, SyncMode
    from camctl.reporting.publication import DbPublicationSession
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    from .test_freeze import _plan_body
    owned, path = environment
    def completed(outcome):
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    completed(AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("input.json", _plan_body("1")), Catalog(), CommandMode.RUN, 2_900_000),
        new_operation_key(), owned))
    completed(policy.start_sync(new_operation_key(), owned, action_id=1, mode=SyncMode.FULL, occurred_at=1_100_000))
    first = freeze(owned)
    old_payload = generate(path, first)
    completed(policy.record_report_bytes(new_operation_key(), owned, first.report_id, ReportBytes(len(old_payload), __import__("hashlib").sha256(old_payload).hexdigest()), occurred_at=4_000_000))
    completed(policy.record_report_publish_intent(new_operation_key(), owned, first.report_id, occurred_at=5_000_000))
    completed(policy.publish_report(new_operation_key(), owned, first.report_id,
        PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None), occurred_at=6_000_000))
    completed(AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("ack.json", {"request_id":"1", "last_report_id":str(first.report_id)}), Catalog(), CommandMode.RUN, 7_000_000), new_operation_key(), owned))
    database = await BusinessDatabase.open(lambda: open_existing(
        path / "state.db", DbOpenMode.EXISTING_RW, DbConfig()))
    try:
        completed(await DbPublicationSession(
            database, occurred_at=lambda: 9_500_000).settle_local(1, first.report_id))
    finally:
        await database.close()
    final = freeze(owned)
    assert final.from_wm == first.to_wm and final.from_wm > 0
    payload = generate(path, final, event_batch_size=1)
    plan = json.loads(payload)["plans"][0]
    expected_end = "1970-01-01 00:00:09"
    assert [item["status"] for item in plan["status_history"]] == ["pending", "running", "completed"]
    assert [item["status"] for item in plan["actions"][0]["status_history"]] == ["pending", "running", "succeeded"]
    for timeline in (plan["status_history"], plan["actions"][0]["status_history"]):
        assert [item["changed_at"] for item in timeline] == ["1970-01-01 00:00:02", "1970-01-01 00:00:01", expected_end]
    assert generate(path, first) == old_payload


@pytest.mark.asyncio
async def test_actual_epoch_creation_keeps_zero_time(environment):
    owned, path = environment
    admit(owned, occurred_at=0)
    report = freeze(owned)
    plan = json.loads(generate(path, report))["plans"][0]
    assert plan["status_history"][0]["changed_at"] == "1970-01-01 00:00:00"
    assert plan["actions"][0]["status_history"][0]["changed_at"] == "1970-01-01 00:00:00"


@pytest.mark.asyncio
async def test_reader_port_keeps_historical_clock_judgment(environment):
    from camctl.persistence.repositories.history import HistoryRepository
    owned, path = environment
    admit(owned, occurred_at=-1)
    # 受控历史夹具保留未信任时钟判断；不修改任何状态、身份或事件顺序。
    owned.connection.execute("UPDATE history_events SET clock_status=3")
    report = freeze(owned)
    actual = HistoryRepository(path / "state.db")
    class Reader:
        frozen_registration = actual.frozen_registration
        select_report_scope = actual.select_report_scope
        restore_entity = actual.restore_entity
        related_entity_ids = actual.related_entity_ids
        status_history = actual.status_history
    reader = Reader()
    spec = GenerationSpec(path / "state.db", report.report_id, report.from_wm, report.to_wm,
        report.boundary.last_event_id, path / "port.json")
    generated = generate_report_file(spec, repository=reader)
    plan = json.loads(generated.path.read_bytes())["plans"][0]
    assert plan["status_history"] == [{"status":"completed", "changed_at":"1969-12-31 23:59:59", "clock_status":"invalid"}]
    assert plan["actions"][0]["status_history"] == [{"status":"failed", "changed_at":"1969-12-31 23:59:59", "clock_status":"invalid"}]


@pytest.mark.asyncio
async def test_status_query_uses_sparse_index_without_event_body_reads(environment, monkeypatch):
    from camctl.persistence.repositories import history
    owned, path = environment
    admit(owned)
    report = freeze(owned)
    repo = history.HistoryRepository(path / "state.db")
    with repo.report_read_scope():
        queries = []
        repo._report_connection.set_trace_callback(queries.append)
        monkeypatch.setattr(history, "decode_event_row", lambda *args: pytest.fail("状态查询不能解析事件正文"))
        assert repo.status_history("action", 1, report.boundary, 4) == [
            {"status": 4, "changed_at": 2_900_000, "clock_status": 1}]
        repo.status_history("plan", 1, report.boundary, 3)
        members = [sql for sql in queries if "COUNT(*), MIN(id), MAX(id) FROM history_events" in sql]
        assert len(members) == 1, members
        statement = next(sql for sql in queries if "SELECT l.event_id" in sql)
        plan = owned.connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
        assert any("entity_links_status" in row[-1] for row in plan), plan
        assert all("body_json" not in sql for sql in queries)


@pytest.mark.asyncio
async def test_status_history_rejects_wrong_entity_enum_and_broken_chain(environment):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.history import HistoryRepository
    owned, path = environment
    admit(owned)
    report = freeze(owned)
    repo = HistoryRepository(path / "state.db")
    owned.connection.execute("UPDATE entity_event_links SET after_status=6 WHERE entity_type=4")
    with pytest.raises(ConsistencyError):
        repo.status_history("plan", 1, report.boundary, 3)
    owned.connection.execute("UPDATE entity_event_links SET after_status=3,before_status=1 WHERE entity_type=4")
    with pytest.raises(ConsistencyError):
        repo.status_history("plan", 1, report.boundary, 3)
