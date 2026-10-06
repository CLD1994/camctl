"""历史快照维护的单元测试。

prepare_snapshot 是事务外的纯编码：行按表名字典序与行 ID 升序组
织为 JSON Lines，保存对象在 S 处的计数，不被保存前最新计数覆
盖。maintain_snapshots 按窄仓储端口分批推进：候选空则等待唤
醒，入队前超时只停用本次会话，实际数据库错误不降级；停用后新
提交、空位与迟到通知都不重新启用；进入收尾不再安排新批次。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.history.snapshots import (
    EntityImage,
    MaintenanceContext,
    MaintenanceOutcome,
    MaintenanceState,
    PreparedSnapshot,
    SavedProgress,
    SnapshotEnqueueTimeout,
    SnapshotMaintenanceError,
    SnapshotRef,
    SnapshotRow,
    decode_snapshot,
    maintain_snapshots,
    prepare_snapshot,
)

_REF = SnapshotRef(entity_type=1, entity_id=8)


def _image(*, change_count: int = 5,
           rows: tuple[SnapshotRow, ...] | None = None) -> EntityImage:
    return EntityImage(
        entity_type=_REF.entity_type,
        entity_id=_REF.entity_id,
        boundary=HistoryBoundary(txn_id=3, last_event_id=30),
        change_count=change_count,
        rows=rows if rows is not None else (
            SnapshotRow(table="actions", row_id=8, values={
                "id": 8, "name": "act", "params_json": {"type": "single_shot"},
                "ratio": Decimal("1.234567890123456789"),
            }),
        ),
    )


@dataclass
class _ScriptedStore:
    """按脚本应答的窄仓储替身，记录每次调用与保存内容。"""

    images: tuple[EntityImage, ...] = ()
    candidates: tuple[SnapshotRef, ...] = ()
    remaining_changes: int = 0
    load_error: Exception | None = None
    save_error: Exception | None = None
    candidate_error: Exception | None = None
    limit_seen: int = 0
    threshold_seen: int = 0

    def __post_init__(self) -> None:
        self.candidate_calls = 0
        self.load_calls = 0
        self.save_calls = 0
        self.saved_prepared: tuple[PreparedSnapshot, ...] = ()

    async def snapshot_candidates(self, threshold: int,
                                  limit: int) -> tuple[SnapshotRef, ...]:
        self.candidate_calls += 1
        self.threshold_seen = threshold
        self.limit_seen = limit
        if self.candidate_error is not None:
            raise self.candidate_error
        return self.candidates

    async def load_entity_images(
            self, refs: tuple[SnapshotRef, ...]) -> tuple[EntityImage, ...]:
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        return self.images

    async def save_snapshots(
            self, prepared: tuple[PreparedSnapshot, ...]
    ) -> tuple[SavedProgress, ...]:
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error
        self.saved_prepared = prepared
        return tuple(
            SavedProgress(
                ref=SnapshotRef(item.entity_type, item.entity_id),
                remaining_changes=self.remaining_changes)
            for item in prepared
        )


def _context(store, *, threshold: int = 1, batch_size: int = 16,
             state: MaintenanceState | None = None) -> MaintenanceContext:
    return MaintenanceContext(
        threshold=threshold,
        batch_size=batch_size,
        state=state if state is not None else MaintenanceState(),
        store=store,
    )


# ---- prepare_snapshot：事务外编码 ----


def test_prepare_snapshot_encodes_header_and_sorted_rows() -> None:
    """头记录携带对象身份、S 与 S 处计数；行按表名字典序与 ID 升序。"""
    seed = _image(change_count=5, rows=(
        SnapshotRow(table="file_copies", row_id=2, values={"id": 2}),
        SnapshotRow(table="file_copies", row_id=10, values={"id": 10}),
        SnapshotRow(table="actions", row_id=8, values={
            "id": 8, "name": "act", "params_json": {"type": "single_shot"},
            "ratio": Decimal("1.234567890123456789"), "note": None,
        }),
    ))
    prepared = prepare_snapshot(seed)

    assert prepared.entity_type == 1
    assert prepared.entity_id == 8
    assert prepared.boundary == HistoryBoundary(txn_id=3, last_event_id=30)
    assert prepared.change_count == 5
    assert prepared.length == sum(len(chunk) for chunk in prepared.chunks)
    content = b"".join(prepared.chunks)
    lines = content.split(b"\n")
    assert lines[-1] == b""
    records = [line for line in lines[:-1]]

    import json
    header = json.loads(records[0])
    assert header == {
        "format_version": 1, "entity_type": 1, "entity_id": 8,
        "boundary_event_id": 30, "change_count": 5,
    }
    tables = [json.loads(record)["table"] for record in records[1:]]
    assert tables == ["actions", "file_copies", "file_copies"]
    row_ids = [json.loads(record)["row"]["id"]
               for record in records[1:] if b"file_copies" in record]
    assert row_ids == [2, 10]

    action_row = json.loads(records[1])["row"]
    assert action_row["params_json"] == {"type": "single_shot"}
    # 精确数值以完整数字字面量写入，不经过 float 舍入。
    assert b'"ratio":1.234567890123456789' in records[1]
    assert action_row["note"] is None


def test_prepare_snapshot_splits_large_content_into_chunks() -> None:
    """内容超过单块缓冲时按块输出，不把整个对象拼成一个字符串。"""
    big_text = "x" * 90_000
    seed = _image(rows=(
        SnapshotRow(table="actions", row_id=8, values={
            "id": 8, "evidence_json": {"blob": big_text}}),
    ))
    prepared = prepare_snapshot(seed)
    assert len(prepared.chunks) > 1
    assert prepared.length == sum(len(chunk) for chunk in prepared.chunks)
    assert prepared.length > 90_000


def test_prepare_snapshot_rejects_inconsistent_seed() -> None:
    """计数非正、行表为空或格式版本未知按维护输入错误拒绝。"""
    with pytest.raises(SnapshotMaintenanceError):
        prepare_snapshot(_image(change_count=0))
    with pytest.raises(SnapshotMaintenanceError):
        prepare_snapshot(_image(rows=()))
    with pytest.raises(SnapshotMaintenanceError):
        prepare_snapshot(EntityImage(
            entity_type=1, entity_id=8,
            boundary=HistoryBoundary(txn_id=3, last_event_id=30),
            change_count=5, format_version=2,
            rows=(SnapshotRow(table="actions", row_id=8, values={"id": 8}),),
        ))


# ---- maintain_snapshots：分批推进与停止分区 ----


@pytest.mark.asyncio
async def test_snapshot_preserves_later_changes() -> None:
    """S 处次数 5、保存前最新次数 8：快照标记 5，剩余次数为 3。"""
    store = _ScriptedStore(
        candidates=(_REF,),
        images=(_image(change_count=5),),
        remaining_changes=3,
    )
    result = await maintain_snapshots(_context(store, threshold=1))

    assert result.outcome is MaintenanceOutcome.DRAINED
    assert store.saved_prepared[0].change_count == 5
    assert result.saved[0].ref == _REF
    assert result.saved[0].remaining_changes == 3


@pytest.mark.asyncio
async def test_maintenance_idle_when_no_candidates() -> None:
    """候选查询为空：不读取依据、不保存，按空闲等待唤醒返回。"""
    store = _ScriptedStore(candidates=())
    result = await maintain_snapshots(_context(store, threshold=64))

    assert result.outcome is MaintenanceOutcome.IDLE
    assert result.saved == ()
    assert store.load_calls == 0
    assert store.save_calls == 0


@pytest.mark.asyncio
async def test_maintenance_respects_batch_limit_and_drains() -> None:
    """候选达到阈值与批量上限进入同一批；不足一批时读完即结束。"""
    refs = tuple(SnapshotRef(entity_type=1, entity_id=index) for index in (1, 2))
    store = _ScriptedStore(
        candidates=refs,
        images=tuple(
            _image() for _ in refs),
    )
    result = await maintain_snapshots(_context(store, threshold=1, batch_size=16))

    assert store.threshold_seen == 1
    assert store.limit_seen == 16
    assert result.outcome is MaintenanceOutcome.DRAINED
    assert len(result.saved) == len(refs)


@pytest.mark.asyncio
async def test_enqueue_timeout_disables_for_session() -> None:
    """入队前超时只停用本次维护；同一状态对象下不再查询候选。"""
    state = MaintenanceState()
    store = _ScriptedStore(
        candidates=(_REF,),
        images=(_image(),),
        save_error=SnapshotEnqueueTimeout("保存等待空位超时"),
    )
    result = await maintain_snapshots(_context(store, threshold=1, state=state))
    assert result.outcome is MaintenanceOutcome.DISABLED
    assert state.disabled is True

    # 新提交、空位与迟到通知都通过同一状态对象表达：不再重新启用。
    store_after = _ScriptedStore(candidates=(_REF,))
    again = await maintain_snapshots(_context(store_after, threshold=1, state=state))
    assert again.outcome is MaintenanceOutcome.DISABLED
    assert store_after.candidate_calls == 0


@pytest.mark.asyncio
async def test_real_database_error_is_not_disabled() -> None:
    """已入队后的实际数据库失败按状态库错误上抛，不用停用降级。"""
    store = _ScriptedStore(
        candidates=(_REF,),
        images=(_image(),),
        save_error=SnapshotMaintenanceError("保存事务失败"),
    )
    with pytest.raises(SnapshotMaintenanceError):
        await maintain_snapshots(_context(store, threshold=1))
    assert store.save_calls == 1


@pytest.mark.asyncio
async def test_candidate_query_timeout_disables_before_any_work() -> None:
    """候选查询自身入队超时同样停用，且不进行依据读取。"""
    state = MaintenanceState()
    store = _ScriptedStore(
        candidate_error=SnapshotEnqueueTimeout("候选查询等待空位超时"))
    result = await maintain_snapshots(_context(store, threshold=1, state=state))
    assert result.outcome is MaintenanceOutcome.DISABLED
    assert store.load_calls == 0
    assert state.disabled is True


@pytest.mark.asyncio
async def test_closing_stops_scheduling_new_batches() -> None:
    """进入收尾后不再安排新批次；停用状态优先生效。"""
    state = MaintenanceState()
    state.close()
    store = _ScriptedStore(candidates=(_REF,))
    result = await maintain_snapshots(_context(store, threshold=1, state=state))
    assert result.outcome is MaintenanceOutcome.CLOSING
    assert store.candidate_calls == 0


# ---- decode_snapshot：读取校验 ----


def test_decode_snapshot_returns_rows_and_validates_header() -> None:
    """解码核对头身份与行结构，坏输入按快照错误拒绝。"""
    prepared = prepare_snapshot(_image())
    content = b"".join(prepared.chunks)
    header, rows = decode_snapshot(content, expect_ref=_REF)
    assert header["change_count"] == 5
    assert rows[0].table == "actions"
    assert rows[0].row_id == 8

    with pytest.raises(SnapshotMaintenanceError):
        decode_snapshot(content, expect_ref=SnapshotRef(entity_type=2, entity_id=8))
    with pytest.raises(SnapshotMaintenanceError):
        decode_snapshot(b"not json\n", expect_ref=_REF)
    with pytest.raises(SnapshotMaintenanceError):
        decode_snapshot(b'{"format_version":1}\n', expect_ref=_REF)
