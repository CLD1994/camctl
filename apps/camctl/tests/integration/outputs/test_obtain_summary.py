"""X10 取回统一发布汇总的组件集成测试。

真实 SQLite 与真实选择、准备及发布事务组合：从已保存的选择、条
目与交付行映射汇总事实，按开始发布的条件判定等待与发布分区；
判定通过后经真实发布入口逐文件发布成功项并保留失败结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import closing
from io import BytesIO

import pytest_asyncio

from ..support.outputs import obtain_factory, output_drivers, output_operation, prepare_obtain_selection
from ..support.public_delivery_cases import prepare_public_delivery
from ..support.capture import capture_database
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, consumer_world
from ..capture.test_capture_contract import ResultsDouble

from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import ObservedFile, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.read_session import ReadSession
from camctl.outputs.obtain_flow import advance_obtain
from camctl.outputs.handoff import (
    DeliveryContext,
    DeliveryDirectories,
    DeliveryPhase,
    publish_delivery,
)
from camctl.outputs.obtain_summary import (
    ObtainPhase,
    decide_obtain_finish,
)
from camctl.host_files.models import BoundDirectories
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.file_batches import register_file_batch_guards
from camctl.persistence.repositories.outputs import (
    FinishObtain,
    ObtainFinishDisposition,
    OutputsRepository, load_obtain_facts,
)
from camctl.persistence.transaction import TransactionError

from .test_copy_segments import _CONTENT
from .test_qualification import _NOW, _seed_action

register_operation_guards()
register_capture_guards()


def _roots(tmp_path):
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    return BoundDirectories(staging=staging)


def _value(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        row = cursor.fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _facts(owned, action_id):
    return load_obtain_facts(owned.connection, action_id)


@pytest_asyncio.fixture
async def prepared_env(tmp_path):
    """通过正式拍摄和取回形成实际 READ 成功且等待发布的交付。"""
    owned, roots, directories, qualification = await prepare_public_delivery(
        tmp_path, content=_CONTENT, now=_NOW)
    try:
        yield owned, roots, directories, qualification
    finally:
        owned.connection.close()


@pytest_asyncio.fixture
async def prepared_with_failure(tmp_path):
    """正式精确请求同时选择真实原片和不存在的 ID。"""
    class Catalog(ResultCatalog):
        def action_types(self):
            return super().action_types() | {"obtain_action_outputs"}

    class Stream(BytesIO):
        def cancel(self):
            pass

    class Driver:
        def __init__(self):
            self.reads, self.digests = [], []

        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.reads.append(ticket)
            return ReadSession(source, offset, Stream(_CONTENT[offset:]), idle_timeout_s)

        async def digest(self, request):
            self.digests.append(request)
            return DeviceCallResult((DeviceObservation("file_digest", 1, {
                "file_id": request.params["file_id"],
                "sha256": hashlib.sha256(_CONTENT).hexdigest(),
            }),), None)

    def capture_options(owned, _driver, _defaults):
        action_id, = owned.connection.execute("SELECT id FROM actions").fetchone()
        entry = ObservedFile(identity="delivery-source", locator={"path": "/DCIM/source.jpg"},
            evidence={"listing": "source"}, complete=True, size_bytes=len(_CONTENT),
            kind=FileKind.PHOTO, original_name="source.jpg", media_type="image/jpeg")
        return {"results": ResultsDouble({action_id: (entry,)}),
                "evidence": RESULT_EVIDENCE.with_contract(RESULT_PAGE_CONTRACT)}

    register_file_batch_guards()
    owned, capture, source_id, handler = await consumer_world(
        tmp_path, "photo", catalog=Catalog(), start=False, now_us=_NOW,
        runtime_options=capture_options)
    try:
        await capture_handler(handler)(source_id, capture)
        output_id, = _value(owned, "SELECT id FROM outputs WHERE source_action_id=?", source_id)
        assert owned.connection.execute("SELECT 1 FROM outputs WHERE id=705").fetchone() is None
        action_id, _ = prepare_obtain_selection(owned, catalog=Catalog(), now=_NOW,
            source_id=source_id, mode=[output_id, 705], request_id=2)
        staging = tmp_path / "staging"
        directories = DeliveryDirectories(staging=staging, ready=tmp_path / "ready", processing=tmp_path / "processing")
        for directory in (directories.staging, directories.ready, directories.processing):
            directory.mkdir(parents=True, exist_ok=True)
        driver = Driver()
        evidence = EvidenceRegistry((
            EvidenceContract("read_returned", 1, "read", frozenset()),
            EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        ))
        factory = obtain_factory(devices={"cam-1": {"driver": "camctl-adb"}},
            drivers=output_drivers(driver, evidence, read=True, digest=True),
            staging=directories.staging, ready=directories.ready, processing=directories.processing,
            occurred_at=lambda: _NOW + 1, monotonic_ns=lambda: 0, segment_size=len(_CONTENT))
        await advance_obtain(factory(await capture_database(owned)))
        delivery_id, = _value(owned, "SELECT id FROM deliveries WHERE action_id=?", action_id)
        status, attempt, result_event, size, committed, source_hash, target_hash, verification, dependency, relative_path = _value(owned,
            "SELECT d.status,a.status,a.result_event_id,c.source_size,c.committed_bytes,"
            " c.source_sha256,c.target_sha256,c.verification_state,i.source_dependency,f.relative_path FROM deliveries d"
            " JOIN file_copies c ON c.delivery_id=d.id JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN operation_attempts a ON a.run_id=r.id JOIN obtain_items i ON i.delivery_id=d.id"
            " JOIN intermediate_files f ON f.id=c.target_file_id WHERE d.id=?", delivery_id)
        assert (status, attempt) == (3, 2) and result_event is not None
        digest = hashlib.sha256(_CONTENT).hexdigest()
        assert (size, committed, source_hash, target_hash, verification, dependency) == (
            len(_CONTENT), len(_CONTENT), digest, digest,
            int(enum_for("file_copies.verification_state").MATCHED), 0)
        assert (staging / relative_path).read_bytes() == _CONTENT
        assert not tuple(directories.ready.iterdir()) and not tuple(directories.processing.iterdir())
        assert len(driver.reads) == len(driver.digests) == 1
        failed = _value(owned, "SELECT id,status,delivery_id,error_code FROM obtain_items"
            " WHERE requested_output_id=705")
        assert failed[1:] == (4, None, 1)
        from types import SimpleNamespace
        qualification = SimpleNamespace(action_id=action_id, delivery_id=delivery_id, failed_item_id=failed[0])
        yield owned, BoundDirectories(staging=staging), directories, qualification
    finally:
        owned.connection.close()


def _add_waiting_source(owned, action_id):
    """为汇总仓储准备另一个尚未完成选择的来源，不修改已固定选择。"""
    source_plan, = _value(owned, "SELECT resolved_source_plan_id FROM actions WHERE id=?", action_id)
    source_id, = _value(owned, "SELECT MAX(id)+1 FROM actions")
    dependency_id, = _value(owned, "SELECT COALESCE(MAX(id),0)+1 FROM action_dependencies")
    selection_id, = _value(owned, "SELECT COALESCE(MAX(id),0)+1 FROM obtain_source_selections")
    existing_source, = _value(owned,
        "SELECT d.depends_on_action_id FROM action_dependencies d WHERE d.action_id=?", action_id)
    _seed_action(owned.connection, source_id, source_plan, action_type=1)
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=(SELECT input_fields_json FROM actions WHERE id=?),"
        " effective_params_json=(SELECT effective_params_json FROM actions WHERE id=?),"
        " execution_spec_json=(SELECT execution_spec_json FROM actions WHERE id=?) WHERE id=?",
        (existing_source, existing_source, existing_source, source_id))
    owned.connection.execute("UPDATE actions SET group_name=? WHERE id IN (?,?)",
        ("summary-group", existing_source, source_id))
    raw, = _value(owned, "SELECT input_fields_json FROM actions WHERE id=?", action_id)
    request = json.loads(raw)
    request["params"]["source"] = {"plan_instance_id": str(source_plan), "group": "summary-group"}
    owned.connection.execute("UPDATE actions SET input_fields_json=? WHERE id=?",
        (json.dumps(request), action_id))
    owned.connection.execute(
        "INSERT INTO action_dependencies (id,action_id,depends_on_action_id) VALUES (?,?,?)",
        (dependency_id, action_id, source_id))
    owned.connection.execute(
        "INSERT INTO obtain_source_selections (id,dependency_id,status) VALUES (?,?,1)",
        (selection_id, dependency_id))
    owned.connection.commit()
    assert _value(owned, "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?", selection_id) == (0,)


def test_publish_waits_until_all_sources_determined(prepared_env, tmp_path) -> None:
    """来源未判定：已有成功准备也不发布。"""
    owned, roots, directories, qualification = prepared_env
    _add_waiting_source(owned, qualification.action_id)
    decision = decide_obtain_finish(_facts(owned, qualification.action_id))
    assert decision.phase is ObtainPhase.WAIT_SOURCES
    assert decision.may_publish is False
    assert not tuple(directories.ready.iterdir())


def test_publish_waits_while_preparation_in_progress(prepared_with_failure) -> None:
    owned, roots, directories, qualification = prepared_with_failure
    owned.connection.execute(
        "UPDATE deliveries SET status=2 WHERE id=?",
        (qualification.delivery_id,))
    owned.connection.commit()
    decision = decide_obtain_finish(_facts(owned, qualification.action_id))
    assert decision.phase is ObtainPhase.WAIT_PREPARATION
    assert decision.may_publish is False
    assert not tuple(directories.ready.iterdir())


def test_determined_summary_publishes_successes(prepared_with_failure, tmp_path) -> None:
    """判定完成：真实发布成功项，失败项保留，重判仍可发布。"""
    owned, roots, directories, qualification = prepared_with_failure
    decision = decide_obtain_finish(_facts(owned, qualification.action_id))
    assert decision.phase is ObtainPhase.READY_TO_PUBLISH, decision
    assert (decision.prepared, decision.failed) == (1, 1)

    result = asyncio.run(output_operation(
        owned, publish_delivery, qualification.delivery_id, DeliveryContext,
        repository=OutputsRepository(),
        directories=directories,
        occurred_at=_NOW + 10,
    ))
    assert result.phase is DeliveryPhase.PUBLISHED
    filename, = _value(owned, "SELECT file_name FROM deliveries WHERE id=?", qualification.delivery_id)
    assert (directories.ready / filename).read_bytes() == _CONTENT
    assert len(tuple(directories.ready.iterdir())) == 1
    assert _value(owned, "SELECT status, delivery_id, error_code FROM obtain_items WHERE id=?", qualification.failed_item_id) == (4, None, 1)
    again = decide_obtain_finish(_facts(owned, qualification.action_id))
    assert again.phase is ObtainPhase.READY_TO_PUBLISH
    assert (again.prepared, again.failed) == (1, 1)
    assert _value(
        owned, "SELECT status FROM deliveries WHERE id=?",
        qualification.delivery_id)[0] == 5


# ---- 取回终态汇总事务 ----


def _publish(owned, roots, delivery_id, tmp_path):
    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    ready.mkdir(exist_ok=True)
    processing.mkdir(exist_ok=True)
    result = asyncio.run(output_operation(
        owned, publish_delivery, delivery_id, DeliveryContext,
        repository=OutputsRepository(),
        directories=DeliveryDirectories(
                staging=roots.staging, ready=ready, processing=processing),
        occurred_at=_NOW + 10,
    ))
    assert result.phase is DeliveryPhase.PUBLISHED
    filename, = _value(owned, "SELECT file_name FROM deliveries WHERE id=?", delivery_id)
    assert (ready / filename).read_bytes() == _CONTENT


def test_finish_obtain_succeeds_after_all_published(
        prepared_env, tmp_path) -> None:
    """全部条目成功且已发布：动作成功终态，失败项为零。"""
    owned, roots, directories, qualification = prepared_env
    plan_id, = _value(owned, "SELECT plan_id FROM actions WHERE id=?", qualification.action_id)
    sibling_id, = _value(owned, "SELECT MAX(id)+1 FROM actions")
    _seed_action(owned.connection, sibling_id, plan_id, action_type=7)
    owned.connection.commit()
    plan_status, = _value(owned, "SELECT status FROM plans WHERE id=?", plan_id)
    assert plan_status == 2
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    result = outcome.value
    assert (result.action_status, result.plan_status) == (3, plan_status)
    assert _value(owned, "SELECT status FROM actions WHERE id=?", sibling_id) == (2,)
    assert _value(owned, "SELECT status FROM plans WHERE id=?", plan_id) == (plan_status,)
    assert (result.prepared, result.failed) == (1, 0)
    row = _value(owned, "SELECT status, error_code FROM actions WHERE id=?", qualification.action_id)
    assert row == (3, None)

    recovered = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
    assert recovered.value.disposition is ObtainFinishDisposition.ALREADY
    assert (recovered.value.prepared, recovered.value.failed) == (1, 0)


def test_finish_obtain_recovers_first_result_on_resend(
        prepared_env, tmp_path) -> None:
    owned, roots, directories, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    repository = OutputsRepository()
    key = new_operation_key()
    first = repository.finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20), key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    events_before = _value(owned, "SELECT COUNT(*) FROM history_events")[0]
    again = repository.finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20), key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.disposition is ObtainFinishDisposition.ALREADY
    assert again.value.prepared == first.value.prepared
    assert _value(owned, "SELECT COUNT(*) FROM history_events")[0] == events_before
    later = repository.finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 21), key, owned)
    assert later.kind is DbOutcomeKind.ROLLED_BACK


def test_finish_obtain_partial_failure_fails_action(
        prepared_with_failure, tmp_path) -> None:
    """部分成功：先发布成功项，动作整体置失败并保留成功交付。"""
    owned, roots, directories, qualification = prepared_with_failure
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    result = outcome.value
    assert result.action_status == 4
    assert (result.prepared, result.failed) == (1, 1)
    status, error_code, details = _value(
        owned,
        "SELECT status, error_code, error_details_json FROM actions"
        " WHERE id=?", qualification.action_id)
    assert (status, error_code, details) == (4, 21, "{}")
    assert _value(
        owned, "SELECT status FROM deliveries WHERE id=?",
        qualification.delivery_id)[0] == 5


def test_finish_obtain_all_failed_without_success_files(
        prepared_with_failure) -> None:
    """没有成功文件：动作失败，具体失败项由条目事实表达。"""
    owned, roots, directories, qualification = prepared_with_failure
    owned.connection.execute(
        "UPDATE deliveries SET status=6, error_json=? WHERE id=?",
        ('{"reason": "seed_failure"}', qualification.delivery_id))
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.action_status == 4
    assert (outcome.value.prepared, outcome.value.failed) == (0, 2)


def test_finish_obtain_rejects_unpublished_deliveries(prepared_env) -> None:
    """汇总确定但成功交付尚未发布：不能保存终态。"""
    owned, roots, directories, qualification = prepared_env
    before = tuple(owned.connection.iterdump())
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


def test_finish_obtain_rejects_undecided_summary(prepared_env) -> None:
    """来源判定未固定：汇总未确定，不能保存终态。"""
    owned, roots, directories, qualification = prepared_env
    _add_waiting_source(owned, qualification.action_id)
    before = tuple(owned.connection.iterdump())
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


def test_finish_obtain_rejects_canceled_action(prepared_env) -> None:
    owned, roots, directories, qualification = prepared_env
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=?", (qualification.action_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ObtainFinishDisposition.SKIPPED
    assert (outcome.value.action_status, outcome.value.plan_status,
            outcome.value.prepared, outcome.value.failed) == (2, None, None, None)
    assert tuple(owned.connection.iterdump()) == before


def test_finish_obtain_completes_plan_with_terminal_siblings(
        prepared_env, tmp_path) -> None:
    owned, roots, directories, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    plan_id, = _value(owned, "SELECT plan_id FROM actions WHERE id=?", qualification.action_id)
    first, = _value(owned, "SELECT MAX(id)+1 FROM actions")
    _seed_action(owned.connection, first, plan_id, action_type=7, status=3)
    _seed_action(owned.connection, first + 1, plan_id, action_type=7)
    owned.connection.execute(
        "UPDATE actions SET status=4, error_code=20, error_details_json='{}' WHERE id=?", (first + 1,))
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.plan_status == 3
    assert _value(owned, "SELECT status FROM plans WHERE id=?", plan_id)[0] == 3
