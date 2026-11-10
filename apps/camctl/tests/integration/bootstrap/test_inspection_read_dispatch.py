"""正式拍摄工厂与普通、残留流程续接检查副本的实际READ结果。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.capture.segment_inspections import SegmentInspectionPhase
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import check_binding
from camctl.outputs.copy import SourceDigest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..support.segment_inspection_cases import CONTENT as _CONTENT, inspection_source
from ..support.bootstrap_consumers import TransactionFault
from ..support.bootstrap_read_cases import inspection_case, held_inspection_case

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def source(tmp_path):
    data = await inspection_source(tmp_path)
    try:
        yield data
    finally:
        data[0].connection.close()


class _DigestGate:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def read_digest(self):
        self.entered.set()
        await self.release.wait()
        self.calls += 1
        return SourceDigest(digest=hashlib.sha256(_CONTENT).hexdigest(), error=None)


def _binding(change):
    devices = {} if change == "missing" else {"cam-1": {"kind": "camera", "driver": "replacement-camera"}}
    return lambda saved: check_binding(saved, SimpleNamespace(devices=devices))


def _copy(owned, copy_id):
    return owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state"
        " FROM file_copies WHERE id=?", (copy_id,)).fetchone()


@pytest.mark.parametrize("entry", ["ordinary", "residual"])
async def test_clean_inspection_end_remains_with_owner_during_digest(source, capture_resources, entry):
    digest = _DigestGate()
    world = await inspection_case(source, capture_resources, digest=digest)
    task = asyncio.create_task(world.inspect())
    try:
        await asyncio.wait_for(digest.entered.wait(), 5)
        ticket = world.held_ticket()
        copy_id = int(ticket.target_id)
        before = world.owned.connection.execute(
            "SELECT c.processing_id,c.inspection_id,c.committed_bytes,c.source_size,c.round,c.recopies_used,"
            "r.id,r.attempts_used,r.max_attempts_used,t.status,t.result_json FROM file_copies c"
            " JOIN operation_runs r ON r.copy_id=c.id JOIN operation_attempts t ON t.run_id=r.id WHERE c.id=?",
            (copy_id,)).fetchone()
        assert before[:6] == (None, 1, len(_CONTENT), len(_CONTENT), 1, 0)
        assert before[6:] == (ticket.run_id, 1, 3, 1, None)
        assert world.tools() == [] and not task.done()
        world.prime_clock()
        await world.replay("normal" if entry == "ordinary" else entry)
        assert len(world.reader.requests) == 1 and world.tools() == [] and not task.done()
        assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
            (ticket.run_id,)).fetchone() == (1,)
    finally:
        digest.release.set()
        result = await asyncio.wait_for(task, 5)
        await world.close()
    assert result.phase is SegmentInspectionPhase.COMPLETED
    assert len(world.reader.requests) == 1 and digest.calls == 1 and world.tools() == ["probe"]
    assert world.owned.connection.execute(
        "SELECT r.id,r.attempts_used,t.attempt_no,t.status FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id WHERE r.copy_id=?", (copy_id,)).fetchone() == (
            ticket.run_id, 1, ticket.attempt_id, 2)


@pytest.mark.parametrize("entry,change", [("ordinary", "missing"), ("residual", "mismatch")])
async def test_inspection_missing_required_digest_saves_actual_read_then_check_failure(
        source, capture_resources, monkeypatch, entry, change):
    world, ticket = await held_inspection_case(source, capture_resources, monkeypatch)
    copy_id = int(ticket.target_id)
    before = _copy(world.owned, copy_id)
    try:
        await world.replay("normal" if entry == "ordinary" else entry, binding=_binding(change))
        row = world.owned.connection.execute(
            "SELECT t.status,t.error_json,t.result_json,r.status,r.error_json,r.attempts_used"
            " FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.copy_id=?", (copy_id,)).fetchone()
        assert row[:2] == (2, None) and row[3] == 4 and row[5] == 1
        assert json.loads(row[2])["settlement"] == {
            "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
        error = json.loads(row[4])
        assert error["code"] == "device_binding_unavailable" and error["details"]["reason"] == change
        assert _copy(world.owned, copy_id) == before
        inspection = world.owned.connection.execute("SELECT state,result_json FROM segment_inspections WHERE id=1").fetchone()
        assert inspection[0] == 3 and json.loads(inspection[1])["failure"]["read_error"] == error
        assert world.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == (None,)
        assert len(world.reader.requests) == 1 and world.digests == [] and world.tools() == []
        assert len(world.completions) == 1
        final = await world.inspect()
        assert final.phase is SegmentInspectionPhase.FAILED and final.reason == "device_binding_unavailable"
        assert len(world.reader.requests) == 1
    finally:
        await world.close()


@pytest.mark.parametrize("digest_state", ["saved", "unsupported"])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_inspection_reliable_or_unsupported_digest_finishes_locally(
        source, capture_resources, monkeypatch, digest_state, change):
    world, ticket = await held_inspection_case(source, capture_resources, monkeypatch, digest_state)
    try:
        before = _copy(world.owned, int(ticket.target_id))
        await world.replay("normal", binding=_binding(change))
        assert _copy(world.owned, int(ticket.target_id)) == before and world.tools() == []
        final = await world.inspect()
        assert final.phase is SegmentInspectionPhase.COMPLETED
        assert len(world.reader.requests) == 1 and len(world.digests) == int(digest_state == "saved")
        assert world.tools() == ["probe"] and len(world.completions) == 1
        assert world.owned.connection.execute("SELECT id,status,attempts_used FROM operation_runs WHERE copy_id=?",
            (int(ticket.target_id),)).fetchone() == (ticket.run_id, 3, 1)
    finally:
        await world.close()


@pytest.mark.parametrize("damage", ["copy", "ticket", "attempt", "action", "member", "set", "source", "observer"])
async def test_inspection_dispatch_rejects_broken_owner_relations(source, capture_resources, monkeypatch, damage):
    world, ticket = await held_inspection_case(source, capture_resources, monkeypatch)
    connection, copy_id = world.owned.connection, int(ticket.target_id)
    try:
        # 在规定存储格式模拟原关系损坏，实际READ票据与End保持原值。
        connection.execute("PRAGMA foreign_keys=OFF")
        statement, values = {
            "copy": ("UPDATE file_copies SET id=id+100 WHERE id=?", (copy_id,)),
            "ticket": ("UPDATE operation_runs SET id=id+100 WHERE id=?", (ticket.run_id,)),
            "attempt": ("UPDATE operation_attempts SET attempt_no=attempt_no+100 WHERE run_id=?", (ticket.run_id,)),
            "action": ("UPDATE operation_runs SET action_id=99 WHERE copy_id=?", (copy_id,)),
            "member": ("UPDATE segment_inspections SET member_id=99 WHERE id=1", ()),
            "set": ("UPDATE segment_members SET set_id=99 WHERE id=?", (world.member.member_id,)),
            "source": ("UPDATE segment_inspections SET source_device_file_id=99 WHERE id=1", ()),
            "observer": ("UPDATE device_files SET observer_action_id=99 WHERE id=?", (world.member.file_id,)),
        }[damage]
        connection.execute(statement, values)
        connection.commit()
        before = tuple(connection.iterdump())
        await world.replay("normal", failure=True, binding=lambda saved: check_binding(saved, world.config))
        assert tuple(connection.iterdump()) == before and not connection.in_transaction
        assert len(world.reader.requests) == 1 and world.digests == [] and world.tools() == []
        assert world.held_ticket() == ticket
    finally:
        await world.close()


@pytest.mark.parametrize("boundary", ["read", "inspection"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_inspection_binding_failure_unknown_reuses_original_requests(
        source, capture_resources, monkeypatch, boundary, after_commit):
    world, ticket = await held_inspection_case(source, capture_resources, monkeypatch)
    calls, receipts = [], []
    repository_type, method = ((OperationRepository, "finish_binding_read") if boundary == "read" else
                              (SegmentInspectionRepository, "save_result"))
    original = getattr(repository_type, method)

    def save(repository, request, key, owned, **options):
        calls.append((request, key))
        if len(calls) == 1:
            fault = TransactionFault(owned.connection, after_commit=after_commit)
            outcome = original(repository, request, key, replace(owned, connection=fault), **options)
            assert fault.commit_calls == 1 and outcome.kind is DbOutcomeKind.UNKNOWN, outcome.error
            receipts.append(outcome)
            return outcome
        return original(repository, request, key, owned, **options)

    monkeypatch.setattr(repository_type, method, save)
    try:
        await world.replay("normal", failure=boundary == "read", binding=_binding("missing"))
        assert len(calls) == len(receipts) == 1
        request, key = calls[0]
        before_attempts = world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()
        await world.reconnect()
        await world.rebuild()
        await world.replay("normal", binding=_binding("missing"))
        assert len(calls) >= 2 and all(call == (request, key) for call in calls)
        events = saved_transaction_events(world.owned.connection, key)
        assert events is not None
        assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT r.id,r.status,r.attempts_used,t.attempt_no,t.status FROM operation_runs r"
            " JOIN operation_attempts t ON t.run_id=r.id WHERE r.copy_id=?", (int(ticket.target_id),)).fetchone() == (
                ticket.run_id, 4, 1, ticket.attempt_id, 2)
        assert world.owned.connection.execute("SELECT state FROM segment_inspections WHERE id=1").fetchone() == (3,)
        assert world.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
            (int(ticket.target_id),)).fetchone() == (None,)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == before_attempts
        assert len(world.reader.requests) == 1 and world.digests == [] and world.tools() == []
        assert (await world.inspect()).phase is SegmentInspectionPhase.FAILED
    finally:
        await world.close()


@pytest.mark.parametrize("priority", ["action_terminal", "read_terminal", "canceled"])
async def test_inspection_end_dispatch_preserves_existing_terminal_or_cancel(source, capture_resources, monkeypatch, priority):
    world, ticket = await held_inspection_case(source, capture_resources, monkeypatch)
    connection, copy_id = world.owned.connection, int(ticket.target_id)
    try:
        if priority in ("action_terminal", "read_terminal"):
            from camctl.contracts.values import new_operation_key
            from camctl.devices.bindings import DeviceBinding, binding_failure_details
            from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
            from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
            from camctl.operations.validation import validate_outcome

            actual = validate_outcome(ticket, CallOutcome(status=AttemptStatus.SUCCEEDED,
                effect=EffectState.UNKNOWN, settlement=Settlement(SettlementBasis.OBSERVED,
                    EvidenceValue("read_returned", 1, {}))), world.entry.evidence)
            binding = _binding("missing")(DeviceBinding("cam-1", "camctl-adb"))
            failure = ErrorValue("device_binding_unavailable", "execution", binding_failure_details(binding))
            key = new_operation_key()
            receipt = await world.database.write(lambda owned: OperationRepository().finish_binding_read(
                AttemptFinish(ticket, actual, world.wall[0], run_finish=RunFinish(RunOutcome.FAILED, failure)),
                key, owned), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        if priority in ("action_terminal", "canceled"):
            from camctl.bootstrap.flows import cancel_flow
            from camctl.capture.handlers import capture_handler
            from ..support.bootstrap_consumers import accept_cancellation

            await accept_cancellation(world, occurred_at=world.wall[0])
            await cancel_flow(staging=world.config.paths.staging,
                ready=world.config.paths.ready, processing=world.config.paths.processing)(
                    SimpleNamespace(database=world.database,
                        clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
            if priority == "action_terminal":
                await capture_handler("camera_record")(world.ref.action_id, world.runtime)
                assert connection.execute("SELECT status FROM actions WHERE id=?",
                    (world.ref.action_id,)).fetchone() == (6,)
        before = tuple(connection.iterdump())
        await world.replay("normal", binding=_binding("missing"))
        assert tuple(connection.iterdump()) == before and not connection.in_transaction
        assert world.held_ticket() == ticket
        assert len(world.reader.requests) == 1 and world.digests == [] and world.tools() == []
    finally:
        await world.close()


async def test_original_inspection_result_survives_recreated_capture_runtime(source, capture_resources, monkeypatch):
    world = await inspection_case(source, capture_resources)
    original = SegmentInspectionRepository.save_result
    saves = []

    def lost_reply(repository, request, key, owned):
        saves.append((request, key))
        if len(saves) == 1:
            fault = TransactionFault(owned.connection, after_commit=True)
            outcome = original(repository, request, key, replace(owned, connection=fault))
            assert outcome.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            return outcome
        return original(repository, request, key, owned)

    monkeypatch.setattr(SegmentInspectionRepository, "save_result", lost_reply)
    try:
        waiting = await world.inspect()
        assert waiting.phase is SegmentInspectionPhase.WAITING and waiting.reason == "result_save_unknown"
        await world.reconnect()
        await world.rebuild()
        final = await world.inspect()
        assert final.phase is SegmentInspectionPhase.COMPLETED and final.metric.value == Decimal("1.5")
        assert saves == [saves[0], saves[0]]
        assert len(world.reader.requests) == 1 and world.tools() == ["probe"]
    finally:
        await world.close()
