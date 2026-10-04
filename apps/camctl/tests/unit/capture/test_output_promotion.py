"""修复成品提升的 output 守卫分支：提升只因同事务登记发生。

REPAIRED 登记核对承载文件尚未提升且修复成功事实齐备；LIFECYCLE
推进到 PROMOTED 要求同事务先行的登记行。释放与交接的保留变化不
受配对核对约束。
"""

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import validators
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.transaction import event_envelope, row_change, update_change

_LIFECYCLE_EVENT = 26
_LIFECYCLE_REASON = 2
_REGISTERED_EVENT = 20


@pytest.fixture
def output_guard(monkeypatch):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    return validators.NAMED_GUARDS["output"]


def _states():
    """REPAIRED 登记与提升共用的最小事实集合。"""
    return {
        "actions": {
            1: {"id": 1, "type": 2, "device_id": "cam", "driver_id": "driver",
                "status": 3},
        },
        "device_files": {
            9: {"id": 9, "source_action_id": 1, "observer_action_id": 1,
                "ownership_evidence_json": {}, "role": 2, "completion_state": 3,
                "original_device_file_id": None, "pairing_evidence_json": None},
        },
        "intermediate_files": {
            21: {"id": 21, "owner_action_id": 1, "owner_delivery_id": None,
                 "purpose": 4, "retention_state": 1, "cleanup_state": 1,
                 "size_bytes": 2048, "sha256": "a" * 64},
        },
        "recording_processing": {
            5: {"id": 5, "action_id": 1, "repair_state": 5,
                "repair_output_file_id": 21},
        },
        "outputs": {
            9: {"id": 9, "kind": 1, "source_action_id": 1, "device_file_id": 9,
                "intermediate_file_id": None},
        },
        "output_origins": {},
    }


def _registered_event():
    """OUTPUT_REGISTERED.REPAIRED：中间文件 21 承载，引用原片产物 9。"""
    rows = (
        row_change("outputs", 10, {
            "source_action_id": 1, "kind": 2, "device_file_id": None,
            "intermediate_file_id": 21}),
        row_change("output_origins", 8, {
            "output_id": 10, "original_output_id": 9}),
    )
    return event_envelope(1, 1, _REGISTERED_EVENT, 2, rows, 1)


def _registered_context(states):
    return EventContext(
        TransactionRange(1, 1, 1), {}, states,
        read_coverage=ReadCoverage({
            ("output_origins", "output_id"): frozenset({9, 10}),
            ("output_origins", "original_output_id"): frozenset({9}),
        }))


def _promotion_event(after_retention: int = 3):
    """INTERMEDIATE_FILE_CHANGED.LIFECYCLE：中间文件 21 的保留推进。"""
    row = update_change(
        "intermediate_files", 21,
        {"retention_state": 1}, {"retention_state": after_retention})
    return event_envelope(1, 1, _LIFECYCLE_EVENT, _LIFECYCLE_REASON, (row,), 1)


# ---- REPAIRED 登记资格 ----


def test_registered_repaired_accepts_promotable_file(output_guard) -> None:
    """未提升的完整修复输出配修复成功事实：登记可以进入。"""
    output_guard(_registered_event(), _registered_context(_states()))


@pytest.mark.parametrize("retention", [2, 3, 4])
def test_registered_repaired_rejects_non_required_file(
    output_guard, retention: int,
) -> None:
    """已释放、已提升或已交接的文件不再有首次提升资格。"""
    states = _states()
    states["intermediate_files"][21]["retention_state"] = retention
    with pytest.raises(EventValidationError):
        output_guard(_registered_event(), _registered_context(states))


def test_registered_repaired_rejects_missing_processing(output_guard) -> None:
    """没有处理记录就不能宣称修复成功。"""
    states = _states()
    del states["recording_processing"]
    with pytest.raises(EventValidationError):
        output_guard(_registered_event(), _registered_context(states))


def test_registered_repaired_rejects_unfinished_repair(output_guard) -> None:
    """修复仍在进行或已失败取消：成品不具备登记依据。"""
    for repair_state in (3, 4, 6, 7):
        states = _states()
        states["recording_processing"][5]["repair_state"] = repair_state
        with pytest.raises(EventValidationError):
            output_guard(_registered_event(), _registered_context(states))


def test_registered_repaired_rejects_other_output_file(output_guard) -> None:
    """处理记录指向别的输出文件：该文件不是本次修复成品。"""
    states = _states()
    states["recording_processing"][5]["repair_output_file_id"] = 999
    with pytest.raises(EventValidationError):
        output_guard(_registered_event(), _registered_context(states))


def test_registered_repaired_rejects_other_action_processing(output_guard) -> None:
    """处理记录属于其他动作：不能为本次来源冒充修复成功。"""
    states = _states()
    states["recording_processing"][5]["action_id"] = 2
    with pytest.raises(EventValidationError):
        output_guard(_registered_event(), _registered_context(states))


@pytest.mark.parametrize("column,value", [
    ("purpose", 2), ("size_bytes", None), ("sha256", None),
])
def test_registered_repaired_requires_complete_repair_output(
    output_guard, column: str, value,
) -> None:
    """非修复用途或缺完整字节事实的文件不能登记为修复成品。"""
    states = _states()
    states["intermediate_files"][21][column] = value
    with pytest.raises(EventValidationError):
        output_guard(_registered_event(), _registered_context(states))


# ---- LIFECYCLE 提升配对 ----


def test_promotion_accepts_prior_registration(output_guard) -> None:
    """同事务先行的 REPAIRED 登记行授权保留推进。"""
    states = _states()
    states["outputs"][10] = {
        "id": 10, "kind": 2, "source_action_id": 1, "device_file_id": None,
        "intermediate_file_id": 21}
    output_guard(_promotion_event(), EventContext(TransactionRange(1, 1, 1), {}, states))


def test_promotion_rejects_missing_registration(output_guard) -> None:
    """没有先行登记的提升不成立：不能绕过正式产物登记。"""
    with pytest.raises(EventValidationError):
        output_guard(
            _promotion_event(),
            EventContext(TransactionRange(1, 1, 1), {}, _states()))


@pytest.mark.parametrize("kind", [1, 3])
def test_promotion_rejects_non_repaired_registration(output_guard, kind: int) -> None:
    """设备承载产物的登记不能授权中间文件提升。"""
    states = _states()
    states["outputs"][10] = {
        "id": 10, "kind": kind, "source_action_id": 1, "device_file_id": 9,
        "intermediate_file_id": None}
    with pytest.raises(EventValidationError):
        output_guard(_promotion_event(), EventContext(TransactionRange(1, 1, 1), {}, states))


def test_promotion_rejects_registration_of_other_file(output_guard) -> None:
    """登记行承载别的中间文件：不构成本文件的提升依据。"""
    states = _states()
    states["outputs"][10] = {
        "id": 10, "kind": 2, "source_action_id": 1, "device_file_id": None,
        "intermediate_file_id": 999}
    with pytest.raises(EventValidationError):
        output_guard(_promotion_event(), EventContext(TransactionRange(1, 1, 1), {}, states))


def test_promotion_rejects_registration_of_other_source(output_guard) -> None:
    """其他来源动作的登记不能授权本文件提升。"""
    states = _states()
    states["outputs"][10] = {
        "id": 10, "kind": 2, "source_action_id": 2, "device_file_id": None,
        "intermediate_file_id": 21}
    with pytest.raises(EventValidationError):
        output_guard(_promotion_event(), EventContext(TransactionRange(1, 1, 1), {}, states))


def test_release_retention_change_skips_pairing_check(output_guard) -> None:
    """推进到 RELEASABLE 属于释放流程，不要求产物登记先行。"""
    output_guard(
        _promotion_event(after_retention=2),
        EventContext(TransactionRange(1, 1, 1), {}, _states()))
