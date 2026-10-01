"""段内小块传输与源无数据观察。

按 [C,E) 顺序逐块读写：每次请求不超过段剩余量，数据在线程内及时
写入目标，不把整段攒入内存。块之间与段尾同步前停止新增普通操作；
正在进行的本地调用等待真实结果并保留实际阶段。目标写入与同步不
参与源无数据等待的计量；源错误按会话分类透传。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from camctl.devices.read_session import ReadSession

__all__ = [
    "DEFAULT_CHUNK_SIZE_BYTES",
    "MAX_CHUNK_SIZE_BYTES",
    "SegmentError",
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
    """受约束目标端口：顺序写入与显式同步。"""

    def write(self, data: bytes) -> int: ...

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

    processed_end 是已写入目标的源偏移（不含）；synced 只在段尾同步
    真实成功后为 True。source_ended 表示源读取已实际结束（EOF 或错
    误结束），与段是否完成无关。
    """

    processed_end: int
    synced: bool
    error: str | None
    source_ended: bool


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
    source_ended = False
    error: str | None = None
    while remaining > 0:
        if spec.stop.is_set():
            error = "stopped"
            break
        chunk = source.read_chunk(min(spec.chunk_size, remaining))
        if chunk.data:
            try:
                target.write(chunk.data)
            except Exception as failure:  # 目标错误保留已写字节与阶段。
                error = _describe("write_failed", failure)
                break
            processed += len(chunk.data)
            remaining -= len(chunk.data)
        if chunk.error is not None:
            source_ended = True
            error = chunk.error
            break
        if chunk.eof:
            source_ended = True
            if remaining > 0:
                error = "source_eof_early"
            break
    if error is None and spec.stop.is_set():
        # 段尾同步前停止：字节已写入，可靠进度不推进。
        error = "stopped"
    if error is None:
        assert remaining == 0
        try:
            target.sync()
        except Exception as failure:
            error = _describe("sync_failed", failure)
        else:
            synced = True
    return SegmentResult(
        processed_end=spec.range_start + processed,
        synced=synced,
        error=error,
        source_ended=source_ended,
    )


def _describe(kind: str, failure: Exception) -> str:
    return f"{kind}: {type(failure).__name__}: {failure}"
