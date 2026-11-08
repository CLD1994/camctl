"""真实受理和会话流程保持取回、正式源清理及固定历史报告的独立性。"""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import pytest
import pytest_asyncio

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.obtain_assembly import obtain_flow
from camctl.contracts.values import new_operation_key
from camctl.history.queries import FileHistoryKind, FileHistoryRequest
from camctl.outputs.handoff import DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery
from camctl.outputs.sources import SourceSpec
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.outputs import (
    FinishObtain, OutputsRepository, PublicationIntentRequest, ResolveSources,
)
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import ReportDecisionKind, ReportingRepository

from ..bootstrap.test_output_binding_changes import (
    Catalog, _CONTENT, _NOW, _registry, _save_photos, environment,  # noqa: F401
)
from ..support.outputs import obtain_factory

pytestmark = pytest.mark.asyncio


def _value(owned, sql, *parameters):
    row = owned.connection.execute(sql, parameters).fetchone()
    assert row is not None, sql
    return row


async def _submit(context, body):
    return await accept_input(
        ParsedInput("outputs.json", body),
        AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(), clock=context.clock),
        new_operation_key(), context.database,
    )


def _obtain_body(request_id, source, names):
    return {
        "request_id": str(request_id), "created_at": "2026-01-15 08:00:00",
        "name": "独立取回", "actions": [{
            "name": name, "type": "obtain_action_outputs",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"source": {"action_instance_id": str(source)}},
        } for name in names],
    }


@pytest_asyncio.fixture
async def prepared_pair(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_id, device_file_id, source_id = _value(owned,
        "SELECT o.id,o.device_file_id,o.source_action_id FROM outputs o"
        " JOIN actions a ON a.id=o.source_action_id WHERE a.device_id='cam-a'")
    body = _obtain_body(9002, source_id, ("obtain-1", "obtain-2"))
    accepted = await _submit(context, body)
    assert accepted.plan_id is not None, accepted
    factory = obtain_factory(
        devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0, segment_size=4,
    )
    flow = obtain_flow(factory)
    # 同机取回按批次连续执行；第二轮先发布已准备的第一份，再读取另一份。
    await flow(context)
    assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == [(3,), (1,)]
    released, = owned.connection.execute(
        "SELECT b.released_event_id FROM device_file_batches b"
        " JOIN deliveries d ON d.action_id=b.action_id ORDER BY d.id LIMIT 1").fetchone()
    assert released is not None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.kind=3 AND a.status=1").fetchone() == (0,)
    await flow(context)
    copies = owned.connection.execute(
        "SELECT d.id,c.target_file_id,c.id,d.action_id,d.status,d.file_name,"
        " f.relative_path,c.source_size,c.committed_bytes,c.target_sha256 FROM deliveries d"
        " JOIN file_copies c ON c.delivery_id=d.id"
        " JOIN intermediate_files f ON f.id=c.target_file_id ORDER BY d.id").fetchall()
    assert len(copies) == 2
    assert [row[4] for row in copies] == [5, 3]
    assert owned.connection.execute("SELECT source_dependency FROM obtain_items ORDER BY id").fetchall() == [(0,), (0,)]
    expected = hashlib.sha256(_CONTENT).hexdigest()
    for row in copies:
        assert row[7:] == (len(_CONTENT), len(_CONTENT), expected)
        path = (Path(cfg.paths.ready) / row[5] if row[4] == 5 else Path(cfg.paths.staging) / row[6])
        assert path.read_bytes() == _CONTENT
    assert len(driver.reads) == 2
    second, first = [SimpleNamespace(
        delivery_id=row[0], target_file_id=row[1], copy_id=row[2], action_id=row[3],
    ) for row in copies]
    return {
        "cfg": cfg, "owned": owned, "context": context, "driver": driver, "flow": flow,
        "tmp_path": Path(cfg.paths.state_db).parent, "staging": Path(cfg.paths.staging),
        "ready": Path(cfg.paths.ready), "processing": Path(cfg.paths.processing),
        "output_id": output_id, "device_file_id": device_file_id, "photo_action": source_id,
        "first": first, "second": second, "first_action": first.action_id,
        "second_action": second.action_id, "body": body,
        "boundary": HistoryRepository(Path(cfg.paths.state_db)).current_boundary(),
    }


async def _publish(pair, delivery_id):
    return await publish_delivery(delivery_id, DeliveryContext(
        repository=OutputsRepository(), database=pair["context"].database,
        directories=DeliveryDirectories(pair["staging"], pair["ready"], pair["processing"]),
        occurred_at=_NOW,
    ))


async def _cleanup_output(pair):
    receipt = await _submit(pair["context"], {
        "request_id": "9003", "created_at": "2026-01-15 08:00:00", "name": "清理正式源",
        "actions": [{"name": "清理", "type": "delete_action_outputs",
                     "scheduled_at": "2026-01-15 09:00:00",
                     "params": {"source": {"action_instance_id": str(pair["photo_action"])}}}],
    })
    assert receipt.plan_id is not None, receipt
    cfg = pair["cfg"]
    flow = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(pair["driver"]), max_delete_attempts=1,
        max_query_attempts=1, staging=pair["staging"], occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    ))
    await flow(pair["context"])
    await flow.settle()
    assert _value(pair["owned"], "SELECT availability,cleanup_status FROM outputs WHERE id=?",
                  pair["output_id"]) == (3, 4)
    assert _value(pair["owned"], "SELECT status FROM actions WHERE plan_id=?", receipt.plan_id) == (3,)


def _reuse_original_source(pair):
    from camctl.contracts.values import OperationKey
    owned = pair["owned"]
    for action_id in (pair["first_action"], pair["second_action"]):
        key, occurred_at = _value(owned,
            "SELECT t.operation_key,e.occurred_at FROM history_events e"
            " JOIN history_transactions t ON t.id=e.transaction_id WHERE e.event_type=3"
            " AND json_extract(e.body_json,'$.evidence.source_resolution_request.action_id')=?"
            " ORDER BY e.id LIMIT 1", action_id)
        before = tuple(owned.connection.iterdump())
        saved = OutputsRepository().resolve_sources(
            ResolveSources(action_id, SourceSpec(action_instance_id=pair["photo_action"]), occurred_at),
            OperationKey(key), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.fixed and saved.value.member_count == saved.value.selection_count == 1
        assert tuple(owned.connection.iterdump()) == before


class TestOutputLifecyclesAreIndependent:
    async def test_output_lifecycles_are_independent(self, prepared_pair):
        pair, owned = prepared_pair, prepared_pair["owned"]
        first, second = pair["first"], pair["second"]
        assert first.delivery_id != second.delivery_id
        names = [row[0] for row in owned.connection.execute("SELECT file_name FROM deliveries ORDER BY id")]
        assert len(set(names)) == 2
        reads_before = tuple(pair["driver"].reads)
        replay = await _submit(pair["context"], pair["body"])
        assert replay.plan_id is not None, replay
        assert _value(owned, "SELECT COUNT(*) FROM deliveries") == (2,)
        assert tuple(pair["driver"].reads) == reads_before
        _reuse_original_source(pair)
        await _cleanup_output(pair)
        _reuse_original_source(pair)
        await pair["flow"](pair["context"])
        assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == [(5,), (5,)]
        for name in names:
            assert (pair["ready"] / name).read_bytes() == _CONTENT
        assert owned.connection.execute("SELECT status FROM actions WHERE type=4 ORDER BY id").fetchall() == [(3,), (3,)]
        _reuse_original_source(pair)


class TestCleanedOutputBlocksNewObtain:
    async def test_cleaned_output_rejects_new_request_but_history_remains(self, prepared_pair):
        pair, owned = prepared_pair, prepared_pair["owned"]
        history = HistoryRepository(Path(pair["cfg"].paths.state_db))
        request = FileHistoryRequest(boundary=pair["boundary"], kind=FileHistoryKind.OUTPUT_FILE,
                                     output_id=pair["output_id"])
        before = history.read_files_at_h(request)
        assert [record.file_id for record in before.page.items] == [pair["device_file_id"]]
        assert before.page.items[0].row["presence_state"] == 2
        await _cleanup_output(pair)
        current = history.read_files_at_h(FileHistoryRequest(
            boundary=history.current_boundary(), kind=FileHistoryKind.OUTPUT_FILE,
            output_id=pair["output_id"]))
        assert current.page.items[0].row["presence_state"] == 3
        assert history.read_files_at_h(request).page.items[0].row["presence_state"] == 2
        receipt = await _submit(pair["context"], _obtain_body(9004, pair["photo_action"], ("obtain-3",)))
        assert receipt.plan_id is not None, receipt
        await pair["flow"](pair["context"])
        assert _value(owned, "SELECT status,error_code FROM actions WHERE plan_id=?", receipt.plan_id) == (4, 21)
        error, = _value(owned,
            "SELECT i.error_details_json FROM obtain_items i JOIN obtain_source_selections s"
            " ON s.id=i.selection_id JOIN action_dependencies d ON d.id=s.dependency_id"
            " JOIN actions a ON a.id=d.action_id WHERE a.plan_id=?", receipt.plan_id)
        assert json.loads(error)["output_id"] == str(pair["output_id"])
        assert json.loads(error)["availability"] == "cleaned"
        assert _value(owned, "SELECT COUNT(*) FROM deliveries") == (2,)


class TestFrozenReportBytes:
    async def test_frozen_report_bytes_survive_later_progress(self, prepared_pair):
        pair = prepared_pair
        report = _freeze_report(pair["owned"])
        first = generate_report_file(_report_spec(pair["tmp_path"], report, "first.json"))
        assert first.size_bytes > 0
        await _submit(pair["context"], pair["body"])
        await _cleanup_output(pair)
        assert (await _publish(pair, pair["first"].delivery_id)).phase is DeliveryPhase.PUBLISHED
        rebuilt = generate_report_file(_report_spec(pair["tmp_path"], report, "second.json"))
        assert rebuilt.path.read_bytes() == first.path.read_bytes()
        assert (rebuilt.size_bytes, rebuilt.sha256) == (first.size_bytes, first.sha256)


class TestFailureUnionBeyondDeliveryIncrement:
    async def test_failed_delivery_outside_window_still_reported(self, prepared_pair):
        pair, owned = prepared_pair, prepared_pair["owned"]
        delivery = pair["first"]
        saved = OutputsRepository().save_publication_intent(
            PublicationIntentRequest(delivery.delivery_id, _NOW), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        relative, = _value(owned, "SELECT relative_path FROM intermediate_files WHERE id=?", delivery.target_file_id)
        (pair["staging"] / relative).unlink()
        assert (await _publish(pair, delivery.delivery_id)).phase is DeliveryPhase.FAILED_FINAL
        first = _freeze_report(owned)
        doc = _read_report(generate_report_file(_report_spec(pair["tmp_path"], first, "union-1.json")))
        action_name, = _value(owned, "SELECT name FROM actions WHERE id=?", delivery.action_id)
        fragment = _action_fragment(doc, action_name)
        assert [item["error"]["code"] for item in fragment["result"]["failures"]] == ["delivery_handoff_unconfirmed"]
        assert fragment["result"]["failures"][0]["delivery_id"] == str(delivery.delivery_id)
        ack = await _submit(pair["context"], {
            "request_id": f"ack-{first.report_id}", "last_report_id": str(first.report_id),
        })
        assert ack.ack_watermark == first.to_wm, ack
        finished = OutputsRepository().finish_obtain(
            FinishObtain(pair["first_action"], _NOW), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED and finished.value.action_status == 4, finished.error
        second = _freeze_report(owned)
        assert second.from_wm == first.to_wm
        doc = _read_report(generate_report_file(_report_spec(pair["tmp_path"], second, "union-2.json")))
        fragment = _action_fragment(doc, action_name)
        assert "deliveries" not in fragment
        assert [item["error"]["code"] for item in fragment["result"]["failures"]] == ["delivery_handoff_unconfirmed"]
        assert fragment["result"]["failures"][0]["delivery_id"] == str(delivery.delivery_id)


def _freeze_report(owned):
    receipt = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=_NOW)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.kind is ReportDecisionKind.GENERATE, receipt.value
    return receipt.value.report


def _report_spec(tmp_path, report, name):
    return GenerationSpec(
        db_path=tmp_path / "state.db", report_id=report.report_id, from_wm=report.from_wm,
        to_wm=report.to_wm, frozen_event_id=report.boundary.last_event_id,
        staging_path=tmp_path / "reports" / name,
    )


def _read_report(generated):
    return json.loads(generated.path.read_text(encoding="utf-8"))


def _action_fragment(document, name):
    for plan in document["plans"]:
        for action in plan.get("actions", ()):
            if action["name"] == name:
                return action
    raise AssertionError(name)
