"""关联文件同边界历史查询的组件集成测试。

真实 SQLite 与事务内核组合：候选扫描绑定固定上界与创建边界，逐
候选恢复到 H 后筛选；继续位置越过已检查候选，不取最后有效结果。
H 后的归属补齐、在场变化、成品字节固定与新建文件不改变 H 集合；
引用类查询先恢复引用方再按当时引用取文件。被恢复的行一律经生产
命令创建，raw 种子只承担不被恢复的所属对象与处理、拷贝子行。主
对象在 H 不存在与存在但集合为空分别表达；读取失败与矛盾行不冒
充空页或读完。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

import pytest

from camctl.capture.files import (
    FileCompletionSave,
    FileObservationSave,
    FilePresenceSave,
    OwnershipSave,
)
from camctl.capture.processing import (
    CheckPhase,
    CheckResultSave,
    MediaObservation,
    RepairBasis,
    RepairDecisionChoice,
    RepairDecisionSave,
    RepairReason,
    RepairStart,
    RepairSuccess,
)
from camctl.contracts.history_values import BoundaryError, HistoryBoundary
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.queries import FileHistoryKind, FileHistoryRequest
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCapture,
    register_capture_guards,
)
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..outputs.test_read_associations import read_targets
from ..persistence.test_runtime import _create_valid_database

register_operation_guards()
register_capture_guards()
register_outputs_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_SEED_BOUNDARY = HistoryBoundary(txn_id=1, last_event_id=1)


def _environment(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    _seed_plan(connection, 1)
    _seed_action(connection, 11, action_type=2)
    _seed_action(connection, 12, action_type=1)
    connection.commit()
    return owned, target


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, 4241 + plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, *, action_type: int) -> None:
    params = {"type": "ordinary"}
    if action_type == 2:
        params["duration_s"] = 60
    original = {"params": params, "policy": {"max_delay_ms": 1000}}
    definition = {"target_duration_ms": 60000} if action_type == 2 else {}
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, ?, 'cam-1', ?, NULL, ?, ?, 'camctl-adb',"
        " 1000, ?, 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"act-{action_id}", action_type, _NOW,
         json.dumps(original), json.dumps(params), json.dumps(definition)),
    )


def _seed_obtain_action(connection, action_id: int, plan_id: int) -> None:
    """历史读取用的取回前置种子，固定原输入与 DEFAULT 选择一致。"""
    from ..outputs.test_qualification import _seed_action as seed_qualified_action

    seed_qualified_action(connection, action_id, plan_id, action_type=4)
    connection.execute("UPDATE actions SET input_fields_json = ? WHERE id = ?",
        (json.dumps({"params": {"source": {"current_plan": True}, "purpose": "manual"}}),
         action_id))


def _seed_processing(connection, processing_id: int, action_id: int,
                     source_file: int | None) -> None:
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (?, ?, ?, 1, 3, ?, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (processing_id, action_id, source_file,
         '{"reason": 2, "target_duration_ms": 60000}'),
    )


def _seed_file_copies(connection, copy_id: int, processing_id: int,
                      source_device_file: int, target_file: int) -> None:
    connection.execute(
        "INSERT INTO file_copies (id, delivery_id, processing_id,"
        " source_device_file_id, source_intermediate_file_id, target_file_id,"
        " round, recopies_used, max_recopies_used, source_size,"
        " committed_bytes, reset_state, verification_state)"
        " VALUES (?, NULL, ?, ?, NULL, ?, 1, 0, 0, 250, 0, 1, 1)",
        (copy_id, processing_id, source_device_file, target_file),
    )


class _StubSource:
    """受理输入源替身：按内存正文应答读取。"""

    def __init__(self, body: dict) -> None:
        self._body = body

    def read(self, path: str) -> bytes:
        return json.dumps(self._body).encode("utf-8")


async def _submit_plan(owned) -> int:
    """经受理命令建立生产计划与单张动作，返回动作身份。"""
    from camctl.acceptance.input import parse_input, read_input
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from unit.acceptance.helpers import StubCatalog
    from camctl.persistence.repositories.acceptance import (
        AcceptanceRepository,
        register_acceptance_guards,
    )

    register_acceptance_guards()
    body = {
        "request_id": "9001",
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }
    source = parse_input(await read_input("/tmp/plan.json", _StubSource(body)))
    accepted = await accept_input(
        source,
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=StubCatalog(),
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    )
    assert accepted.plan_id is not None
    with closing(owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?", (accepted.plan_id,))) as cursor:
        return int(cursor.fetchone()[0])


def _observe(capture: CaptureRepository, owned, observer: int,
             identity: str) -> int:
    """经生产命令登记一个设备文件并返回文件身份。"""
    outcome = capture.save_file_observation(
        FileObservationSave(
            observer_action_id=observer,
            file_identity=identity,
            locator={"path": f"/DCIM/{identity}"},
            occurred_at=_NOW,
            original_name=identity,
            media_type="video/mp4",
        ),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.file_id


def _confirm(capture: CaptureRepository, owned, file_id: int, source: int) -> None:
    outcome = capture.save_file_ownership(
        OwnershipSave(
            file_id=file_id, source_action_id=source, method=1, role=2,
            observation={"task": "a"}, occurred_at=_NOW,
        ),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _presence(capture: CaptureRepository, owned, file_id: int, state: int) -> None:
    outcome = capture.save_file_presence(
        FilePresenceSave(file_id=file_id, state=state, occurred_at=_NOW),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _complete(capture: CaptureRepository, owned, file_id: int) -> None:
    """经生产命令按设备保证确认文件写完并固定完整大小。"""
    outcome = capture.save_file_completion(
        FileCompletionSave(
            file_id=file_id, state=3, basis=1,
            observation={"listed": True}, occurred_at=_NOW, size_bytes=250),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _finish_output(capture: CaptureRepository, owned, action_id: int,
                   device_file_id: int) -> None:
    """确认归属与完成后经生产完成登记为动作登记一个原片产物。

    产物登记校验承载文件已有该来源动作的可靠归属且已确认写完，
    先经所有权确认与完成保存从未知补齐。
    """
    _confirm(capture, owned, device_file_id, action_id)
    _complete(capture, owned, device_file_id)
    outcome = capture.finish_capture(
        FinishCapture(
            action_id=action_id,
            drafts=(
                OutputDraft(
                    kind=OutputKind.ORIGINAL,
                    file=FileReference(device_file_id=device_file_id),
                    file_complete=True,
                    sha256=None,
                ),
            ),
            catalog_facts=OutputCatalogFacts(
                action_id=action_id, ownership_confirmed=True),
            occurred_at=_NOW,
        ),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _seed_repair_pending(capture: CaptureRepository, owned) -> None:
    """把种子处理行推进到修复待执行（生产命令链）。"""
    saves = (
        (capture.save_check_result, CheckResultSave(
            1, MediaObservation(CheckPhase.RUNNING), occurred_at=_NOW + 1)),
        (capture.save_check_result, CheckResultSave(
            1, MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")),
            occurred_at=_NOW + 2)),
        (capture.save_repair_decision, RepairDecisionSave(
            1, RepairDecisionChoice.PENDING,
            basis=RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=60_000,
                threshold_s=Decimal("70"),
                actual_duration_s=Decimal("75.125")),
            occurred_at=_NOW + 3)),
    )
    for save, command in saves:
        outcome = save(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _start_repair(capture: CaptureRepository, owned) -> int:
    """登记修复输出文件并返回文件身份。"""
    outcome = capture.start_repair_output(
        RepairStart(1, "mp4", occurred_at=_NOW + 4),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.file_id


def _finish_repair(capture: CaptureRepository, owned, file_id: int) -> None:
    outcome = capture.complete_repair_output(
        RepairSuccess(
            1, file_id, 8, "b" * 64, occurred_at=_NOW + 5),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _collect(repository: HistoryRepository, request: FileHistoryRequest):
    """按继续位置读完一次查询的全部页面，返回（记录, 主对象存在）。"""
    result = repository.read_files_at_h(request)
    records = list(result.page.items)
    pages = 1
    while not result.page.exhausted:
        assert pages < 20, "继续位置未推进，扫描没有结束"
        result = repository.read_files_at_h(
            replace(request, cursor=result.page.next_cursor))
        records.extend(result.page.items)
        pages += 1
    return records, result.owner_present


# ---- 候选扫描：同边界集合与继续位置 ----


async def test_old_files_use_same_h(tmp_path: Path) -> None:
    """H 后归属补齐、在场转缺席与修复成品字节固定不改写 H 的集合。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    file_id = _observe(capture, owned, 11, "task-a/original.mp4")
    old = history.current_boundary()
    # H 之后的演进：来源从未知补为确认，在场最终转为缺席。
    _confirm(capture, owned, file_id, 11)
    _presence(capture, owned, file_id, 2)
    _presence(capture, owned, file_id, 3)
    now = history.current_boundary()

    # 独立事实：H 时 F 已由 11 观察、来源未知、未见缺席；仅 F 一个设备文件。
    expected_old_ids = [file_id]
    observed_at_h, owner_present = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.OBSERVED_DEVICE_FILES, action_id=11))
    assert owner_present is True
    file_ids_at_h = [record.file_id for record in observed_at_h]
    assert file_ids_at_h == expected_old_ids
    assert observed_at_h[0].file_type == "device_file"
    assert observed_at_h[0].row["source_action_id"] is None
    assert observed_at_h[0].row["presence_state"] == 1

    confirmed_at_h, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES, action_id=11))
    assert [record.file_id for record in confirmed_at_h] == []

    observed_now, _ = _collect(history, FileHistoryRequest(
        boundary=now, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES, action_id=11))
    assert [record.file_id for record in observed_now] == expected_old_ids
    assert observed_now[0].row["source_action_id"] == 11
    assert observed_now[0].row["presence_state"] == 3


async def test_old_intermediate_keeps_registration_state_at_h(
        tmp_path: Path) -> None:
    """H 后修复输出取得完整字节：H 时仍是登记无字节的责任记录。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    source_id = _observe(capture, owned, 11, "task-a/original.mp4")
    _seed_processing(owned.connection, 1, 11, source_id)
    _seed_repair_pending(capture, owned)
    output_id = _start_repair(capture, owned)
    old = history.current_boundary()
    _finish_repair(capture, owned, output_id)
    now = history.current_boundary()

    files_at_h, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.ACTION_INTERMEDIATE_FILES,
        action_id=11))
    # 独立事实：H 时动作 11 名下仅有刚登记、尚未取得字节的修复输出。
    assert [record.file_id for record in files_at_h] == [output_id]
    assert all(record.file_type == "intermediate_file" for record in files_at_h)
    output_at_h = files_at_h[0].row
    assert output_at_h["size_bytes"] is None
    assert output_at_h["sha256"] is None

    files_now, _ = _collect(history, FileHistoryRequest(
        boundary=now, kind=FileHistoryKind.ACTION_INTERMEDIATE_FILES,
        action_id=11))
    output_now = files_now[0].row
    assert output_now["size_bytes"] == 8
    assert output_now["sha256"] == "b" * 64


async def test_filtered_empty_page_continues(tmp_path: Path) -> None:
    """首页候选在 H 均无效：返回零项但未读完，后页仍返回有效文件。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    third = _observe(capture, owned, 11, "task-a/third.mp4")
    # H 时只有 third 已确认来源；first、second 在 H 之后才确认。
    _confirm(capture, owned, third, 11)
    old = history.current_boundary()
    _confirm(capture, owned, first, 11)
    _confirm(capture, owned, second, 11)

    first_page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=2))
    assert first_page.owner_present is True
    assert [record.file_id for record in first_page.page.items] == []
    assert first_page.page.next_cursor is not None
    assert first_page.page.exhausted is False

    second_page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=2, cursor=first_page.page.next_cursor))
    assert [record.file_id for record in second_page.page.items] == [third]
    assert second_page.page.exhausted is True


async def test_batch_limit_one_skips_invalid_candidates(tmp_path: Path) -> None:
    """批次上限 1：无效候选页返回零项并推进，有效候选按升序返回。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    third = _observe(capture, owned, 11, "task-a/third.mp4")
    _confirm(capture, owned, first, 11)
    _confirm(capture, owned, third, 11)
    old = history.current_boundary()
    _confirm(capture, owned, second, 11)

    records, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=1))
    assert [record.file_id for record in records] == [first, third]


async def test_exact_candidate_multiple_ends_with_empty_last_page(
        tmp_path: Path) -> None:
    """候选数恰为批次倍数时先返回满页，最后一页空且读完。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    _confirm(capture, owned, first, 11)
    _confirm(capture, owned, second, 11)
    # 干扰文件抬高固定上界但不属于查询集合（另一来源动作）。
    decoy = _observe(capture, owned, 12, "task-b/decoy.mp4")
    _confirm(capture, owned, decoy, 12)
    old = history.current_boundary()

    page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=2))
    assert [record.file_id for record in page.page.items] == [first, second]
    assert page.page.next_cursor is not None

    last = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=2, cursor=page.page.next_cursor))
    assert [record.file_id for record in last.page.items] == []
    assert last.page.exhausted is True


async def test_fixed_upper_bound_excludes_files_created_after_first_page(
        tmp_path: Path) -> None:
    """首页之后新建的文件不进入本次扫描的固定上界。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    _confirm(capture, owned, first, 11)
    _confirm(capture, owned, second, 11)
    old = history.current_boundary()

    page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=1))
    assert [record.file_id for record in page.page.items] == [first]
    # 页间新建并通过生产命令确认的文件不在固定上界内。
    later = _observe(capture, owned, 11, "task-a/later.mp4")
    _confirm(capture, owned, later, 11)

    rest, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=1, cursor=page.page.next_cursor))
    assert [record.file_id for record in rest] == [second]


async def test_commits_between_pages_do_not_change_h_membership(
        tmp_path: Path) -> None:
    """页间提交不改变 H 集合：越过的候选不回头，未过的按 H 事实判。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    passed = _observe(capture, owned, 11, "task-a/passed.mp4")
    _observe(capture, owned, 11, "task-a/current.mp4")
    pending = _observe(capture, owned, 11, "task-a/pending.mp4")
    _confirm(capture, owned, passed, 11)
    old = history.current_boundary()

    page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=1))
    assert [record.file_id for record in page.page.items] == [passed]
    # 页间：已越过与未检查的候选在 H 后补确认，都不进入 H 集合。
    _confirm(capture, owned, pending, 11)

    rest, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=2, cursor=page.page.next_cursor))
    assert [record.file_id for record in rest] == []


async def test_owner_absent_at_h_is_explicit_result(tmp_path: Path) -> None:
    """主对象在 H 不存在与存在但集合为空分别表达。"""
    owned, target = _environment(tmp_path)
    history = HistoryRepository(target)

    missing, owner_present = _collect(history, FileHistoryRequest(
        boundary=_SEED_BOUNDARY, kind=FileHistoryKind.OBSERVED_DEVICE_FILES,
        action_id=99))
    assert owner_present is False
    assert [record.file_id for record in missing] == []

    # 动作 12 存在但从未观察过设备文件：主对象存在、集合为空。
    empty, owner_present = _collect(history, FileHistoryRequest(
        boundary=_SEED_BOUNDARY, kind=FileHistoryKind.OBSERVED_DEVICE_FILES,
        action_id=12))
    assert owner_present is True
    assert [record.file_id for record in empty] == []


async def test_file_types_with_same_id_restore_independently(
        tmp_path: Path) -> None:
    """设备文件与主机文件同 ID 互不混淆，身份始终带类型。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    file_id = _observe(capture, owned, 11, "task-a/same-id.mp4")
    _seed_processing(owned.connection, 1, 11, file_id)
    _seed_repair_pending(capture, owned)
    # 生产设备文件与修复输出在各表内都从 1 起编：同一编号跨类型。
    repair_id = _start_repair(capture, owned)
    old = history.current_boundary()
    assert repair_id == file_id

    device_files, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.OBSERVED_DEVICE_FILES, action_id=11))
    intermediate_files, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.ACTION_INTERMEDIATE_FILES,
        action_id=11))
    device_ids = [record.file_id for record in device_files]
    intermediate_ids = [record.file_id for record in intermediate_files]
    assert device_ids == [file_id]
    assert intermediate_ids == [repair_id]
    for record in device_files:
        assert record.file_type == "device_file"
        assert "identity_key" in record.row
    for record in intermediate_files:
        assert record.file_type == "intermediate_file"
        assert "relative_path" in record.row


# ---- 引用类查询：先恢复引用方再取文件 ----


async def test_output_file_reference_uses_h_facts(tmp_path: Path) -> None:
    """产物在 H 引用的设备文件按 H 事实返回；产物 H 后才登记则明确表达。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first_file = _observe(capture, owned, 11, "task-a/original.mp4")
    _finish_output(capture, owned, 11, first_file)
    old = history.current_boundary()
    # 第二个产物在 H 之后才由动作 12 登记。
    second_file = _observe(capture, owned, 12, "task-b/second.mp4")
    _finish_output(capture, owned, 12, second_file)
    with closing(owned.connection.execute(
            "SELECT id FROM outputs ORDER BY id")) as cursor:
        output_ids = [int(row[0]) for row in cursor.fetchall()]
    assert output_ids == [1, 2]

    result = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.OUTPUT_FILE, output_id=1))
    assert result.owner_present is True
    assert [record.file_id for record in result.page.items] == [first_file]
    assert result.page.items[0].file_type == "device_file"
    assert result.page.exhausted is True

    later = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.OUTPUT_FILE, output_id=2))
    assert later.owner_present is False
    assert [record.file_id for record in later.page.items] == []


async def test_copy_files_reference_restores_source_and_target(
        tmp_path: Path) -> None:
    """拷贝在 H 的源与目标文件按同一 H 恢复（处理拷贝归动作子树）。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    # 受理建立生产动作：拷贝的引用方按动作子树恢复，动作必须可恢复。
    action_id = await _submit_plan(owned)
    source_id = _observe(capture, owned, action_id, "task-a/original.mp4")
    _seed_processing(owned.connection, 1, action_id, source_id)
    _seed_repair_pending(capture, owned)
    target_id = _start_repair(capture, owned)
    _seed_file_copies(owned.connection, 801, 1, source_id, target_id)
    owned.connection.commit()
    boundary = history.current_boundary()

    result = history.read_files_at_h(FileHistoryRequest(
        boundary=boundary, kind=FileHistoryKind.COPY_FILES, copy_id=801))
    assert result.owner_present is True
    types = {record.file_type for record in result.page.items}
    assert types == {"device_file", "intermediate_file"}
    by_identity = {(record.file_type, record.file_id) for record in result.page.items}
    assert by_identity == {("device_file", source_id), ("intermediate_file", target_id)}
    assert result.page.exhausted is True


async def test_processing_file_references_at_h(tmp_path: Path) -> None:
    """处理引用按 H 事实：修复输出 H 时未登记，成功固定后才引用。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    action_id = await _submit_plan(owned)
    source_id = _observe(capture, owned, action_id, "task-a/original.mp4")
    _seed_processing(owned.connection, 1, action_id, source_id)
    _seed_repair_pending(capture, owned)
    output_id = _start_repair(capture, owned)
    old = history.current_boundary()
    _finish_repair(capture, owned, output_id)
    now = history.current_boundary()

    before, _ = _collect(history, FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.PROCESSING_FILES, action_id=action_id))
    assert [(record.file_type, record.file_id) for record in before] == [
        ("device_file", source_id)]

    after, _ = _collect(history, FileHistoryRequest(
        boundary=now, kind=FileHistoryKind.PROCESSING_FILES, action_id=action_id))
    assert [(record.file_type, record.file_id) for record in after] == [
        ("device_file", source_id), ("intermediate_file", output_id)]


async def test_delivery_intermediate_files_restore_at_h(read_targets) -> None:
    """交付创建的主机文件按交付归属在同一 H 恢复（生产交付链）。"""
    from ..outputs.test_read_associations import _command
    from camctl.persistence.repositories.outputs import OutputsRepository

    owned = read_targets
    result = OutputsRepository().grant_file(
        _command(), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    delivery_id = result.value.delivery_id
    target_id = result.value.target_file_id
    database_path = Path(owned.connection.execute(
        "PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(database_path)
    boundary = history.current_boundary()

    records, owner_present = _collect(history, FileHistoryRequest(
        boundary=boundary, kind=FileHistoryKind.DELIVERY_INTERMEDIATE_FILES,
        delivery_id=delivery_id))
    assert owner_present is True
    assert [record.file_id for record in records] == [target_id]
    assert records[0].file_type == "intermediate_file"
    assert "relative_path" in records[0].row


async def test_required_reference_to_missing_file_is_consistency_error(
        tmp_path: Path) -> None:
    """H 已保存的必需引用指向不存在的文件按一致性错误处理。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first_file = _observe(capture, owned, 11, "task-a/original.mp4")
    _finish_output(capture, owned, 11, first_file)
    old = history.current_boundary()
    owned.connection.close()
    # 注入投影损坏：绕过外键删除被引用文件行。
    rogue = sqlite3.connect(target)
    rogue.execute("PRAGMA foreign_keys=OFF")
    rogue.execute("DELETE FROM device_files WHERE id=?", (first_file,))
    rogue.commit()
    rogue.close()

    with pytest.raises(ConsistencyError):
        history.read_files_at_h(FileHistoryRequest(
            boundary=old, kind=FileHistoryKind.OUTPUT_FILE, output_id=1))


# ---- 失败与继续位置校验 ----


async def test_restore_failure_is_not_an_empty_page(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选恢复失败按错误传播，不解释为空页或读完。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    file_id = _observe(capture, owned, 11, "task-a/original.mp4")
    _confirm(capture, owned, file_id, 11)
    old = history.current_boundary()

    def broken(self, entity, entity_id, boundary, **kwargs):
        raise sqlite3.OperationalError("注入的读取失败")

    monkeypatch.setattr(HistoryRepository, "restore_entity", broken)
    with pytest.raises(sqlite3.OperationalError):
        history.read_files_at_h(FileHistoryRequest(
            boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
            action_id=11))


async def test_cursor_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    """继续位置绑定查询种类、H 与所属对象，不在不同集合间复用。"""
    owned, target = _environment(tmp_path)
    capture = CaptureRepository()
    history = HistoryRepository(target)
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    _confirm(capture, owned, first, 11)
    _confirm(capture, owned, second, 11)
    old = history.current_boundary()

    page = history.read_files_at_h(FileHistoryRequest(
        boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
        action_id=11, batch_size=1))
    cursor = page.page.next_cursor
    assert cursor is not None

    with pytest.raises(BoundaryError):
        history.read_files_at_h(FileHistoryRequest(
            boundary=old, kind=FileHistoryKind.OBSERVED_DEVICE_FILES,
            action_id=11, cursor=cursor))
    with pytest.raises(BoundaryError):
        history.read_files_at_h(FileHistoryRequest(
            boundary=old, kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
            action_id=12, cursor=cursor))
    # 新提交推进当前边界后，同一继续位置不再属于新的 H。
    _observe(capture, owned, 11, "task-a/extra.mp4")
    with pytest.raises(BoundaryError):
        history.read_files_at_h(FileHistoryRequest(
            boundary=history.current_boundary(),
            kind=FileHistoryKind.CONFIRMED_SOURCE_FILES,
            action_id=11, cursor=cursor))
