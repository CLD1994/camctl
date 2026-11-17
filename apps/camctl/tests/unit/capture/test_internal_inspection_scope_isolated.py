"""原内部读取与检查结果在回执未知时沿原身份恢复。"""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from camctl.capture import media_flow, segment_inspections
from camctl.operations.attempts import RunFinish, RunOutcome
from camctl.operations.models import ErrorValue
from camctl.outputs import read_attempts
from camctl.outputs.slots import SlotDecision, SlotOutcome
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from unit.capture.test_internal_read_save_owner import _flow


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


@pytest.mark.asyncio
async def test_original_result_and_inspection_unknown_recover_with_shared_keys(monkeypatch):
    flow, original = _flow()
    pending = replace(original, finish=replace(original.finish,
        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("device_binding_unavailable", "read"))))
    ticket = pending.finish.ticket
    identity = (ticket.run_id, ticket.attempt_id)
    results = {identity: pending}
    inspections, slot = {}, ["camera"]
    flow.pending_read_results = results
    read_keys, writes, releases = [], [], []
    inspection = SimpleNamespace(inspection_id=8, state=1, target_json={"metric": "frames"})
    error = {"code": "device_binding_unavailable", "stage": "execution", "details": {"reason": "missing"}}

    class Connection:
        def execute(self, sql, params):
            if sql.startswith("SELECT c.inspection_id,c.committed_bytes"):
                return Cursor([(8, 10, 10, None, None, 1, None, ticket.run_id, 4, json.dumps(error), 0, 1)])
            if sql.startswith("SELECT 1 FROM operation_attempts"):
                return Cursor([])
            if sql.startswith("SELECT c.inspection_id,c.slot_device_id"):
                return Cursor([(8, slot[0], 4, 0, inspection.state, 0, 55)])
            if sql.startswith("SELECT id FROM file_copies WHERE inspection_id"):
                return Cursor([(9,)])
            if sql.startswith("SELECT processing_id,inspection_id"):
                return Cursor([(None, 8)])
            if sql.startswith("SELECT status FROM operation_runs"):
                return Cursor([(4,)])
            raise AssertionError(sql)

    connection = Connection()
    owned = SimpleNamespace(connection=connection)
    async def read(callback, **kwargs): return callback(owned)
    async def write(callback, **kwargs): return callback(owned)
    flow.database.read.side_effect = read
    flow.database.write.side_effect = write
    repository = Mock()
    repository.read.return_value = inspection
    def save_result(request, key, actual_owned):
        assert actual_owned is owned
        writes.append((request, key))
        if len(writes) == 1:
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("检查回执未知"))
        inspection.state = 3
        return DbOutcome(DbOutcomeKind.COMPLETED, value=inspection)
    repository.save_result.side_effect = save_result
    monkeypatch.setattr(segment_inspections, "SegmentInspectionRepository", lambda: repository)
    monkeypatch.setattr(segment_inspections, "new_operation_key", lambda: "original-inspection-key")
    monkeypatch.setattr(segment_inspections, "record_error", lambda *args, **kwargs: None)

    async def save_read_result(scope, actual, *, retry=False):
        assert actual is pending
        read_keys.append(actual.key)
        if len(read_keys) == 1:
            raise RuntimeError("read receipt unknown")
        return actual
    monkeypatch.setattr(media_flow, "save_read_result", save_read_result)

    async def release(scope, copy_id, stage, request, method, **kwargs):
        releases.append((copy_id, stage, request))
        slot[0] = None
        return SimpleNamespace(value=SlotDecision(SlotOutcome.RELEASED))
    monkeypatch.setattr(read_attempts, "save_read_business", release)
    continuations = {9: SimpleNamespace(ticket=ticket)}
    args = dict(pending_read_results=results, pending_read_business={}, pending_read_ends={},
                continuing_read_tickets=continuations, pending_segment_inspection_results=inspections,
                occurred_at=lambda: 20)
    with pytest.raises(RuntimeError, match="read receipt unknown"):
        await media_flow.resume_prepared_internal_reads(flow.database, **args)
    assert results[identity] is pending
    assert not writes and not releases and slot[0] == "camera"

    await media_flow.resume_prepared_internal_reads(flow.database, **args)
    assert read_keys == [pending.key, pending.key]
    assert [key for _, key in writes] == ["original-inspection-key", "original-inspection-key"]
    assert writes[0][0] == writes[1][0]
    assert writes[0][0].inspection_id == 8
    assert writes[0][0].result_json["failure"]["read_error"] == error
    assert writes[0][0].result_json["tool"]["ran"] is False
    assert not inspections and not results and not continuations
    assert len(releases) == 1 and slot[0] is None
    await media_flow.resume_prepared_internal_reads(flow.database, **args)
    assert read_keys == [pending.key, pending.key]
    assert len(releases) == 1
