"""设备操作证据的类型登记与校验（单一来源）。

证据契约声明类型、版本、适用操作、结构及身份指向；生产驱动与
测试替身共用同一登记。观察只保留影响执行、恢复或结果解释的必
要事实，未知类型、未知版本、操作不匹配及身份错误分别拒绝，不
推定空文件、已停止或无效果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from camctl.contracts.values import MAX_OBJECT_ID, MIN_OBJECT_ID

__all__ = [
    "DeviceObservation",
    "EvidenceContract",
    "EvidenceError",
    "EvidenceRegistry",
    "RESULT_LISTED_TYPE",
    "RESULT_LISTED_VERSION",
    "RESULT_FILES_CONTRACT",
    "RESULT_PAGE_CONTRACT",
    "validate_observation",
]

#: 证据 data 中身份成员的规范十进制写法。
_CANONICAL_IDENTITY = re.compile(r"\A[1-9][0-9]*\Z")

#: 第一版登记的操作类别；观察与所属操作的契约必须一致。
OPERATIONS = frozenset(
    {"control", "stop", "query", "result", "read", "digest", "delete"}
)


class EvidenceError(ValueError):
    """观察不符合所属证据契约；分区见消息，不以默认值代替。"""


@dataclass(frozen=True)
class EvidenceContract:
    """一种设备操作证据的类型化契约。

    fields 是 data 的全部合法成员（不接受未定义成员）；identity_field
    为空表示该证据不携带目标身份（沿原关联读取）；max_observations
    是本契约在一次结果中允许的观察数量上限，必须为正。
    """

    type: str
    version: int
    operation: str
    fields: frozenset[str]
    identity_field: str | None = None
    max_observations: int = 1

    def __post_init__(self) -> None:
        if not self.type or not isinstance(self.type, str):
            raise EvidenceError(f"证据类型必须是非空标识: {self.type!r}")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise EvidenceError(f"证据版本必须是正整数: {self.version!r}")
        if self.operation not in OPERATIONS:
            raise EvidenceError(f"证据操作类别未登记: {self.operation!r}")
        if not isinstance(self.fields, frozenset):
            raise EvidenceError("fields 必须是 frozenset")
        if self.max_observations < 1:
            raise EvidenceError("观察数量上限必须为正整数")
        if self.identity_field is not None and self.identity_field not in self.fields:
            raise EvidenceError("身份成员必须属于 fields")


RESULT_LISTED_TYPE = "result_files_listed"
RESULT_LISTED_VERSION = 1
RESULT_FILES_CONTRACT = EvidenceContract(
    RESULT_LISTED_TYPE, RESULT_LISTED_VERSION, "result",
    frozenset({"activity_id", "entries"}), identity_field="activity_id")
RESULT_PAGE_CONTRACT = EvidenceContract(
    RESULT_LISTED_TYPE, 2, "result",
    frozenset({"activity_id", "entries", "cursor", "next_cursor", "set_finalized", "completion_evidence"}),
    identity_field="activity_id")


@dataclass(frozen=True)
class DeviceObservation:
    """一次实际取得的设备观察：类型化对象，与调用结果分开。"""

    type: str
    version: int
    data: Mapping[str, Any]


def validate_observation(
    value: DeviceObservation,
    contract: EvidenceContract,
    expected_identity: str | None = None,
) -> None:
    """按契约校验观察；不满足时抛 EvidenceError，不返回降级结果。

    expected_identity 提供时，观察携带的身份必须与操作目标一致；
    已取得可靠事实后的调用错误由结果层同时保留，本函数只校验观
    察本身。
    """
    if value.type != contract.type:
        raise EvidenceError(f"观察类型与契约不符: {value.type!r}")
    if value.version != contract.version:
        raise EvidenceError(
            f"证据版本未知: {value.type!r} v{value.version!r}"
        )
    extra = set(value.data) - contract.fields
    if extra:
        raise EvidenceError(f"证据包含未定义成员: {sorted(extra)!r}")
    if contract.identity_field is not None:
        raw = value.data.get(contract.identity_field)
        if not isinstance(raw, str) or not _CANONICAL_IDENTITY.match(raw):
            raise EvidenceError(f"证据身份缺失或写法不规范: {raw!r}")
        identity = int(raw)
        if not MIN_OBJECT_ID <= identity <= MAX_OBJECT_ID:
            raise EvidenceError(f"证据身份超出范围: {raw!r}")
        if (
            expected_identity is not None
            and raw != expected_identity
        ):
            raise EvidenceError(
                f"观察身份与操作目标不符: {raw!r} != {expected_identity!r}"
            )


class EvidenceRegistry:
    """证据契约登记：同一登记供生产验证与测试替身使用。"""

    def __init__(self, contracts: Sequence[EvidenceContract]) -> None:
        self._contracts: dict[tuple[str, int], EvidenceContract] = {}
        for contract in contracts:
            key = (contract.type, contract.version)
            if key in self._contracts:
                raise EvidenceError(f"证据契约重复登记: {key!r}")
            self._contracts[key] = contract

    def contract(self, type_: str, version: int) -> EvidenceContract:
        key = (type_, version)
        found = self._contracts.get(key)
        if found is None:
            raise EvidenceError(f"证据契约未登记: {key!r}")
        return found

    def with_contract(self, contract: EvidenceContract) -> EvidenceRegistry:
        """保留原登记，仅在新登记中替换或加入指定类型和版本。"""
        contracts = dict(self._contracts)
        contracts[(contract.type, contract.version)] = contract
        return EvidenceRegistry(tuple(contracts.values()))
