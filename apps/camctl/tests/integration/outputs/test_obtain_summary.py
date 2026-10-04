"""X10 取回统一发布汇总的组件集成测试。

真实 SQLite 与真实选择、准备及发布事务组合：从已保存的选择、条
目与交付行映射汇总事实，按开始发布的条件判定等待与发布分区；
判定通过后经真实发布入口逐文件发布成功项并保留失败结果。
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.copy import CompletionContext, CompletionPhase, complete_copy
from camctl.outputs.handoff import (
    DeliveryContext,
    DeliveryDirectories,
    DeliveryPhase,
    publish_delivery,
)
from camctl.outputs.obtain_summary import (
    ObtainFacts,
    ObtainItemStage,
    ObtainPhase,
    ObtainSourceFacts,
    decide_obtain_finish,
    obtain_item_stage,
)
from camctl.host_files.models import BoundDirectories
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import (
    FinishObtain,
    ObtainFinishDisposition,
    OutputsRepository,
)
from camctl.persistence.transaction import TransactionError
from camctl.outputs.qualification import OperationConfig

from .test_copy_resume import _command, _qualified
from .test_copy_segments import _CONTENT, _drive
from .test_local_read import local_read  # noqa: F401  主机源建档夹具
from .test_qualification import _NOW
from .test_read_associations import read_targets  # noqa: F401

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


def _facts(owned, action_id: int) -> ObtainFacts:
    """从已保存行装配汇总事实：选择固定性、待判定与逐项阶段。

    该读取规则是调度接线前的参考装载实现：选择按依赖归属动作，
    条目按选择，交付按条目保存的交付身份。
    """
    with closing(owned.connection.execute(
        "SELECT s.id, s.status FROM obtain_source_selections s"
        " JOIN action_dependencies d ON d.id = s.dependency_id"
        " WHERE d.action_id=? ORDER BY s.id", (action_id,),
    )) as cursor:
        selections = cursor.fetchall()
    sources = []
    items: list[ObtainItemStage] = []
    for selection_id, status in selections:
        unresolved = _value(
            owned,
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?"
            " AND status=1", selection_id)[0]
        sources.append(ObtainSourceFacts(
            selection_fixed=status == 2, unresolved_items=unresolved))
        with closing(owned.connection.execute(
            "SELECT status, delivery_id FROM obtain_items"
            " WHERE selection_id=? AND status<>1 ORDER BY id", (selection_id,),
        )) as cursor:
            rows = cursor.fetchall()
        for item_status, delivery_id in rows:
            delivery = None
            if delivery_id is not None:
                delivery = _value(
                    owned, "SELECT status FROM deliveries WHERE id=?",
                    delivery_id)[0]
            items.append(obtain_item_stage(item_status, delivery))
    return ObtainFacts(sources=tuple(sources), items=tuple(items))


@pytest.fixture
def prepared_env(read_targets, tmp_path):
    """取回动作 31：一项真实准备完成，另一项已最终失败。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute(
        "UPDATE device_files SET size_bytes=10, checksum_support=2, sha256=?"
        " WHERE id=501",
        (hashlib.sha256(_CONTENT).hexdigest(),))
    owned.connection.commit()
    qualification = _qualified(owned, _command())
    roots = _roots(tmp_path)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    step = asyncio.run(complete_copy(qualification.copy_id, CompletionContext(
        repository=OutputsRepository(), owned=owned, roots=roots,
        occurred_at=_NOW + 5)))
    assert step.phase is CompletionPhase.PREPARED
    owned.connection.commit()
    return owned, roots, qualification


def _add_failed_item(owned) -> None:
    """种下一个显式 ID 未找到的最终失败条目（output_not_found）。"""
    owned.connection.execute(
        "INSERT INTO obtain_items (id, selection_id, requested_output_id,"
        " output_id, basis, original_output_id, preview_output_id,"
        " preview_size, repaired_size, status, source_dependency,"
        " delivery_id, error_code, error_details_json)"
        " VALUES (112, 31, 705, NULL, 5, NULL, NULL, NULL, NULL, 4, 0,"
        ' NULL, 1, \'{"requested_output_id": "705"}\')',
    )
    owned.connection.commit()


def test_publish_waits_until_all_sources_determined(prepared_env, tmp_path) -> None:
    """来源未判定：已有成功准备也不发布。"""
    owned, roots, qualification = prepared_env
    owned.connection.execute(
        "UPDATE obtain_source_selections SET status=1 WHERE id=31")
    owned.connection.commit()
    decision = decide_obtain_finish(_facts(owned, 31))
    assert decision.phase is ObtainPhase.WAIT_SOURCES
    assert decision.may_publish is False


def test_publish_waits_while_preparation_in_progress(prepared_env) -> None:
    owned, roots, qualification = prepared_env
    _add_failed_item(owned)
    owned.connection.execute(
        "UPDATE deliveries SET status=2 WHERE id=?",
        (qualification.delivery_id,))
    owned.connection.commit()
    decision = decide_obtain_finish(_facts(owned, 31))
    assert decision.phase is ObtainPhase.WAIT_PREPARATION


def test_determined_summary_publishes_successes(prepared_env, tmp_path) -> None:
    """判定完成：真实发布成功项，失败项保留，重判仍可发布。"""
    owned, roots, qualification = prepared_env
    _add_failed_item(owned)
    decision = decide_obtain_finish(_facts(owned, 31))
    assert decision.phase is ObtainPhase.READY_TO_PUBLISH, decision
    assert (decision.prepared, decision.failed) == (1, 1)

    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    ready.mkdir()
    processing.mkdir()
    result = asyncio.run(publish_delivery(
        qualification.delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=DeliveryDirectories(
                staging=roots.staging, ready=ready, processing=processing),
            occurred_at=_NOW + 10)))
    assert result.phase is DeliveryPhase.PUBLISHED
    again = decide_obtain_finish(_facts(owned, 31))
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
    result = asyncio.run(publish_delivery(
        delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=DeliveryDirectories(
                staging=roots.staging, ready=ready, processing=processing),
            occurred_at=_NOW + 10)))
    assert result.phase is DeliveryPhase.PUBLISHED


def test_finish_obtain_succeeds_after_all_published(
        prepared_env, tmp_path) -> None:
    """全部条目成功且已发布：动作成功终态，失败项为零。"""
    owned, roots, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    result = outcome.value
    assert (result.action_status, result.plan_status) == (3, 1)
    assert (result.prepared, result.failed) == (1, 0)
    row = _value(owned, "SELECT status, error_code FROM actions WHERE id=31")
    assert row == (3, None)

    recovered = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
    assert recovered.value.disposition is ObtainFinishDisposition.ALREADY
    assert (recovered.value.prepared, recovered.value.failed) == (1, 0)


def test_finish_obtain_recovers_first_result_on_resend(
        prepared_env, tmp_path) -> None:
    owned, roots, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    repository = OutputsRepository()
    key = new_operation_key()
    first = repository.finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20), key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    events_before = _value(owned, "SELECT COUNT(*) FROM history_events")[0]
    again = repository.finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20), key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.disposition is ObtainFinishDisposition.ALREADY
    assert again.value.prepared == first.value.prepared
    assert _value(owned, "SELECT COUNT(*) FROM history_events")[0] == events_before
    later = repository.finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 21), key, owned)
    assert later.kind is DbOutcomeKind.ROLLED_BACK


def test_finish_obtain_partial_failure_fails_action(
        prepared_env, tmp_path) -> None:
    """部分成功：先发布成功项，动作整体置失败并保留成功交付。"""
    owned, roots, qualification = prepared_env
    _add_failed_item(owned)
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    result = outcome.value
    assert result.action_status == 4
    assert (result.prepared, result.failed) == (1, 1)
    status, error_code, details = _value(
        owned,
        "SELECT status, error_code, error_details_json FROM actions"
        " WHERE id=31")
    assert (status, error_code, details) == (4, 21, "{}")
    assert _value(
        owned, "SELECT status FROM deliveries WHERE id=?",
        qualification.delivery_id)[0] == 5


def test_finish_obtain_all_failed_without_success_files(
        prepared_env) -> None:
    """没有成功文件：动作失败，具体失败项由条目事实表达。"""
    owned, roots, qualification = prepared_env
    _add_failed_item(owned)
    owned.connection.execute(
        "UPDATE deliveries SET status=6, error_json=? WHERE id=?",
        ('{"reason": "seed_failure"}', qualification.delivery_id))
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.action_status == 4
    assert (outcome.value.prepared, outcome.value.failed) == (0, 2)


def test_finish_obtain_rejects_unpublished_deliveries(prepared_env) -> None:
    """汇总确定但成功交付尚未发布：不能保存终态。"""
    owned, roots, qualification = prepared_env
    before = tuple(owned.connection.iterdump())
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


def test_finish_obtain_rejects_undecided_summary(prepared_env) -> None:
    """来源判定未固定：汇总未确定，不能保存终态。"""
    owned, roots, qualification = prepared_env
    owned.connection.execute(
        "UPDATE obtain_source_selections SET status=1 WHERE id=31")
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


def test_finish_obtain_rejects_canceled_action(prepared_env) -> None:
    owned, roots, qualification = prepared_env
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


def test_finish_obtain_completes_plan_with_terminal_siblings(
        prepared_env, tmp_path) -> None:
    owned, roots, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.execute(
        "UPDATE actions SET status=4, error_code=20, error_details_json='{}'"
        " WHERE id=32")
    owned.connection.commit()
    outcome = OutputsRepository().finish_obtain(
        FinishObtain(action_id=31, occurred_at=_NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.plan_status == 3
    assert _value(owned, "SELECT status FROM plans WHERE id=1")[0] == 3
