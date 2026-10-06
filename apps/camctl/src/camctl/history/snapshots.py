"""历史快照维护：事务外编码、窄仓储分批推进与会话内停用。

prepare_snapshot 把一份一致读取视图内取得的对象状态编码为 JSON
Lines：头记录携带对象身份、完整边界 S 与 S 处累计变化次数；行按
表名字典序与行 ID 升序，使用项目的精确确定编码，内容按块组织。
maintain_snapshots 通过窄仓储端口分批推进：候选空则等待唤醒；
入队前超时只停用本次会话的维护；实际数据库失败按状态库错误上
抛；进入收尾不再安排新批次。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import parse_exact_json
from camctl.persistence.transaction import encode_json_value

__all__ = [
    "EntityImage",
    "MaintenanceContext",
    "MaintenanceOutcome",
    "MaintenanceResult",
    "MaintenanceState",
    "PreparedSnapshot",
    "SavedProgress",
    "SnapshotEnqueueTimeout",
    "SnapshotMaintenanceError",
    "SnapshotRef",
    "SnapshotRow",
    "decode_snapshot",
    "maintain_snapshots",
    "prepare_snapshot",
]

#: 编码与写出使用的单块字节缓冲上限。
CHUNK_BYTES = 64 * 1024

#: 当前快照格式版本；与 entity_snapshots.format_version 的表约束一致。
FORMAT_VERSION = 1


class SnapshotMaintenanceError(Exception):
    """快照维护遇到的状态库错误或无效输入。

    候选或进度缺失、计数矛盾、保存失败、提交结果未知及编码输入
    不合法都属于本类；不把损坏解释为没有候选或空快照。
    """


class SnapshotEnqueueTimeout(Exception):
    """快照数据库操作在入队前等待空位超时。

    该操作未入队且以后也不会执行；只停用本次会话的维护，不用
    于替代实际数据库错误的处理。
    """


@dataclass(frozen=True)
class SnapshotRef:
    """一个快照维护范围内对象的身份（类型编号与行 ID）。"""

    entity_type: int
    entity_id: int

    def __post_init__(self) -> None:
        for name in ("entity_type", "entity_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SnapshotMaintenanceError(
                    f"{name} 必须是正整数: {value!r}")


@dataclass(frozen=True)
class SnapshotRow:
    """快照中的一行完整投影（JSON 列为结构化值，空值为 None）。"""

    table: str
    row_id: int
    values: Mapping[str, object]


@dataclass(frozen=True)
class EntityImage:
    """一份在一致读取视图内取得的快照种子。

    boundary 是该视图的完整已提交边界 S；change_count 是对象在
    S 处的累计变化次数；rows 覆盖对象按归属规则应进入快照的全
    部自身行。
    """

    entity_type: int
    entity_id: int
    boundary: HistoryBoundary
    change_count: int
    rows: tuple[SnapshotRow, ...]
    format_version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        SnapshotRef(self.entity_type, self.entity_id)
        if self.boundary.txn_id < 1 or self.boundary.last_event_id < 1:
            raise SnapshotMaintenanceError(
                f"快照边界必须是完整已提交边界: {self.boundary!r}")
        if (isinstance(self.change_count, bool) or not isinstance(self.change_count, int)
                or self.change_count < 1):
            raise SnapshotMaintenanceError(
                f"快照计数必须是正整数: {self.change_count!r}")
        if self.format_version != FORMAT_VERSION:
            raise SnapshotMaintenanceError(
                f"未知的快照格式版本: {self.format_version!r}")
        if not self.rows:
            raise SnapshotMaintenanceError("快照种子缺少自身行")
        for row in self.rows:
            if not isinstance(row.table, str) or not row.table:
                raise SnapshotMaintenanceError(f"行表名非法: {row.table!r}")
            if isinstance(row.row_id, bool) or not isinstance(row.row_id, int) or row.row_id < 1:
                raise SnapshotMaintenanceError(f"行标识非法: {row.row_id!r}")


@dataclass(frozen=True)
class PreparedSnapshot:
    """事务外完成编码的一份快照：内容按块组织，标记 S 与 S 处计数。"""

    entity_type: int
    entity_id: int
    boundary: HistoryBoundary
    change_count: int
    format_version: int
    chunks: tuple[bytes, ...]
    length: int

    def __post_init__(self) -> None:
        if self.length != sum(len(chunk) for chunk in self.chunks):
            raise SnapshotMaintenanceError("分块长度与内容不一致")


def _encoded_lines(seed: EntityImage):
    """按表名字典序与行 ID 升序逐行产出 JSON Lines 文本。"""
    header = {
        "format_version": seed.format_version,
        "entity_type": seed.entity_type,
        "entity_id": seed.entity_id,
        "boundary_event_id": seed.boundary.last_event_id,
        "change_count": seed.change_count,
    }
    yield encode_json_value(header)
    ordered = sorted(seed.rows, key=lambda row: (row.table, row.row_id))
    for row in ordered:
        yield encode_json_value({"table": row.table, "row": dict(row.values)})


def prepare_snapshot(seed: EntityImage) -> PreparedSnapshot:
    """在事务外把一份种子编码为分块的快照内容。

    不把整个对象拼接成一个字符串：逐行编码并按固定字节块切分，
    块边界与行边界无关；每行以 LF 结尾。
    """
    chunks: list[bytes] = []
    buffer = bytearray()
    for line in _encoded_lines(seed):
        buffer += line.encode("utf-8") + b"\n"
        while len(buffer) >= CHUNK_BYTES:
            chunks.append(bytes(buffer[:CHUNK_BYTES]))
            del buffer[:CHUNK_BYTES]
    if buffer:
        chunks.append(bytes(buffer))
    if not chunks:
        raise SnapshotMaintenanceError("快照内容为空")
    return PreparedSnapshot(
        entity_type=seed.entity_type,
        entity_id=seed.entity_id,
        boundary=seed.boundary,
        change_count=seed.change_count,
        format_version=seed.format_version,
        chunks=tuple(chunks),
        length=sum(len(chunk) for chunk in chunks),
    )


def decode_snapshot(content: bytes, *,
                    expect_ref: SnapshotRef) -> tuple[dict, tuple[SnapshotRow, ...]]:
    """解码并核对一份快照内容，返回头记录与行集。

    读取时核对头身份、格式版本与行结构，数字按精确规则解码；
    行归属不一致或正文无法解释时按快照错误处理，不返回部分
    结果。
    """
    if not isinstance(content, bytes) or not content:
        raise SnapshotMaintenanceError("快照内容为空")
    if not content.endswith(b"\n"):
        raise SnapshotMaintenanceError("快照内容缺少结尾换行")
    try:
        records = [
            parse_exact_json(line)
            for line in content.decode("utf-8").split("\n")[:-1]
        ]
    except Exception as error:
        raise SnapshotMaintenanceError(f"快照内容无法解码: {error}") from error
    if not records or not isinstance(records[0], dict):
        raise SnapshotMaintenanceError("快照缺少头记录")
    header = records[0]
    if (header.get("format_version") != FORMAT_VERSION
            or header.get("entity_type") != expect_ref.entity_type
            or header.get("entity_id") != expect_ref.entity_id):
        raise SnapshotMaintenanceError(
            f"快照头与对象身份不符: {header!r} vs {expect_ref!r}")
    if not isinstance(header.get("boundary_event_id"), int) or not isinstance(
            header.get("change_count"), int):
        raise SnapshotMaintenanceError(f"快照头缺少完整边界或计数: {header!r}")
    rows: list[SnapshotRow] = []
    for record in records[1:]:
        if not isinstance(record, dict) or set(record) != {"table", "row"}:
            raise SnapshotMaintenanceError(f"快照行结构非法: {record!r}")
        table, values = record["table"], record["row"]
        if not isinstance(table, str) or not isinstance(values, dict):
            raise SnapshotMaintenanceError(f"快照行类型非法: {record!r}")
        if values.get("id") is None:
            raise SnapshotMaintenanceError(f"快照行缺少标识: {record!r}")
        rows.append(SnapshotRow(table=table, row_id=int(values["id"]), values=values))
    return header, tuple(rows)


class MaintenanceState:
    """本次会话内的快照维护状态；停用与收尾只在会话内保存。

    停用后新提交、队列空位与迟到通知都不重新启用维护；累计计
    数、快照基准与派生进度仍以数据库为准。
    """

    def __init__(self) -> None:
        self.disabled = False
        self.closing = False

    def disable(self) -> None:
        """入队前超时后停用本次会话的后续快照维护。"""
        self.disabled = True

    def close(self) -> None:
        """进入会话收尾：不再安排新的维护批次。"""
        self.closing = True


class SnapshotStore(Protocol):
    """快照维护的窄仓储端口。

    每个方法对应一类数据库操作，分别遵守入队等待时限；入队前
    超时以 SnapshotEnqueueTimeout 表达，实际数据库失败以
    SnapshotMaintenanceError 表达。
    """

    async def snapshot_candidates(
            self, threshold: int, limit: int) -> tuple[SnapshotRef, ...]:
        """按阈值与排序取一批候选对象身份。"""
        ...

    async def load_entity_images(
            self, refs: tuple[SnapshotRef, ...]) -> tuple[EntityImage, ...]:
        """在同一读取视图内取得各对象的状态、S 与 S 处计数。"""
        ...

    async def save_snapshots(
            self, prepared: tuple[PreparedSnapshot, ...]
    ) -> tuple[SavedProgress, ...]:
        """短写事务保存快照并更新维护进度，返回各对象剩余次数。"""
        ...


@dataclass(frozen=True)
class SavedProgress:
    """一份快照保存后的进度结果：对象身份与尚未包含的变化次数。"""

    ref: SnapshotRef
    remaining_changes: int


@dataclass(frozen=True)
class MaintenanceContext:
    """一次维护调用的配置与协作对象。"""

    threshold: int
    batch_size: int
    state: MaintenanceState
    store: SnapshotStore

    def __post_init__(self) -> None:
        for name in ("threshold", "batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SnapshotMaintenanceError(f"{name} 必须是正整数: {value!r}")


class MaintenanceOutcome(Enum):
    """一次维护调用的结束分类。"""

    IDLE = "idle"                # 候选查询为空，等待提交或发现唤醒
    DRAINED = "drained"          # 本轮候选已读完，会话仍正常运行
    DISABLED = "disabled"        # 入队前超时，本次会话维护停用
    CLOSING = "closing"          # 会话收尾，未安排新批次


@dataclass(frozen=True)
class MaintenanceResult:
    """一次维护调用的结果汇总。"""

    outcome: MaintenanceOutcome
    saved: tuple[SavedProgress, ...] = ()
    batches: int = 0


async def maintain_snapshots(context: MaintenanceContext) -> MaintenanceResult:
    """按窄仓储端口分批推进快照维护。

    每批依次执行候选查询、依据读取、事务外准备与短写事务保存；
    批间让出执行机会并重新检查会话状态。候选不足一批时本轮读
    完；任何数据库操作在入队前超时则停用本次会话的维护并返回，
    实际数据库错误按状态库错误向上传播。
    """
    if context.state.disabled:
        return MaintenanceResult(outcome=MaintenanceOutcome.DISABLED)
    saved: list[SavedProgress] = []
    batches = 0
    while True:
        if context.state.closing:
            return MaintenanceResult(
                outcome=MaintenanceOutcome.CLOSING,
                saved=tuple(saved), batches=batches)
        try:
            candidates = await context.store.snapshot_candidates(
                context.threshold, context.batch_size)
        except SnapshotEnqueueTimeout:
            context.state.disable()
            return MaintenanceResult(
                outcome=MaintenanceOutcome.DISABLED,
                saved=tuple(saved), batches=batches)
        if not candidates:
            return MaintenanceResult(
                outcome=MaintenanceOutcome.IDLE, saved=tuple(saved), batches=batches)
        try:
            images = await context.store.load_entity_images(tuple(candidates))
        except SnapshotEnqueueTimeout:
            context.state.disable()
            return MaintenanceResult(
                outcome=MaintenanceOutcome.DISABLED,
                saved=tuple(saved), batches=batches)
        prepared = tuple(prepare_snapshot(image) for image in images)
        if prepared:
            try:
                batch_saved = await context.store.save_snapshots(prepared)
            except SnapshotEnqueueTimeout:
                context.state.disable()
                return MaintenanceResult(
                    outcome=MaintenanceOutcome.DISABLED,
                    saved=tuple(saved), batches=batches)
            saved.extend(batch_saved)
        batches += 1
        await asyncio.sleep(0)
        if len(candidates) < context.batch_size:
            return MaintenanceResult(
                outcome=MaintenanceOutcome.DRAINED,
                saved=tuple(saved), batches=batches)
