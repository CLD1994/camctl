"""完整目录基准的分批保存与固定输入。"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ObjectId, OperationKey, UtcMicros
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryRequest
from camctl.contracts.pages import Page
from camctl.devices.file_identity import FileIdentity
from camctl.operations.models import ErrorValue

BaselineState = enum_for("device_activities.baseline_state")
BaselineReadState = enum_for("baseline_read_calls.state")


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


@dataclass(frozen=True)
class BaselineReadIntent:
    """实际目录调用之前固定的输入；不表示调用已经派发。"""

    action_id: int
    activity_id: int
    binding: DeviceBinding
    directories: tuple[str, ...]
    cursor: DirectoryCursor | None
    timeout_s: Decimal
    baseline_ref: BaselineRef
    read_no: int
    call_key: OperationKey
    result_save_key: OperationKey
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.action_id)
        ObjectId(self.activity_id)
        ObjectId(self.read_no)
        UtcMicros(self.occurred_at)
        request = self.directory_request()
        object.__setattr__(self, 'directories', request.directories)
        call_key, result_key = OperationKey(self.call_key), OperationKey(self.result_save_key)
        if call_key == result_key:
            raise ValueError('基准调用与结果保存必须使用不同的原操作身份')
        object.__setattr__(self, 'call_key', call_key)
        object.__setattr__(self, 'result_save_key', result_key)
        ref = self.baseline_ref
        if (not isinstance(ref, BaselineRef) or ref.activity_id != self.activity_id
                or ref.state is not BaselineState.COLLECTING):
            raise ValueError('基准调用必须引用原活动正在收集的基准')
        if self.read_no == 1:
            if self.cursor is not None or ref.chunk_count:
                raise ValueError('首次基准调用必须从空前缀及空游标开始')
        elif self.cursor is None:
            raise ValueError('后续基准调用必须保留原成功返回的继续游标')

    def directory_request(self) -> DirectoryRequest:
        """沿用目录端口的规范化与期限契约，批次固定为 128。"""
        return DirectoryRequest(self.binding, self.directories, self.cursor, 128, self.timeout_s)


@dataclass(frozen=True)
class BaselineReadCallView:
    """原目录调用在完整提交边界的有限事实；目录内容由原批次持有。"""

    call_id: int
    intent_event_id: int
    state: BaselineReadState
    request: BaselineReadIntent
    result_event_id: int | None
    returned_at: int | None
    error: ErrorValue | None
    next_cursor: DirectoryCursor | None
    chunk_event_id: int | None
    boundary: HistoryBoundary


@dataclass(frozen=True)
class BaselineReadReturn:
    """一次实际目录返回的基准语义输入；工具诊断由原调用负责。"""

    call_id: int
    returned_at: int
    error: ErrorValue | None
    returned: DirectoryRead

    def __post_init__(self):
        ObjectId(self.call_id)
        UtcMicros(self.returned_at)
        if not isinstance(self.returned, DirectoryRead) or self.error != self.returned.error:
            raise ValueError('基准返回必须保留同一次实际目录读取及错误')
        error = deepcopy(self.error)
        page = self.returned.page
        if page is not None:
            if len(page.items) > 128 or any(not isinstance(item, FileIdentity) for item in page.items):
                raise ValueError('基准返回必须是至多128份实际稳定文件身份')
            page = Page(tuple(page.items), page.next_cursor)
        object.__setattr__(self, 'error', error)
        object.__setattr__(self, 'returned', DirectoryRead(None, error, page))
