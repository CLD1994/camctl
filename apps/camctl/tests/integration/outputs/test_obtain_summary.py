"""X10 取回统一发布汇总的组件集成测试。

真实 SQLite 与真实选择、准备及发布事务组合：从已保存的选择、条
目与交付行映射汇总事实，按开始发布的条件判定等待与发布分区；
判定通过后经真实发布入口逐文件发布成功项并保留失败结果。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest_asyncio

from ..support.outputs import output_operation
from ..support.plan_completion import assert_action_plan, plan_before
from ..support.public_delivery_cases import prepare_public_delivery

from camctl.contracts.values import new_operation_key
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
from camctl.persistence.repositories.outputs import (
    FinishObtain,
    ObtainFinishDisposition,
    OutputsRepository, load_obtain_facts,
)
from camctl.persistence.repositories.transactions import TransactionError

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


def _append_storage_action(owned, action_id, plan_id, *, action_type, device_id=None, status=2):
    """按已有计划的下一个输入顺序准备仓储事务的动作投影。"""
    input_index, = _value(owned,
        "SELECT COALESCE(MAX(input_index),-1)+1 FROM actions WHERE plan_id=?", plan_id)
    _seed_action(owned.connection, action_id, plan_id, action_type=action_type,
                 device_id=device_id, status=status)
    owned.connection.execute("UPDATE actions SET input_index=? WHERE id=?", (input_index, action_id))


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
    owned, roots, directories, qualification = await prepare_public_delivery(
        tmp_path, content=_CONTENT, now=_NOW, missing_output_id=705)
    try:
        assert owned.connection.execute("SELECT 1 FROM outputs WHERE id=705").fetchone() is None
        assert _value(owned, "SELECT status,delivery_id,error_code FROM obtain_items"
            " WHERE requested_output_id=705") == (4, None, 1)
        yield owned, roots, directories, qualification
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
    _append_storage_action(owned, source_id, source_plan, action_type=1, device_id="cam-2")
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=(SELECT input_fields_json FROM actions WHERE id=?),"
        " effective_params_json=(SELECT effective_params_json FROM actions WHERE id=?),"
        " execution_spec_json=(SELECT execution_spec_json FROM actions WHERE id=?) WHERE id=?",
        (existing_source, existing_source, existing_source, source_id))
    owned.connection.execute("UPDATE plans SET status=2 WHERE id=?", (source_plan,))
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
    assert _value(owned, "SELECT status FROM plans WHERE id=?", source_plan) == (2,)
    assert _value(owned, "SELECT device_id FROM actions WHERE id=?", source_id) == ("cam-2",)
    raw_source, = _value(owned, "SELECT input_fields_json FROM actions WHERE id=?", source_id)
    assert not {"name", "type", "device_id", "group"} & json.loads(raw_source).keys()
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
    assert _value(owned, "SELECT status, delivery_id, error_code FROM obtain_items WHERE requested_output_id=705") == (4, None, 1)
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
    _append_storage_action(owned, sibling_id, plan_id, action_type=7)
    owned.connection.commit()
    plan_status, = _value(owned, "SELECT status FROM plans WHERE id=?", plan_id)
    assert plan_status == 2
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    before_plan = plan_before(owned.connection, plan_id)
    key = new_operation_key()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert_action_plan(owned.connection, qualification.action_id, plan_id, key,
                       action_status=3, previous_plan=before_plan, completed=False)
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
    _append_storage_action(owned, first, plan_id, action_type=7, status=3)
    _append_storage_action(owned, first + 1, plan_id, action_type=7)
    owned.connection.execute(
        "UPDATE actions SET status=4, error_code=20, error_details_json='{}' WHERE id=?", (first + 1,))
    owned.connection.commit()
    before_plan = plan_before(owned.connection, plan_id)
    key = new_operation_key()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=qualification.action_id, occurred_at=_NOW + 20),
        key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.plan_status == 3
    assert_action_plan(owned.connection, qualification.action_id, plan_id, key,
                       action_status=3, previous_plan=before_plan, completed=True)
    assert _value(owned, "SELECT status FROM plans WHERE id=?", plan_id)[0] == 3
