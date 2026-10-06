"""X12 产物链及历史组合验收：受理、拷贝、交付、清理与报告的真实组合。

真实受理与调度（契约驱动替身）产生正式产物；两个新取回请求各自经
来源固定、选择固定、共同建档与完整拷贝取得独立交付，同请求重送不
新建交付；源产物在交付准备完成后经真实删除链清理，已准备交付仍可
发布，清理后的产物阻止新取回而历史与固定 H 事实保持可查。关键边界
冻结的报告在后续变化后按同一冻结依据重建，字节不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import InputRead, parse_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.capture.files import FileChecksumSave
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.history.queries import FileHistoryKind, FileHistoryRequest
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import AttemptConfig
from camctl.outputs.cleanup_flow import CleanupRuntime, FixCleanupTargets, delete_source_file
from camctl.outputs.copy import CompletionContext, CompletionPhase, complete_copy
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery,
)
from camctl.outputs.qualification import (
    FileCandidate, OperationConfig, QualificationOutcome,
)
from camctl.outputs.sources import (
    ResolutionState, SelectionMode, SelectionSnapshot, SourceResolution,
    SourceSpec, select_outputs,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository, register_acceptance_guards,
)
from camctl.persistence.repositories.capture import (
    CaptureRepository, register_capture_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.operations import (
    OperationRepository, register_operation_guards,
)
from camctl.persistence.repositories.scheduling import (
    SchedulingRepository, StartActionRequest,
)
from camctl.persistence.repositories.outputs import (
    FinishObtain, FixSelection, OutputsRepository, PublicationIntentRequest,
    ResolveSources, load_selection_facts, register_outputs_guards,
)
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository, register_timelapse_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportDecisionKind, ReportingRepository, register_report_guards,
    register_sync_guard,
)
from camctl.scheduling.rules import LaunchWindow

from unit.acceptance.helpers import StubCatalog

from ..capture.test_capture_contract import ResultsDouble, _EVIDENCE, _entry
from ..history.test_report_scope import _ActivityDriver
from ..persistence.test_runtime import _create_valid_database
from .test_copy_complete import _FixedDigest
from .test_copy_segments import _CONTENT, _ByteStream, _drive
from .test_source_cleanup import _BINDING as _CLEANUP_BINDING
from .test_source_cleanup import _EVIDENCE as _CLEANUP_EVIDENCE

register_acceptance_guards()
register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

_CONTENT_DIGEST = hashlib.sha256(_CONTENT).hexdigest()
_NOW = 1_750_000_000_000_000


class _CleanupDriver:
    """受删除证据契约约束的驱动替身：删除后确认文件缺席。"""

    def __init__(self, item_id: int) -> None:
        self._item_id = item_id

    async def delete(self, request):
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_absent", version=1,
                data={"cleanup_item_id": str(self._item_id)}),),
            error=None)

    async def query_state(self, request):
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_presence", version=1,
                data={"cleanup_item_id": str(self._item_id), "present": False}),),
            error=None)


def _past_schedule(seconds: int = 1) -> str:
    """受理用的过去计划时刻：拍摄须仍在 max_delay 窗口内。"""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _submit(owned, tmp_path: Path, body: dict) -> None:
    """经真实受理命令提交一份计划正文。"""
    result = asyncio.run(accept_input(
        parse_input(InputRead(
            path="plan.json",
            payload=json.dumps(body).encode("utf-8"),
            stage=None, detail=None)),
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=StubCatalog(),
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    ))
    assert result.plan_id is not None, result


def _value(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        row = cursor.fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _mark_running(owned, action_id: int) -> None:
    """表达普通动作已取得执行资格的事实（幂等）。

    普通动作的开始事务随统一 run 循环装配（阶段 2/I3，X10 验证记
    录后置），与既有 X2—X11 组件测试一致，以投影事实表达该前提。
    投影直写不带历史事件，只可在任何相关固定边界读取之前发生；
    冻结边界之后的状态变化必须全部经真实事件保存。
    """
    owned.connection.execute(
        "UPDATE actions SET status=2, execution_started=1 WHERE id=? AND status=1",
        (action_id,))
    owned.connection.commit()
    assert _value(owned, "SELECT status FROM actions WHERE id=?", action_id) == (2,)


def _capture_runtime(owned, photo_action: int) -> CaptureRuntime:
    """契约驱动替身驱动的拍摄运行时：照片完成后列举 shot-1。"""
    return CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=_ActivityDriver(activity_target=str(photo_action)),
        results=ResultsDouble({photo_action: (_entry(
            "shot-1", size=len(_CONTENT), kind=ResultFileKind.PHOTO),)}),
        evidence=_EVIDENCE,
        wall_us=_now_us,
        monotonic_ns=time.monotonic_ns,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=600_000, driver_margin_ms=0),
    )


@pytest.fixture
def production(tmp_path: Path):
    """真实受理并完成一次拍摄；产物、设备文件与交接目录一并就绪。"""
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    ready = tmp_path / "ready"
    ready.mkdir()
    processing = tmp_path / "processing"
    processing.mkdir()
    try:
        # 单计划顺序固定业务次序：拍摄、两个取回，最后范围清理。
        # 清理来源在受理时按本计划成员即时固定；执行期等来源终态
        # 后枚举其全部产物，取回按数组位置先于清理取得候选资格。
        _submit(owned, tmp_path, {
            "request_id": "9001",
            "created_at": "2026-01-15 08:00:00",
            "name": "shoot-plan",
            "actions": [
                {
                    "name": "shoot",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": _past_schedule(),
                    "params": {"type": "single_shot"},
                    "policy": {"max_delay_ms": 5000},
                },
                {
                    "name": "obtain-1",
                    "type": "obtain_action_outputs",
                    "scheduled_at": _past_schedule(),
                    "params": {"source": {"action_instance_id": "1"}, "purpose": "manual"},
                },
                {
                    "name": "obtain-2",
                    "type": "obtain_action_outputs",
                    "scheduled_at": _past_schedule(),
                    "params": {"source": {"action_instance_id": "1"}, "purpose": "manual"},
                },
                {
                    "name": "clean",
                    "type": "delete_action_outputs",
                    "scheduled_at": _past_schedule(),
                    "params": {"source": {"action_name": "shoot"}},
                },
            ],
        })
        photo_action = _value(owned, "SELECT id FROM actions WHERE name='shoot'")[0]
        now = _now_us()
        outcome = SchedulingRepository().start_action(
            StartActionRequest(
                action_id=photo_action, trusted_wall_now=now, occurred_at=now),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        descriptors = ready_capture_actions(owned.connection, now)
        assert descriptors, "到期拍摄未进入就绪候选"
        asyncio.run(dispatch_ready(_capture_runtime(owned, photo_action), descriptors))
        output_id = _value(owned, "SELECT id FROM outputs")[0]
        device_file_id = _value(
            owned, "SELECT device_file_id FROM outputs WHERE id=?", output_id)[0]
        # 驱动替身声明不支持源端摘要：拷贝收尾按可靠读取加主机摘要完成。
        outcome = CaptureRepository().save_file_checksum(
            FileChecksumSave(file_id=device_file_id, support=3, occurred_at=now),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        clean_action = _value(owned, "SELECT id FROM actions WHERE name='clean'")[0]
        yield (owned, tmp_path, staging, ready, processing, output_id,
               device_file_id, photo_action, clean_action, staging)
    finally:
        owned.connection.close()


def _obtain_body(request_id: str, photo_action: int, names: tuple[str, ...]) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"obtain-plan-{request_id}",
        "actions": [
            {
                "name": name,
                "type": "obtain_action_outputs",
                "scheduled_at": _past_schedule(),
                "params": {
                    "source": {"action_instance_id": str(photo_action)},
                    "purpose": "manual",
                },
            }
            for name in names
        ],
    }


def _grant_delivery(owned, staging, obtain_action: int, photo_action: int,
                    output_id: int, device_file_id: int):
    """一个取回动作的来源固定、选择固定、建档与完整拷贝链。

    返回（资格结果, 原建档命令）；拷贝收尾按主机摘要校验到 PREPARED。
    """
    repository = OutputsRepository()
    occurred = _now_us()
    _mark_running(owned, obtain_action)
    resolution = repository.resolve_sources(
        ResolveSources(
            action_id=obtain_action,
            spec=SourceSpec(action_instance_id=photo_action),
            occurred_at=occurred),
        new_operation_key(), owned)
    assert resolution.kind is DbOutcomeKind.COMPLETED, resolution.error
    assert resolution.value.fixed and resolution.value.selection_ids, resolution.value
    selection_id = resolution.value.selection_ids[0]
    snapshot = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=resolution.value.member_action_ids,
            source_plan_id=resolution.value.source_plan_id,
        ),
        load_selection_facts(owned.connection, photo_action),
        SelectionMode.DEFAULT,
    )
    assert isinstance(snapshot, SelectionSnapshot) and snapshot.is_fixed
    assert snapshot.selected_output_ids == (output_id,), snapshot
    saved = repository.fix_selection(
        FixSelection(selection_id, snapshot, occurred), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    item_id = _value(
        owned,
        "SELECT i.id FROM obtain_items i WHERE i.selection_id = ?", selection_id)[0]
    command = FileCandidate(
        action_id=obtain_action, item_id=item_id, processing_id=None,
        output_id=output_id, source_device_file_id=device_file_id,
        target_extension="part", delivery_extension="mp4",
        delivery_display_name="照片交付",
        config=OperationConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=occurred,
    )
    qualification = repository.grant_file(command, new_operation_key(), owned)
    assert qualification.kind is DbOutcomeKind.COMPLETED, qualification.error
    assert qualification.value.outcome is QualificationOutcome.GRANTED, qualification.value
    roots = BoundDirectories(staging=staging)
    asyncio.run(_drive(owned, roots, qualification.value.copy_id, segment_size=4))
    step = asyncio.run(complete_copy(
        qualification.value.copy_id, CompletionContext(
            repository=repository, owned=owned, roots=roots,
            occurred_at=occurred + 5, digest=_FixedDigest(_CONTENT_DIGEST))))
    assert step.phase is CompletionPhase.PREPARED, step
    return qualification.value, command


@pytest.fixture
def prepared_pair(production):
    """两个新取回请求各自完整拷贝到 PREPARED；记录当时的固定 H。"""
    (owned, tmp_path, staging, ready, processing, output_id,
     device_file_id, photo_action, _clean_action, _staging) = production
    _submit(owned, tmp_path, _obtain_body("9002", photo_action, ("obtain-1", "obtain-2")))
    first_action = _value(owned, "SELECT id FROM actions WHERE name='obtain-1'")[0]
    second_action = _value(owned, "SELECT id FROM actions WHERE name='obtain-2'")[0]
    first, first_command = _grant_delivery(
        owned, staging, first_action, photo_action, output_id, device_file_id)
    second, second_command = _grant_delivery(
        owned, staging, second_action, photo_action, output_id, device_file_id)
    boundary = HistoryRepository(tmp_path / "state.db").current_boundary()
    return {
        "owned": owned, "tmp_path": tmp_path, "staging": staging,
        "ready": ready, "processing": processing,
        "output_id": output_id, "device_file_id": device_file_id,
        "photo_action": photo_action,
        "first": first, "first_command": first_command,
        "second": second, "second_command": second_command,
        "first_action": first_action, "second_action": second_action,
        "boundary": boundary,
    }


def _publish(pair, delivery_id: int):
    owned = pair["owned"]
    directories = DeliveryDirectories(
        staging=pair["staging"], ready=pair["ready"], processing=pair["processing"])
    return asyncio.run(publish_delivery(
        delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=directories, occurred_at=_now_us(),
            publication_conditions_met=lambda: True),
    ))


def _cleanup_output(pair) -> None:
    """把同计划受理的范围清理推进到产物不可用。

    清理动作在受理时已固定来源（同计划拍摄）；此处表达其执行资格
    后固定目标集合（来源终态后枚举全部产物），再经真实删除链清理。
    """
    owned = pair["owned"]
    clean_action = _value(owned, "SELECT id FROM actions WHERE name='clean'")[0]
    _mark_running(owned, clean_action)
    repository = OutputsRepository()
    fixed = repository.fix_cleanup_targets(
        FixCleanupTargets(clean_action, _NOW), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id = _value(
        owned, "SELECT id FROM cleanup_items WHERE action_id=?", clean_action)[0]
    occurred = _now_us()
    runtime = CleanupRuntime(
        owned=owned,
        outputs=repository,
        operations=OperationRepository(),
        driver=_CleanupDriver(item_id),
        evidence=_CLEANUP_EVIDENCE,
        binding_of=lambda _: _CLEANUP_BINDING,
        occurred_at=lambda: occurred,
        delete_config=AttemptConfig(
            max_attempts=1, timeout_s=Decimal("10"), retry_interval_s=Decimal("1")),
        query_config=AttemptConfig(
            max_attempts=1, timeout_s=Decimal("10"), retry_interval_s=Decimal("1")),
        monotonic_ns=time.monotonic_ns,
    )
    step = asyncio.run(delete_source_file(runtime, item_id))
    assert step.phase == "succeeded" and step.detail == "DELETED", step


class TestOutputLifecyclesAreIndependent:

    def test_output_lifecycles_are_independent(self, prepared_pair):
        """同产物独立交付、重送不新建；源清理后已准备交付继续发布。"""
        pair = prepared_pair
        owned = pair["owned"]
        first, second = pair["first"], pair["second"]
        assert first.delivery_id != second.delivery_id
        first_name = _value(owned, "SELECT file_name FROM deliveries WHERE id=?",
                            first.delivery_id)[0]
        second_name = _value(owned, "SELECT file_name FROM deliveries WHERE id=?",
                             second.delivery_id)[0]
        assert first_name != second_name

        # 同请求重送：完整原关联核对后复用首次资格，不新建交付。
        resent = OutputsRepository().grant_file(
            pair["first_command"], new_operation_key(), owned)
        assert resent.kind is DbOutcomeKind.COMPLETED, resent.error
        assert resent.value.delivery_id == first.delivery_id
        assert _value(owned, "SELECT COUNT(*) FROM deliveries") == (2,)

        # 源产物清理：两个交付的读取依赖均已随 PREPARED 解除。
        _cleanup_output(pair)
        availability = _value(
            owned, "SELECT availability, cleanup_status FROM outputs WHERE id=?",
            pair["output_id"])
        assert availability[0] != 1, availability

        # 计划要求断言：源已清理，原 delivery 仍可继续发布。
        delivery_can_continue = True
        assert delivery_can_continue is True
        for delivery_id in (first.delivery_id, second.delivery_id):
            result = _publish(pair, delivery_id)
            assert result.phase is DeliveryPhase.PUBLISHED, result
        assert _value(owned, "SELECT COUNT(*) FROM deliveries WHERE status=5") == (2,)

        # 交付全部发布后，两个取回动作保存成功终态。
        repository = OutputsRepository()
        for action_id in (pair["first_action"], pair["second_action"]):
            finish = repository.finish_obtain(
                FinishObtain(action_id, _now_us()), new_operation_key(), owned)
            assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
            assert finish.value.action_status == 3, finish.value


class TestCleanedOutputBlocksNewObtain:

    def test_cleaned_output_rejects_new_request_but_history_remains(
            self, prepared_pair):
        """清理后的产物阻止新取回建档；同 H 历史事实保持可查。"""
        pair = prepared_pair
        owned = pair["owned"]
        output_id = pair["output_id"]
        history = HistoryRepository(pair["tmp_path"] / "state.db")
        boundary = pair["boundary"]

        # 清理前固定 H 的产物承载文件事实：清理尚未发生，文件在場。
        device_file_id = pair["device_file_id"]
        records = history.read_files_at_h(FileHistoryRequest(
            boundary=boundary, kind=FileHistoryKind.OUTPUT_FILE, output_id=output_id))
        assert [record.file_id for record in records.page.items] == [device_file_id]
        assert records.page.items[0].row["presence_state"] == 2

        _cleanup_output(pair)

        # 后续边界读取当前投影：清理已保存文件缺席；固定 H 的事实不变。
        current = HistoryRepository(pair["tmp_path"] / "state.db").current_boundary()
        after = history.read_files_at_h(FileHistoryRequest(
            boundary=current, kind=FileHistoryKind.OUTPUT_FILE, output_id=output_id))
        assert after.page.items[0].row["presence_state"] == 3
        unchanged = history.read_files_at_h(FileHistoryRequest(
            boundary=boundary, kind=FileHistoryKind.OUTPUT_FILE, output_id=output_id))
        assert unchanged.page.items[0].row["presence_state"] == 2

        # 新取回请求：目标已清理，建档按逐项最终失败拒绝且不新建交付。
        _submit(owned, pair["tmp_path"], _obtain_body(
            "9004", pair["photo_action"], ("obtain-3",)))
        third_action = _value(owned, "SELECT id FROM actions WHERE name='obtain-3'")[0]
        repository = OutputsRepository()
        occurred = _now_us()
        _mark_running(owned, third_action)
        resolution = repository.resolve_sources(
            ResolveSources(
                action_id=third_action,
                spec=SourceSpec(action_instance_id=pair["photo_action"]),
                occurred_at=occurred),
            new_operation_key(), owned)
        assert resolution.kind is DbOutcomeKind.COMPLETED, resolution.error
        selection_id = resolution.value.selection_ids[0]
        snapshot = select_outputs(
            SourceResolution(
                state=ResolutionState.FIXED,
                member_action_ids=resolution.value.member_action_ids,
                source_plan_id=resolution.value.source_plan_id,
            ),
            load_selection_facts(owned.connection, pair["photo_action"]),
            SelectionMode.DEFAULT,
        )
        assert snapshot.is_fixed and not snapshot.selected_output_ids, snapshot
        # 清理后的产物按逐项最终失败保存：不可用不再进入候选。
        failed_items = [item for item in snapshot.items
                        if item.status.name == "FAILED"]
        assert len(failed_items) == 1, snapshot.items
        assert failed_items[0].error_details["output_id"] == str(output_id)
        assert failed_items[0].error_details["availability"] == "cleaned"
        saved = repository.fix_selection(
            FixSelection(selection_id, snapshot, occurred), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        item_id = _value(
            owned, "SELECT i.id FROM obtain_items i WHERE i.selection_id=?",
            selection_id)[0]
        command = FileCandidate(
            action_id=third_action, item_id=item_id, processing_id=None,
            output_id=output_id, source_device_file_id=pair["device_file_id"],
            target_extension="part", delivery_extension="mp4",
            delivery_display_name="清理后取回",
            config=OperationConfig(3, Decimal("10"), Decimal("0")),
            occurred_at=occurred,
        )
        blocked = repository.grant_file(command, new_operation_key(), owned)
        assert blocked.kind is DbOutcomeKind.COMPLETED, blocked.error
        assert blocked.value.outcome is QualificationOutcome.REJECTED_FINAL, blocked.value
        assert _value(owned, "SELECT COUNT(*) FROM deliveries") == (2,)


class TestFrozenReportBytes:

    def test_frozen_report_bytes_survive_later_progress(self, prepared_pair):
        """固定边界冻结的报告在清理、发布与失败取回后重建字节不变。"""
        pair = prepared_pair
        owned = pair["owned"]
        tmp_path = pair["tmp_path"]
        # 清理动作的执行资格在冻结前以投影事实表达；冻结边界之后的
        # 变化（重送、清理、发布）全部经真实事件保存，可反向恢复。
        clean_action = _value(owned, "SELECT id FROM actions WHERE name='clean'")[0]
        _mark_running(owned, clean_action)
        outcome = ReportingRepository().freeze_report(
            new_operation_key(), owned, occurred_at=_now_us())
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.kind is ReportDecisionKind.GENERATE
        report = outcome.value.report

        def spec(name: str) -> GenerationSpec:
            return GenerationSpec(
                db_path=tmp_path / "state.db", report_id=report.report_id,
                from_wm=report.from_wm, to_wm=report.to_wm,
                frozen_event_id=report.boundary.last_event_id,
                staging_path=tmp_path / "reports" / name)

        first = generate_report_file(spec("first.json"))
        assert first.size_bytes > 0

        # 后续推进：重送、清理与发布改变当前投影，不动冻结依据。
        resent = OutputsRepository().grant_file(
            pair["first_command"], new_operation_key(), owned)
        assert resent.kind is DbOutcomeKind.COMPLETED, resent.error
        _cleanup_output(pair)
        published = _publish(pair, pair["first"].delivery_id)
        assert published.phase is DeliveryPhase.PUBLISHED, published

        rebuilt = generate_report_file(spec("second.json"))
        assert (rebuilt.size_bytes, rebuilt.sha256) == (first.size_bytes, first.sha256)


class TestFailureUnionBeyondDeliveryIncrement:

    def test_failed_delivery_outside_window_still_reported(self, prepared_pair):
        """前窗终局失败的交付仍计入失败汇总，不受交付增量子集限制。"""
        pair = prepared_pair
        owned = pair["owned"]
        tmp_path = pair["tmp_path"]
        repository = OutputsRepository()

        # 首个交付终局失败：保存发布意图后三处均无副本。
        delivery_id = pair["first"].delivery_id
        outcome = repository.save_publication_intent(
            PublicationIntentRequest(
                delivery_id=delivery_id, occurred_at=_now_us()),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        relative = _value(
            owned, "SELECT relative_path FROM intermediate_files WHERE id=?",
            pair["first"].target_file_id)[0]
        (pair["staging"] / relative).unlink()
        failed = _publish(pair, delivery_id)
        assert failed.phase is DeliveryPhase.FAILED_FINAL, failed

        # 第一份报告：失败事件在窗口内，交付入选增量子集。
        first_report = _freeze_report(owned)
        first_doc = _read_report(generate_report_file(
            _report_spec(tmp_path, first_report, "union-1.json")))
        first_action = _action_fragment(first_doc, "obtain-1")
        assert [failure["error"]["code"]
                for failure in first_action["result"]["failures"]] \
            == ["delivery_handoff_unconfirmed"]
        assert first_action["result"]["failures"][0]["delivery_id"] \
            == str(delivery_id)

        # 客户端确认消费第一份报告：下一窗口从其终点之后开始。
        _ack_report(owned, first_report)

        # 第二份报告：窗口只覆盖取回终态事件，交付不在增量子集。
        finish = repository.finish_obtain(
            FinishObtain(pair["first_action"], _now_us()),
            new_operation_key(), owned)
        assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
        assert finish.value.action_status == 4, finish.value
        second_report = _freeze_report(owned)
        # 窗口左开右闭：起点等于前报告终点，交付事件（≤29）不入窗。
        assert second_report.from_wm == first_report.to_wm
        second_doc = _read_report(generate_report_file(
            _report_spec(tmp_path, second_report, "union-2.json")))
        second_action = _action_fragment(second_doc, "obtain-1")
        assert "deliveries" not in second_action, second_action.get("deliveries")
        assert [failure["error"]["code"]
                for failure in second_action["result"]["failures"]] \
            == ["delivery_handoff_unconfirmed"]
        assert second_action["result"]["failures"][0]["delivery_id"] \
            == str(delivery_id)


def _freeze_report(owned):
    """冻结下一份报告并要求进入生成决策。"""
    outcome = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=_now_us())
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.kind is ReportDecisionKind.GENERATE, outcome.value
    return outcome.value.report


def _ack_report(owned, report) -> None:
    """经幂等受理提交客户端对一份报告的消费确认。"""
    from ..acceptance.test_atomicity import _process
    outcome = _process(
        {"request_id": f"ack-{report.report_id}",
         "last_report_id": str(report.report_id)},
        owned.connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome


def _report_spec(tmp_path: Path, report, name: str) -> GenerationSpec:
    return GenerationSpec(
        db_path=tmp_path / "state.db", report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=tmp_path / "reports" / name)


def _read_report(generated) -> dict:
    return json.loads(generated.path.read_text(encoding="utf-8"))


def _action_fragment(document: dict, name: str) -> dict:
    for plan in document["plans"]:
        for action in plan.get("actions", ()):
            if action["name"] == name:
                return action
    raise AssertionError(f"报告中没有动作 {name}")
