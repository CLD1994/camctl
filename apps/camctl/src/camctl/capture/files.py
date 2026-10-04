"""设备文件观察登记的输入类型与身份编码。

观察登记把驱动结果列举的稳定文件身份落为 device_files 行：身份
键由观察者动作的固定设备与驱动绑定及驱动文件身份确定编码构成，
全库唯一，重复发现复用原行并核对原绑定与归属。归属确认与文件
形成状态分别经 OWNERSHIP 与 COMPLETE 分支保存，证据结构与登记
的 JSON 枚举一致；预览配对两端属于同一来源任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId, UtcMicros
from camctl.persistence.transaction import encode_json_value

__all__ = [
    "FileCompletionSave",
    "FileObservationSave",
    "ObservationDisposition",
    "ObservationOutcome",
    "OwnershipSave",
    "file_identity_key",
]

#: 归属证据、完成依据与配对证据的方法编号（file-fields.md#设备文件）。
_OWNERSHIP_METHOD = enum_for("device_files.ownership_evidence_json.method")
_COMPLETION_BASIS = enum_for("device_files.completion_evidence_json.basis")
_PAIRING_METHOD = enum_for("device_files.pairing_evidence_json.method")
_FILE_ROLE = enum_for("device_files.role")
_COMPLETION_STATE = enum_for("device_files.completion_state")

_TASK_SCOPE = int(_OWNERSHIP_METHOD.TASK_SCOPE)
_BASELINE_DIFFERENCE = int(_OWNERSHIP_METHOD.BASELINE_DIFFERENCE)
_DRIVER_TASK_ASSOCIATION = int(_OWNERSHIP_METHOD.DRIVER_TASK_ASSOCIATION)
_DEVICE_GUARANTEE = int(_COMPLETION_BASIS.DEVICE_GUARANTEE)
_TIME_AND_OUTPUTS = int(_COMPLETION_BASIS.TIME_AND_OUTPUTS)
_DRIVER_PAIRING = int(_PAIRING_METHOD.DRIVER_PAIRING)
_ORIGINAL = int(_FILE_ROLE.ORIGINAL)
_PREVIEW = int(_FILE_ROLE.PREVIEW)
_WRITING = int(_COMPLETION_STATE.WRITING)
_COMPLETE = int(_COMPLETION_STATE.COMPLETE)
_UNCONFIRMED = int(_COMPLETION_STATE.UNCONFIRMED)


def file_identity_key(device_id: str, driver_id: str, file_identity: str) -> str:
    """[设备, 驱动, 驱动文件身份] 的确定 JSON 编码，全库唯一。"""
    for name, value in (
        ("device_id", device_id),
        ("driver_id", driver_id),
        ("file_identity", file_identity),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} 必须是非空文本: {value!r}")
    return encode_json_value([device_id, driver_id, file_identity])


def _identity(name: str, value: int | None) -> None:
    if value is not None:
        ObjectId(value)


def _member(enum, name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是登记的整数编号: {value!r}")
    try:
        return int(enum(value))
    except ValueError as error:
        raise ValueError(f"{name} 不属于 {enum.__name__}: {value!r}") from error


def _mapping(name: str, value: Mapping[str, Any] | None, *, required: bool) -> None:
    if value is None:
        if required:
            raise ValueError(f"{name} 不能为空")
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 必须是对象: {value!r}")


def _text(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{name} 必须是非空文本或空: {value!r}")


def _timestamp(value: int) -> None:
    UtcMicros(value)


@dataclass(frozen=True)
class FileObservationSave:
    """一次设备文件发现的登记输入（DEVICE_FILE_OBSERVED.CREATE）。

    locator 是驱动声明的定位结构；original_name 与 media_type 是实
    际取得的可读元信息，未知时为空，不由扩展名猜测内容类型。
    """

    observer_action_id: int
    file_identity: str
    locator: Mapping[str, Any]
    occurred_at: int
    original_name: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        ObjectId(self.observer_action_id)
        if not isinstance(self.file_identity, str) or not self.file_identity:
            raise ValueError(f"驱动文件身份必须是非空文本: {self.file_identity!r}")
        _mapping("文件定位结构", self.locator, required=True)
        if not self.locator:
            raise ValueError("文件定位结构必须是非空对象")
        _text("original_name", self.original_name)
        _text("media_type", self.media_type)
        _timestamp(self.occurred_at)


@dataclass(frozen=True)
class OwnershipSave:
    """一次归属确认的保存输入（DEVICE_FILE_OBSERVED.OWNERSHIP）。

    来源只能从未知变为可靠确认；差集证据另填 activity_id；预览必
    须携带配对证据与原片文件身份，原片与未知用途不携带配对。
    """

    file_id: int
    source_action_id: int
    method: int
    role: int
    observation: Mapping[str, Any]
    occurred_at: int
    activity_id: int | None = None
    paired_device_file_id: int | None = None
    pairing_observation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        ObjectId(self.source_action_id)
        _member(_OWNERSHIP_METHOD, "归属证据方法", self.method)
        role = _member(_FILE_ROLE, "文件用途", self.role)
        if role not in (_ORIGINAL, _PREVIEW):
            raise ValueError(f"归属确认的用途必须是原片或预览: {self.role!r}")
        _mapping("归属观察依据", self.observation, required=True)
        _timestamp(self.occurred_at)
        if self.method == _BASELINE_DIFFERENCE:
            if self.activity_id is None:
                raise ValueError("固定基准差集证据必须填写 activity_id")
            ObjectId(self.activity_id)
        elif self.activity_id is not None:
            raise ValueError("只有固定基准差集证据填写 activity_id")
        if role == _PREVIEW:
            if self.paired_device_file_id is None:
                raise ValueError("预览配对必须携带原片文件身份")
            ObjectId(self.paired_device_file_id)
            _mapping("配对观察依据", self.pairing_observation, required=True)
        elif self.paired_device_file_id is not None or self.pairing_observation is not None:
            raise ValueError("原片或未知用途文件不携带配对")

    def ownership_evidence(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "method": self.method,
            "observation": dict(self.observation),
        }
        if self.method == _BASELINE_DIFFERENCE:
            document["activity_id"] = self.activity_id
        return document

    def pairing_evidence(self) -> dict[str, Any] | None:
        if self.paired_device_file_id is None:
            return None
        return {
            "method": _DRIVER_PAIRING,
            "observation": dict(self.pairing_observation or {}),
        }


@dataclass(frozen=True)
class FileCompletionSave:
    """一次文件形成状态保存（DEVICE_FILE_OBSERVED.COMPLETE）。

    COMPLETE 携带完整大小与完成依据，TIME_AND_OUTPUTS 依据引用已
    保存的等待完成事实；WRITING 只携带仍在形成的观察证据；UNCON-
    FIRMED 携带实际失败证据。文件已经完成后不得倒退。
    """

    file_id: int
    state: int
    occurred_at: int
    basis: int | None = None
    observation: Mapping[str, Any] | None = None
    size_bytes: int | None = None
    activity_id: int | None = None
    wait_completed_event_id: int | None = None
    error: Mapping[str, Any] | None = None
    locator: Mapping[str, Any] | None = None
    original_name: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        state = _member(_COMPLETION_STATE, "文件形成状态", self.state)
        if state not in (_WRITING, _COMPLETE, _UNCONFIRMED):
            raise ValueError(f"文件形成状态保存不接受该分区: {self.state!r}")
        _timestamp(self.occurred_at)
        if state == _COMPLETE:
            basis = _member(_COMPLETION_BASIS, "完成依据", self.basis or 0)
            if basis not in (_DEVICE_GUARANTEE, _TIME_AND_OUTPUTS):
                raise ValueError(f"完成依据不合法: {self.basis!r}")
            _mapping("完成观察依据", self.observation, required=True)
            if (isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int)
                    or self.size_bytes < 0):
                raise ValueError(f"完整大小必须是非负整数: {self.size_bytes!r}")
            if basis == _TIME_AND_OUTPUTS:
                if self.activity_id is None or self.wait_completed_event_id is None:
                    raise ValueError("等待与产物契约依据必须引用任务及等待完成事件")
                ObjectId(self.activity_id)
                ObjectId(self.wait_completed_event_id)
            elif self.activity_id is not None or self.wait_completed_event_id is not None:
                raise ValueError("只有等待与产物契约依据引用任务及等待完成事件")
            if self.error is not None:
                raise ValueError("完成状态不携带失败证据")
        else:
            if self.size_bytes is not None:
                raise ValueError("只有完成状态携带完整大小")
            if self.activity_id is not None or self.wait_completed_event_id is not None:
                raise ValueError("只有等待与产物契约依据引用任务及等待完成事件")
            if state == _WRITING:
                if self.basis is not None:
                    if _member(_COMPLETION_BASIS, "形成依据", self.basis) != _DEVICE_GUARANTEE:
                        raise ValueError(f"仍在形成状态只携带设备保证依据: {self.basis!r}")
                    _mapping("形成观察依据", self.observation, required=True)
                elif self.observation is not None:
                    raise ValueError("形成观察依据必须与形成依据同时提供")
                if self.error is not None:
                    raise ValueError("仍在形成状态不携带失败证据")
            else:
                if self.basis is not None or self.observation is not None:
                    raise ValueError("未确认状态不携带完成或形成依据")
                _mapping("实际失败证据", self.error, required=True)
        _mapping("文件定位结构", self.locator, required=False)
        _text("original_name", self.original_name)
        _text("media_type", self.media_type)

    def completion_evidence(self) -> dict[str, Any] | None:
        if self.basis is None:
            return None
        document: dict[str, Any] = {
            "basis": self.basis,
            "observation": dict(self.observation or {}),
        }
        if self.basis == _TIME_AND_OUTPUTS:
            document["activity_id"] = self.activity_id
            document["wait_completed_event_id"] = self.wait_completed_event_id
        return document


class ObservationDisposition(Enum):
    """一次观察登记事务的结果分类。"""

    SAVED = "saved"
    #: 原键重送或重复发现：只读复用首次结果或原行。
    ALREADY = "already"


@dataclass(frozen=True)
class ObservationOutcome:
    """一次观察登记事务的保存结果。

    created 只在 CREATE 分支有意义：新登记为真；原键重送恢复首次
    响应，重复发现按原行只读返回为假。
    """

    disposition: ObservationDisposition
    file_id: int
    created: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
