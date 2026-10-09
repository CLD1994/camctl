"""C8 内部输入取得编排的组件集成测试。

真实 SQLite、真实资格与分段拷贝事务、真实文件与受 SourceStream
契约约束的读取会话共同验证编排：资格建档、续传准备、分段推进与
完整性收尾；等待、取消跳过、段失败续传与摘要不一致重拷按分区验
证。读取会话替身不证明真实设备读取。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.input_copy import (
    InputContext,
    InputPhase,
    obtain_recording_input,
)
from camctl.capture.media import SaveDisposition, SaveReceipt
from camctl.contracts.values import new_operation_key
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.models import BoundDirectories, FilePurpose
from camctl.outputs.copy import (
    CompletionContext,
    CopyContext,
    SegmentContext,
    SegmentOutcome,
    SourceDigest,
    complete_copy,
    copy_next_segment,
    prepare_copy,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_capture_guards()
register_operation_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000
_CONTENT = b"0123456789"
_CONFIG = OperationConfig(max_attempts=3, timeout_s=Decimal("10"),
                          retry_interval_s=Decimal("0"))


class _ByteStream:
    """受 SourceStream 契约约束的设备流替身；可在指定位后失败。"""

    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self._content = content
        self._position = 0
        self._fail_after = fail_after

    def read(self, limit: int) -> bytes:
        if self._fail_after is not None and self._position >= self._fail_after:
            raise OSError("source read failed")
        data = self._content[self._position:self._position + limit]
        self._position += len(data)
        return data

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Sessions:
    """真实 ReadSession 工厂替身：记录打开偏移，可挂取消钩子。"""

    def __init__(self, content: bytes, *, fail_after: int | None = None,
                 on_open=None) -> None:
        self.content = content
        self.fail_after = fail_after
        self.on_open = on_open
        self.opens: list[int] = []

    async def open_session(self, source_device_file_id: int, offset: int):
        self.opens.append(offset)
        if self.on_open is not None:
            self.on_open()
        stream = _ByteStream(self.content[offset:], fail_after=self.fail_after)
        return ReadSession(
            SourceFile(str(source_device_file_id), {}, len(self.content)),
            offset, stream, Decimal("10"),
        )


class _Digest:
    """源端摘要读取替身。"""

    def __init__(self, digest: str | None) -> None:
        self.digest = digest

    async def read_digest(self) -> SourceDigest:
        return SourceDigest(digest=self.digest, error=None)


class _Copies:
    """把真实资格与拷贝事务适配成编排端口。"""

    def __init__(self, owned, roots: BoundDirectories, occurred_at: int,
                 *, max_recopies: int = 1) -> None:
        self.owned = owned
        self.roots = roots
        self.occurred_at = occurred_at
        self.max_recopies = max_recopies
        self.repository = OutputsRepository()

    def _receipt(self, outcome):
        if outcome.kind is DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    def qualify(self, candidate):
        return self._receipt(self.repository.grant_file(
            candidate, new_operation_key(), self.owned))

    def copy_state(self, copy_id: int):
        return self.repository.load_copy_state(copy_id, self.owned)

    async def prepare(self, copy_id: int):
        return await prepare_copy(copy_id, CopyContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at))

    async def transfer(self, copy_id: int, session, segment_size: int):
        return await copy_next_segment(copy_id, SegmentContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at, segment_size=segment_size,
            session=session))

    async def complete(self, copy_id: int, digest):
        return await complete_copy(copy_id, CompletionContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at, digest=digest,
            max_recopies=self.max_recopies))


@pytest.fixture
def pipeline(tmp_path: Path):
    """检查决定固定为需要检查的处理行、可靠原片与真实目录绑定。"""
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
    _seed_action(connection, 1, 1)
    _seed_device_file(connection, 11)
    _seed_processing(connection, 1, 1)
    connection.commit()

    staging = tmp_path / "staging"
    for name in ("recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    roots = BoundDirectories(staging=staging)
    sessions = _Sessions(_CONTENT)
    copies = _Copies(owned, roots, _NOW + 10)

    def context(**overrides):
        values = dict(
            action_id=1, processing_id=1, source_device_file_id=11,
            target_extension="mp4", config=_CONFIG, segment_size=4,
            staging=staging, copies=copies, sessions=sessions,
            digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
            occurred_at=_NOW + 10,
        )
        values.update(overrides)
        return InputContext(**values)

    return owned, roots, sessions, copies, context


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, 4241 + plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 0, 'rec', 2, 'cam-1', ?, NULL, '{}',"
        " '{\"target_duration_s\": 60}', 'camctl-adb', 1000, '{}', 2, 1, 0, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, _NOW),
    )


def _seed_device_file(connection, file_id: int, *, checksum_support: int = 2) -> None:
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, 1, ?, '{}', '{}', 'video.mp4', 'video/mp4', 2, NULL, NULL,"
        " 2, 3, '{}', ?, ?, NULL, NULL, 1, 1, 1)",
        (file_id, f"file-{file_id:04d}", len(_CONTENT), checksum_support),
    )


def _seed_processing(connection, processing_id: int, action_id: int) -> None:
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (?, ?, 11, 1, 3, ?, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (processing_id, action_id,
         '{"reason": 2, "target_duration_ms": 60000}'),
    )


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _input_row(owned):
    return _row(
        owned,
        "SELECT id, relative_path, size_bytes, sha256 FROM intermediate_files"
        " WHERE purpose=2")


def _copy_row(owned, copy_id: int):
    return _row(
        owned,
        "SELECT committed_bytes, verification_state, round FROM file_copies"
        " WHERE id=?", copy_id)


# ---- 正常与重入 ----


@pytest.mark.asyncio
async def test_full_input_copy_pipeline(pipeline) -> None:
    owned, roots, sessions, copies, context = pipeline
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.INPUT_READY, step.error
    file_id, relative, size, sha = _input_row(owned)
    assert size == len(_CONTENT)
    assert sha == hashlib.sha256(_CONTENT).hexdigest()
    target_path = roots.staging / relative
    assert target_path.read_bytes() == _CONTENT
    assert step.input_file is not None
    assert (step.input_file.file_id, step.input_file.relative_path) == (
        file_id, relative)
    assert step.input_file.root == roots.staging
    assert _copy_row(owned, step.copy_id) == (len(_CONTENT), 3, 1)
    assert sessions.opens == [0]

    again = await obtain_recording_input(context())
    assert again.phase is InputPhase.INPUT_READY
    assert again.copy_id == step.copy_id
    assert sessions.opens == [0]
    assert _row(owned, "SELECT COUNT(*) FROM file_copies")[0] == 1


@pytest.mark.asyncio
async def test_verify_only_reentry_skips_session(pipeline) -> None:
    """原实际 clean End 已持有：重入只完成本地校验，不重开源会话。"""
    owned, roots, sessions, copies, context = pipeline
    qualification = copies.repository.grant_file(
        FileCandidate(
            action_id=1, item_id=None, processing_id=1, output_id=None,
            source_device_file_id=11, target_extension="mp4",
            delivery_extension=None, delivery_display_name=None,
            config=_CONFIG, occurred_at=_NOW + 10),
        new_operation_key(), owned)
    assert qualification.kind is DbOutcomeKind.COMPLETED, qualification.error
    copy_id = qualification.value.copy_id
    await copies.prepare(copy_id)
    session = await sessions.open_session(11, 0)
    try:
        while True:
            seg = await copies.transfer(copy_id, session, 4)
            if seg.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                break
    finally:
        session.request_stop()
        end = await session.wait_stopped()
    assert end.stopped is True and end.error is None
    assert end.bytes_read == len(_CONTENT)
    driven = list(sessions.opens)
    assert driven == [0]

    step = await obtain_recording_input(context(read_end=end))
    assert step.phase is InputPhase.INPUT_READY, step.error
    assert step.read_end is end
    assert sessions.opens == driven
    assert _copy_row(owned, copy_id)[1] == 3
    assert (roots.staging / step.input_file.relative_path).read_bytes() == _CONTENT


# ---- 资格等待 ----


@pytest.mark.asyncio
async def test_undecided_check_waits(pipeline) -> None:
    owned, roots, sessions, copies, context = pipeline
    owned.connection.execute(
        "UPDATE recording_processing SET check_decision=1 WHERE id=1")
    owned.connection.commit()
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.WAITING
    assert step.reason == "input_undecided"
    assert sessions.opens == []
    assert _input_row(owned) is None


@pytest.mark.asyncio
async def test_future_schedule_waits(pipeline) -> None:
    owned, roots, sessions, copies, context = pipeline
    owned.connection.execute(
        "UPDATE actions SET scheduled_at=? WHERE id=1", (_NOW + 1_000_000,))
    owned.connection.commit()
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.WAITING
    assert step.reason == "not_due"
    assert _input_row(owned) is None


# ---- 失败与恢复 ----


@pytest.mark.asyncio
async def test_segment_failure_resumes_from_committed(pipeline) -> None:
    owned, roots, sessions, copies, context = pipeline
    sessions.fail_after = 4
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.SEGMENT_FAILED
    assert step.error is not None
    copy_id = step.copy_id
    assert _copy_row(owned, copy_id)[0] == 4

    sessions.fail_after = None
    sessions.content = _CONTENT
    resumed = await obtain_recording_input(context())
    assert resumed.phase is InputPhase.INPUT_READY, resumed.error
    assert sessions.opens == [0, 4]
    file_id, relative, size, sha = _input_row(owned)
    assert (size, sha) == (len(_CONTENT),
                           hashlib.sha256(_CONTENT).hexdigest())
    assert (roots.staging / relative).read_bytes() == _CONTENT


@pytest.mark.asyncio
async def test_mismatch_registers_recopy_and_retry_succeeds(pipeline) -> None:
    """首轮读取到损坏字节：登记重拷；次轮读取正常字节后完成。

    源端摘要保持为真实值；不一致来自目标字节与源摘要的比较。
    """
    owned, roots, sessions, copies, context = pipeline
    copies.max_recopies = 2
    sessions.content = b"XXXXXXXXXX"
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.RECOPY_PENDING, step.error
    copy_id = step.copy_id
    committed, verification, rounds = _copy_row(owned, copy_id)
    assert (committed, verification, rounds) == (0, 1, 2)
    assert _row(owned, "SELECT reset_state FROM file_copies WHERE id=?",
                copy_id)[0] == 2
    assert _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=3",
    )[0] == 1

    sessions.content = _CONTENT
    retried = await obtain_recording_input(context())
    assert retried.phase is InputPhase.INPUT_READY, retried.error
    assert sessions.opens == [0, 0]
    assert _copy_row(owned, copy_id) == (len(_CONTENT), 3, 2)
    file_id, relative, size, sha = _input_row(owned)
    assert sha == hashlib.sha256(_CONTENT).hexdigest()
    assert (roots.staging / relative).read_bytes() == _CONTENT


@pytest.mark.asyncio
async def test_owner_cancel_skips_progress_commit(pipeline) -> None:
    owned, roots, sessions, copies, context = pipeline

    def cancel_owner():
        owned.connection.execute(
            "UPDATE actions SET status=6, cancel_requested=1 WHERE id=1")
        owned.connection.commit()

    sessions.on_open = cancel_owner
    step = await obtain_recording_input(context())
    assert step.phase is InputPhase.OWNER_SKIPPED
    assert step.reason == "cancel_requested"
    assert _copy_row(owned, step.copy_id)[0] == 0
    sessions.on_open = None
