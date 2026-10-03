"""段内小块传输与源无数据观察。

按 [C,E) 顺序逐块读写：每次请求不超过段剩余量，数据在线程内及时
写入目标，不把整段攒入内存。块之间与段尾同步前停止新增普通操作；
正在进行的本地调用等待真实结果并保留实际阶段。目标写入与同步不
参与源无数据等待的计量；源错误按会话分类透传。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from camctl.devices.read_session import ReadEnd, ReadError, ReadSession

__all__ = [
    "DEFAULT_CHUNK_SIZE_BYTES",
    "MAX_CHUNK_SIZE_BYTES",
    "SegmentError",
    "SegmentFailure",
    "SegmentResult",
    "SegmentSpec",
    "WritableFile",
    "transfer_segment",
]

#: 块大小默认值与合法上限；按测量调整，调用前集中定义。
DEFAULT_CHUNK_SIZE_BYTES = 1 << 20
MAX_CHUNK_SIZE_BYTES = 4194304


class SegmentError(ValueError):
    """段传输使用错误：范围非法或会话位置与段起点不符。"""


class WritableFile(Protocol):
    """顺序写入与显式同步；write 返回实际写入数，允许短写和零写。

    只在调用期间使用数据缓冲；抛出异常时，本次调用的写入效果未知。
    """

    def write(self, data: bytes | memoryview) -> int: ...

    def sync(self) -> None: ...


@dataclass(frozen=True)
class SegmentSpec:
    """一次段传输的输入：范围、块大小与停止通知。

    attempt、round_index 与 target_name 是调用方提供的诊断身份，
    不在本模块解释。
    """

    attempt: str
    round_index: int
    target_name: str
    range_start: int
    range_end: int
    chunk_size: int
    stop: threading.Event

    def __post_init__(self) -> None:
        if not self.attempt or not self.target_name:
            raise SegmentError("尝试与目标名称不能为空")
        for field, value in (
            ("round_index", self.round_index), ("range_start", self.range_start),
            ("range_end", self.range_end), ("chunk_size", self.chunk_size),
        ):
            if type(value) is not int:
                raise SegmentError(f"{field} 必须是整数: {value!r}")
        if self.round_index < 0:
            raise SegmentError(f"轮次不能为负: {self.round_index!r}")
        if self.range_start < 0 or self.range_end <= self.range_start:
            raise SegmentError(
                f"段范围必须为正长度且非负起点: [{self.range_start},{self.range_end})"
            )
        if not 1 <= self.chunk_size <= MAX_CHUNK_SIZE_BYTES:
            raise SegmentError(
                f"块大小必须在 1~{MAX_CHUNK_SIZE_BYTES} 字节内: {self.chunk_size!r}"
            )


@dataclass(frozen=True)
class SegmentResult:
    """一次段传输的实际结果。

    processed_end 是已确认写入的连续范围末尾（不含），不是可靠进度。
    write_extent_known 为 False 时，异常写入可能还有未确认的效果。
    synced 只表示段尾同步成功；源结束事实来自会话的独立完成观察。
    source_end 和 source_close_error 均为空表示尚未确认关闭结果，
    所属流程仍须通过 wait_stopped 跟踪收场。
    """

    processed_end: int
    synced: bool
    error: SegmentFailure | None
    failure: Exception | None
    write_extent_known: bool
    source_end: ReadEnd | None
    source_close_error: Exception | None

    @property
    def source_ended(self) -> bool:
        return self.source_end is not None and self.source_end.stopped


class SegmentFailure(StrEnum):
    """分段结果的机器分类；原异常独立保存在 failure。"""

    STOPPED = "stopped"
    NO_DATA = "no_data"
    SOURCE_EOF_EARLY = "source_eof_early"
    READ_FAILED = "read_failed"
    WRITE_FAILED = "write_failed"
    SYNC_FAILED = "sync_failed"


def transfer_segment(
    spec: SegmentSpec, source: ReadSession, target: WritableFile
) -> SegmentResult:
    """按 [C,E) 顺序传输一段源字节到目标。

    块间与段尾同步前检查停止通知；已开始的本地写入与同步等待真实
    结果。源无数据超时由读取会话计量，本函数的本地操作不参与累计。
    """
    if source.position() != spec.range_start:
        raise SegmentError(
            f"会话位置 {source.position()} 与段起点 {spec.range_start} 不符"
        )
    remaining = spec.range_end - spec.range_start
    processed = 0
    synced = False
    write_extent_known = True
    error: SegmentFailure | None = None
    failure: Exception | None = None
    while remaining > 0:
        if spec.stop.is_set():
            error = SegmentFailure.STOPPED
            break
        try:
            chunk = source.read_chunk(min(spec.chunk_size, remaining))
        except Exception as read_failure:
            error, failure = SegmentFailure.READ_FAILED, read_failure
            break
        if chunk.data:
            # 视图切片不复制剩余内容；每次只持有一个源块。
            with memoryview(chunk.data) as pending:
                offset = 0
                while offset < len(pending):
                    if spec.stop.is_set():
                        error = SegmentFailure.STOPPED
                        break
                    try:
                        with pending[offset:] as part:
                            count = target.write(part)
                    except Exception as write_failure:
                        error, failure = SegmentFailure.WRITE_FAILED, write_failure
                        write_extent_known = False
                        break
                    if type(count) is not int or not 0 <= count <= len(pending) - offset:
                        error = SegmentFailure.WRITE_FAILED
                        failure = SegmentError(f"目标返回非法写入数: {count!r}")
                        write_extent_known = False
                        break
                    if count == 0:
                        error = SegmentFailure.WRITE_FAILED
                        failure = SegmentError("目标写入未取得进展")
                        break
                    offset += count
                    processed += count
                    remaining -= count
            if error is not None:
                break
        if chunk.error is not None:
            if chunk.error in (SegmentFailure.STOPPED, SegmentFailure.NO_DATA):
                error = SegmentFailure(chunk.error)
            else:
                error, failure = SegmentFailure.READ_FAILED, ReadError(chunk.error)
            break
        if chunk.eof:
            if remaining > 0:
                error = SegmentFailure.SOURCE_EOF_EARLY
            break
    if error is None and spec.stop.is_set():
        # 段尾同步前停止：字节已写入，可靠进度不推进。
        error = SegmentFailure.STOPPED
    if error is None:
        assert remaining == 0
        try:
            target.sync()
        except Exception as sync_failure:
            error, failure = SegmentFailure.SYNC_FAILED, sync_failure
        else:
            synced = True
    source_end = None
    source_close_error = None
    try:
        source_end = source.poll_stopped()
    except Exception as close_failure:
        source_close_error = close_failure
    return SegmentResult(
        processed_end=spec.range_start + processed,
        synced=synced,
        error=error,
        failure=failure,
        write_extent_known=write_extent_known,
        source_end=source_end,
        source_close_error=source_close_error,
    )
