"""完整目录基准的分批保存与固定输入。"""
from dataclasses import dataclass

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId, UtcMicros
from camctl.devices.file_identity import FileIdentity

BaselineState = enum_for("device_activities.baseline_state")


def _count(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("基准计数必须是非负整数")


@dataclass(frozen=True)
class BaselineChunkSave:
    activity_id: int
    chunk_no: int
    entries: tuple[FileIdentity, ...]
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.activity_id)
        ObjectId(self.chunk_no)
        UtcMicros(self.occurred_at)
        entries = tuple(self.entries)
        if not 1 <= len(entries) <= 128 or any(not isinstance(entry, FileIdentity) for entry in entries):
            raise ValueError("每个基准批次必须包含 1—128 个稳定文件身份")
        if any(a.sort_key >= b.sort_key for a, b in zip(entries, entries[1:])):
            raise ValueError("基准身份必须严格有序且无重复")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True)
class BaselineFixSave:
    activity_id: int
    chunk_count: int
    entry_count: int
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.activity_id)
        UtcMicros(self.occurred_at)
        _count(self.chunk_count)
        _count(self.entry_count)
        if bool(self.chunk_count) != bool(self.entry_count):
            raise ValueError("基准批次和条目必须共同为零或共同为正")


@dataclass(frozen=True)
class BaselineChunkResult:
    activity_id: int
    chunk_no: int
    event_id: int


@dataclass(frozen=True)
class BaselineChunk:
    event_id: int
    chunk_no: int
    entries: tuple[FileIdentity, ...]


@dataclass(frozen=True)
class BaselineRef:
    activity_id: int
    state: BaselineState
    first_event_id: int | None
    last_event_id: int | None
    chunk_count: int
    entry_count: int

    def __post_init__(self):
        ObjectId(self.activity_id)
        if not isinstance(self.state, BaselineState) or self.state not in (BaselineState.COLLECTING, BaselineState.FIXED):
            raise ValueError("基准引用必须明确处于收集或固定状态")
        _count(self.chunk_count)
        _count(self.entry_count)
        if bool(self.chunk_count) != bool(self.entry_count):
            raise ValueError("基准引用计数不一致")
        if (self.first_event_id is None) != (self.last_event_id is None):
            raise ValueError("基准首尾引用必须共同存在或共同为空")
        if self.first_event_id is not None:
            ObjectId(self.first_event_id)
            ObjectId(self.last_event_id)
            if self.first_event_id > self.last_event_id or not self.chunk_count:
                raise ValueError("非空基准范围与计数不一致")
        elif self.chunk_count:
            raise ValueError("空范围不能携带非零计数")
