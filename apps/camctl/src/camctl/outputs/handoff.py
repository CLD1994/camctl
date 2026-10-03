"""普通交付的本地发布判定、编排与三位置恢复。

副本准备完成后按[普通交付的保存顺序与中断恢复](../../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)
发布：先保存发布意图，事务外原子移动并同步目录，再按实际证据保
存完成事实。恢复按 staging/ready/processing 三位置观察与已保存事
实决策：完整副本继续原身份，交接位置观察到副本补存本地交付事实，
三处均无且结果无法确认时结束为终局未知失败，不自动重投。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import (
    ConsistencyError, MAX_OBJECT_ID, ObjectId, UtcMicros, new_operation_key,
)
from camctl.contracts.workflow_errors import registered_error, validate_error_details
from camctl.host_files.handoff import (
    HandoffDirectories, PublishResult, PublishStage, ReadyName, publish_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.models import BoundDirectories, FileRef
from camctl.outputs.copy import CopyTargetRef
from camctl.persistence.models import DbOutcomeKind

if TYPE_CHECKING:
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.runtime import OwnedConnection

_DELIVERY_STATUS = enum_for("deliveries.status")
_HEX = frozenset("0123456789abcdef")

_HANDOFF_UNCONFIRMED = "delivery_handoff_unconfirmed"


def _require_digest(name: str, value) -> None:
    """摘要必须是 64 位小写十六进制，或明确未知（None）。"""
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} 必须是 64 位小写十六进制摘要: {value!r}")


class HandoffPlace(Enum):
    """交接决策涉及的位置：工作目录与两个主程序接管目录。"""

    STAGING = "staging"
    READY = "ready"
    PROCESSING = "processing"


@dataclass(frozen=True)
class LocationObservation:
    """一个交接位置的一次可靠观察；不可靠时只保留错误。

    present 表达文件是否存在；存在时必须携带同一次观察取得的长度，
    可选携带内容摘要。恢复确认交接位置的副本时必须携带摘要。
    """

    observed: bool
    present: bool | None = None
    length: int | None = None
    sha256: str | None = None
    error: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observed, bool):
            raise ValueError(f"观察可靠标志必须是布尔值: {self.observed!r}")
        if not self.observed:
            if self.present is not None or self.length is not None \
                    or self.sha256 is not None:
                raise ValueError("观察不可靠时不能携带文件事实")
            if self.error is None:
                raise ValueError("观察不可靠时必须携带失败诊断")
            return
        if not isinstance(self.present, bool):
            raise ValueError("可靠观察必须表达文件是否存在")
        if self.present:
            if isinstance(self.length, bool) or not isinstance(self.length, int) \
                    or self.length < 0:
                raise ValueError(f"存在的文件必须携带非负整数长度: {self.length!r}")
        elif self.length is not None or self.sha256 is not None:
            raise ValueError("缺失的文件不能携带长度或摘要")
        _require_digest("sha256", self.sha256)


@dataclass(frozen=True)
class DeliveryLocations:
    """三处交接位置各自的观察；三处都必须给出。"""

    staging: LocationObservation
    ready: LocationObservation
    processing: LocationObservation

    def __post_init__(self) -> None:
        for name in ("staging", "ready", "processing"):
            if not isinstance(getattr(self, name), LocationObservation):
                raise TypeError(
                    f"{name} 位置观察必须使用 LocationObservation:"
                    f" {getattr(self, name)!r}")

    def at(self, place: HandoffPlace) -> LocationObservation:
        return getattr(self, place.value)


@dataclass(frozen=True)
class DeliveryFacts:
    """交接判定所需的已保存事实。

    prepared_size/prepared_sha256 是准备完成事务保存的完整副本事实；
    withdrawal_requested 表达撤回责任已提出，取消等其他收场由所属
    规则处理，不进入普通恢复分支。
    """

    delivery_id: int
    status: int
    publication_intent_event_id: int | None = None
    published_event_id: int | None = None
    prepared_size: int | None = None
    prepared_sha256: str | None = None
    withdrawal_requested: bool = False
    publication_conditions_met: bool = True

    def __post_init__(self) -> None:
        ObjectId(self.delivery_id)
        if isinstance(self.status, bool) or not isinstance(self.status, int) \
                or not 1 <= self.status <= len(_DELIVERY_STATUS):
            raise ValueError(f"交付状态不在登记范围内: {self.status!r}")
        for name in ("publication_intent_event_id", "published_event_id"):
            value = getattr(self, name)
            if value is not None:
                ObjectId(value)
        if isinstance(self.prepared_size, bool) or \
                (self.prepared_size is not None and (
                    not isinstance(self.prepared_size, int) or self.prepared_size < 0)):
            raise ValueError(f"准备记录长度必须是非负整数: {self.prepared_size!r}")
        _require_digest("prepared_sha256", self.prepared_sha256)
        for name in ("withdrawal_requested", "publication_conditions_met"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须是布尔值: {getattr(self, name)!r}")


@dataclass(frozen=True)
class DeliveryFailure:
    """交付最终失败的公共错误诊断；按公共登记构造并校验。"""

    code: str
    stage: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not isinstance(self.stage, str):
            raise ValueError("失败诊断必须携带错误名与阶段")
        spec = registered_error(self.code)
        if self.stage != spec["stage"]:
            raise ValueError(
                f"失败阶段与公共登记不符: {self.stage!r} != {spec['stage']!r}")
        if not isinstance(self.details, Mapping):
            raise ValueError(f"失败详情必须是对象: {self.details!r}")
        validate_error_details(self.code, self.details)

    def as_json(self) -> dict[str, Any]:
        """公开投影使用的错误对象；字段顺序稳定。"""
        return {"code": self.code, "stage": self.stage, "details": dict(self.details)}


class DeliveryHandoffOutcome(Enum):
    """交接判定的分区；与中断恢复决策表一一对应。"""

    COMPLETED = "completed"
    DELIVERED_LOCALLY = "delivered_locally"
    PUBLISH = "publish"
    HOLD = "hold"
    UNCONFIRMED_FINAL = "unconfirmed_final"
    UNDECIDABLE = "undecidable"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True)
class DeliveryDecision:
    """一次交接判定；republishes 表达本判定安排的自动重投次数。"""

    outcome: DeliveryHandoffOutcome
    republishes: int = 0
    failure: DeliveryFailure | None = None
    error: object | None = None
    observed_location: HandoffPlace | None = None


def _unconfirmed_failure(delivery_id: int) -> DeliveryFailure:
    return DeliveryFailure(
        code=_HANDOFF_UNCONFIRMED,
        stage=registered_error(_HANDOFF_UNCONFIRMED)["stage"],
        details={"delivery_id": str(delivery_id)},
    )


def _undecidable(reason: str) -> DeliveryDecision:
    return DeliveryDecision(
        outcome=DeliveryHandoffOutcome.UNDECIDABLE, error=reason)


def _confirm_prepared_copy(
    facts: DeliveryFacts, observation: LocationObservation, place: HandoffPlace,
) -> bool:
    """按准备记录核对观察到的副本；不符或证据不足保留诊断。"""
    assert facts.prepared_size is not None
    if observation.length != facts.prepared_size:
        return False
    if observation.sha256 is None or observation.sha256 != facts.prepared_sha256:
        return False
    return True


def decide_handoff(facts: DeliveryFacts, files: DeliveryLocations) -> DeliveryDecision:
    """按已保存事实与三位置观察决定普通交付的交接处理。

    已有可靠完成事实保留成功；交接位置观察到同一完整副本时补存
    本地交付事实；完整副本仍在 staging 时继续原身份发布；三处均
    无且无完成事实时结束为终局未知失败，不自动重投。观察失败或
    证据矛盾保留诊断，不套用缺失分支，也不猜测成功。
    """
    if not isinstance(facts, DeliveryFacts):
        raise TypeError(f"交接判定必须使用 DeliveryFacts: {facts!r}")
    if not isinstance(files, DeliveryLocations):
        raise TypeError(f"交接判定必须使用 DeliveryLocations: {files!r}")
    status = facts.status
    if status == int(_DELIVERY_STATUS.PUBLISHED):
        return DeliveryDecision(outcome=DeliveryHandoffOutcome.COMPLETED)
    if status in (
        int(_DELIVERY_STATUS.FAILED),
        int(_DELIVERY_STATUS.CANCELED),
        int(_DELIVERY_STATUS.WITHDRAWN),
    ) or facts.withdrawal_requested:
        return DeliveryDecision(outcome=DeliveryHandoffOutcome.NOT_ACTIVE)
    if status in (
        int(_DELIVERY_STATUS.PENDING), int(_DELIVERY_STATUS.PREPARING),
    ):
        raise ConsistencyError(
            f"交付 {facts.delivery_id} 尚未准备完成，不属于交接判定: {status}")
    if facts.published_event_id is not None:
        raise ConsistencyError("交付保存了完成事件但状态未进入 PUBLISHED")
    if facts.prepared_size is None or facts.prepared_sha256 is None:
        raise ConsistencyError("已准备交付缺少完整副本的准备记录")
    for place in HandoffPlace:
        if not files.at(place).observed:
            return _undecidable(
                f"{place.value} 观察不可靠: {files.at(place).error!r}")
    delivered = [
        place for place in (HandoffPlace.READY, HandoffPlace.PROCESSING)
        if files.at(place).present
    ]
    if len(delivered) > 1:
        return _undecidable("交接位置同时存在多份副本")
    if delivered:
        place = delivered[0]
        if not _confirm_prepared_copy(facts, files.at(place), place):
            return _undecidable(
                f"{place.value} 副本与准备记录不符，不能确认同一完整副本")
        return DeliveryDecision(
            outcome=DeliveryHandoffOutcome.DELIVERED_LOCALLY,
            observed_location=place,
        )
    staging = files.at(HandoffPlace.STAGING)
    if staging.present:
        if staging.length != facts.prepared_size:
            return _undecidable("staging 副本长度与准备记录不符")
        if staging.sha256 is not None and staging.sha256 != facts.prepared_sha256:
            return _undecidable("staging 副本摘要与准备记录不符")
        if not facts.publication_conditions_met:
            return DeliveryDecision(outcome=DeliveryHandoffOutcome.HOLD)
        return DeliveryDecision(outcome=DeliveryHandoffOutcome.PUBLISH)
    return DeliveryDecision(
        outcome=DeliveryHandoffOutcome.UNCONFIRMED_FINAL,
        failure=_unconfirmed_failure(facts.delivery_id),
    )


# ---- 仓储输入与结果 ----


def _require_timestamp(occurred_at: int) -> None:
    timestamp = UtcMicros(occurred_at)
    if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
        raise ValueError(f"事实时刻超出 SQLite 整数范围: {occurred_at!r}")


@dataclass(frozen=True)
class PublicationIntentRequest:
    """一次发布意图的保存申请。"""

    delivery_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.delivery_id)
        _require_timestamp(self.occurred_at)


@dataclass(frozen=True)
class PublicationSaveRequest:
    """一次本地交付完成事实的保存申请。"""

    delivery_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.delivery_id)
        _require_timestamp(self.occurred_at)


@dataclass(frozen=True)
class UnconfirmedFailureSave:
    """交接结果无法确认的终局失败保存申请。"""

    delivery_id: int
    failure: DeliveryFailure
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.delivery_id)
        if not isinstance(self.failure, DeliveryFailure):
            raise TypeError("终局失败申请必须携带 DeliveryFailure")
        if self.failure.details.get("delivery_id") != str(self.delivery_id):
            raise ValueError("终局失败详情必须关联本次交付")
        _require_timestamp(self.occurred_at)


class IntentDisposition(Enum):
    """意图保存事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class IntentSaveOutcome:
    """意图保存事务的返回；SKIPPED 表示取消或不在执行。"""

    disposition: IntentDisposition
    reason: str | None = None


class PublicationDisposition(Enum):
    """完成事实保存事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class PublicationSaveOutcome:
    """完成事实保存事务的返回。"""

    disposition: PublicationDisposition


class FailureSaveDisposition(Enum):
    """终局失败保存事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class FailureSaveOutcome:
    """终局失败保存事务的返回。"""

    disposition: FailureSaveDisposition


@dataclass(frozen=True)
class DeliveryStateFacts:
    """仓储加载的交付固定事实、交接定位与发起动作。"""

    facts: DeliveryFacts
    file_name: str
    target: CopyTargetRef
    action_id: int


# ---- 发布编排 ----


class DeliveryHandoffError(RuntimeError):
    """普通交付发布失败；保留实际阶段，不猜测未确认事实。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


_OWNER_ERROR_STAGES = {"cancel_requested": "owner_canceled",
                       "owner_not_running": "owner_not_running"}


def _owner_stage(reason: str | None) -> str:
    return _OWNER_ERROR_STAGES.get(reason or "", "owner_not_running")


@dataclass(frozen=True)
class DeliveryDirectories:
    """一次交付交接涉及的三个实际目录。"""

    staging: Path
    ready: Path
    processing: Path


@dataclass(frozen=True)
class DeliveryContext:
    """发布与恢复的协作者、目录、发布条件与事实时刻。"""

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    directories: DeliveryDirectories
    occurred_at: int
    publication_conditions_met: bool = True


class DeliveryPhase(Enum):
    """一次发布或恢复编排的出口。"""

    PUBLISHED = "published"
    FAILED_FINAL = "failed_final"
    HELD = "held"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True)
class DeliveryResult:
    """编排结果；decision 保留判定依据。"""

    phase: DeliveryPhase
    decision: DeliveryDecision


def _stat_location(path: Path) -> LocationObservation:
    """只核对存在与长度；staging 是自有位置，不重算内容摘要。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return LocationObservation(observed=True, present=False)
    except OSError as failure:
        return LocationObservation(observed=False, error=failure)
    return LocationObservation(
        observed=True, present=True, length=info.st_size)


def _digest_location(path: Path) -> LocationObservation:
    """交接位置的完整观察：存在、长度与内容摘要。"""
    observed = _stat_location(path)
    if not observed.observed or not observed.present:
        return observed
    assert observed.length is not None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as failure:
        return LocationObservation(observed=False, error=failure)
    return LocationObservation(
        observed=True, present=True, length=observed.length,
        sha256=digest.hexdigest())


def _observe_locations(
    state: DeliveryStateFacts, directories: DeliveryDirectories,
) -> DeliveryLocations:
    """三位置观察：staging 按工作路径，交接位置按交付文件名。"""
    staging = _stat_location(
        directories.staging / state.target.relative_path)
    ready = _digest_location(directories.ready / state.file_name)
    processing = _digest_location(directories.processing / state.file_name)
    return DeliveryLocations(
        staging=staging, ready=ready, processing=processing)


def _require_completed(outcome, stage_failed: str, stage_unknown: str) -> None:
    """仓储结果不是完成时按分区保留阶段诊断。"""
    if outcome.kind is DbOutcomeKind.COMPLETED:
        return
    raise DeliveryHandoffError(
        stage_unknown if outcome.kind is DbOutcomeKind.UNKNOWN else stage_failed,
        str(outcome.error),
    )


def _staging_ref(
    state: DeliveryStateFacts, directories: DeliveryDirectories,
) -> tuple[FileRef, BoundDirectories, HandoffDirectories]:
    ref = FileRef(
        file_id=state.target.file_id, purpose=state.target.purpose,
        relative_path=state.target.relative_path,
        root=directories.staging,
    )
    return (
        ref,
        BoundDirectories(staging=directories.staging),
        HandoffDirectories(staging=directories.staging, ready=directories.ready),
    )


def _save_intent(delivery_id: int, context: DeliveryContext) -> IntentSaveOutcome:
    """保存发布意图；SKIPPED 表示发起责任不再执行。"""
    outcome = context.repository.save_publication_intent(
        PublicationIntentRequest(
            delivery_id=delivery_id, occurred_at=context.occurred_at),
        new_operation_key(), context.owned,
    )
    _require_completed(outcome, "intent_save_failed", "intent_save_unknown")
    result = outcome.value
    if result.disposition is IntentDisposition.SKIPPED:
        raise DeliveryHandoffError(
            _owner_stage(result.reason), "发起责任不再执行")
    return result


def _save_publication(delivery_id: int, context: DeliveryContext) -> None:
    """保存本地交付完成事实；确认已发生的外部交接结果。"""
    outcome = context.repository.save_publication(
        PublicationSaveRequest(
            delivery_id=delivery_id, occurred_at=context.occurred_at),
        new_operation_key(), context.owned,
    )
    _require_completed(
        outcome, "publication_save_failed", "publication_save_unknown")


def _save_unconfirmed(
    delivery_id: int, failure: DeliveryFailure, context: DeliveryContext,
) -> None:
    """保存终局未知失败；不安排自动重投。"""
    outcome = context.repository.save_unconfirmed_failure(
        UnconfirmedFailureSave(
            delivery_id=delivery_id, failure=failure,
            occurred_at=context.occurred_at),
        new_operation_key(), context.owned,
    )
    _require_completed(
        outcome, "unconfirmed_save_failed", "unconfirmed_save_unknown")


def _decide_again(
    state: DeliveryStateFacts, context: DeliveryContext,
) -> DeliveryDecision:
    """观察三位置并按上下文发布条件判定；观察是只读的，可安全重复。"""
    locations = _observe_locations(state, context.directories)
    facts = state.facts
    if not context.publication_conditions_met:
        facts = replace(facts, publication_conditions_met=False)
    return decide_handoff(facts, locations)


async def publish_delivery(
    delivery_id: int, context: DeliveryContext,
) -> DeliveryResult:
    """发布或恢复一份普通交付，并返回可观察结果。

    依据三位置观察决定处理：staging 完整副本按保存意图、原子移动、
    目录同步、保存完成事实的顺序发布；交接位置观察到副本或已有
    完成事实时只保存本地事实；三处均无且无完成事实时结束为终局
    未知失败，不自动重投。移动结果未知时不猜测，保留诊断等待
    下一次观察。
    """
    state = context.repository.load_delivery_state(delivery_id, context.owned)
    decision = _decide_again(state, context)
    if decision.outcome is DeliveryHandoffOutcome.COMPLETED:
        return DeliveryResult(DeliveryPhase.PUBLISHED, decision)
    if decision.outcome is DeliveryHandoffOutcome.NOT_ACTIVE:
        return DeliveryResult(DeliveryPhase.NOT_ACTIVE, decision)
    if decision.outcome is DeliveryHandoffOutcome.HOLD:
        return DeliveryResult(DeliveryPhase.HELD, decision)
    if decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE:
        raise DeliveryHandoffError(
            "handoff_undecidable", str(decision.error))
    if decision.outcome is DeliveryHandoffOutcome.DELIVERED_LOCALLY:
        _save_publication(delivery_id, context)
        return DeliveryResult(DeliveryPhase.PUBLISHED, decision)
    if decision.outcome is DeliveryHandoffOutcome.UNCONFIRMED_FINAL:
        assert decision.failure is not None
        _save_unconfirmed(delivery_id, decision.failure, context)
        return DeliveryResult(DeliveryPhase.FAILED_FINAL, decision)
    _save_intent(delivery_id, context)
    ref, roots, directories = _staging_ref(state, context.directories)
    publish = await publish_file(ref, roots, directories, ReadyName(state.file_name))
    if publish.stage is PublishStage.MOVED:
        if publish.directory in (
            DirectorySyncStage.SYNCED, DirectorySyncStage.UNSUPPORTED,
        ):
            _save_publication(delivery_id, context)
            return DeliveryResult(DeliveryPhase.PUBLISHED, decision)
        # 已移动事实保留：同步失败不撤销移动，也不保存完成事实。
        raise DeliveryHandoffError(
            "publication_sync_failed", publish.error or "目录同步失败")
    if publish.stage is PublishStage.NOT_MOVED:
        retry = _decide_again(state, context)
        if retry.outcome is DeliveryHandoffOutcome.DELIVERED_LOCALLY:
            _save_publication(delivery_id, context)
            return DeliveryResult(DeliveryPhase.PUBLISHED, retry)
        if retry.outcome is DeliveryHandoffOutcome.UNCONFIRMED_FINAL:
            assert retry.failure is not None
            _save_unconfirmed(delivery_id, retry.failure, context)
            return DeliveryResult(DeliveryPhase.FAILED_FINAL, retry)
        raise DeliveryHandoffError("publish_move_failed", publish.error or "")
    raise DeliveryHandoffError(
        "publication_move_unknown", publish.error or "移动结果未知")
