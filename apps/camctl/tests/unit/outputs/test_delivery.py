"""普通交付交接判定的决策表单元测试。

按[普通交付的保存顺序与中断恢复](../../../../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)
的决策表验证 decide_handoff 的每个证据分区：已有完成事实保留成功、
ready/processing 观察补存本地交付、staging 完整副本继续原身份、
三处均无保存终局未知失败且不自动重投；观察失败或证据矛盾不套用
缺失分支，取消、撤回与终态不被普通恢复覆盖。
"""

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.handoff import (
    DeliveryFacts,
    DeliveryHandoffOutcome,
    DeliveryLocations,
    DeliveryFailure,
    LocationObservation,
    decide_handoff,
)

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


def _locations(*, staging=None, ready=None, processing=None) -> DeliveryLocations:
    return DeliveryLocations(
        staging=staging or LocationObservation(observed=True, present=False),
        ready=ready or LocationObservation(observed=True, present=False),
        processing=processing or LocationObservation(observed=True, present=False),
    )


def _absent() -> LocationObservation:
    return LocationObservation(observed=True, present=False)


def _present(length: int = 10, sha256: str | None = _DIGEST) -> LocationObservation:
    return LocationObservation(
        observed=True, present=True, length=length, sha256=sha256)


def _facts(**overrides) -> DeliveryFacts:
    values = dict(
        delivery_id=7,
        status=4,
        publication_intent_event_id=900,
        published_event_id=None,
        prepared_size=10,
        prepared_sha256=_DIGEST,
        withdrawal_requested=False,
        publication_conditions_met=True,
    )
    values.update(overrides)
    return DeliveryFacts(**values)


# ---- 三处均无副本的终局未知失败 ----


@pytest.mark.parametrize("status", [3, 4])
def test_missing_all_copies_is_final_unknown_failure(status):
    """无完成事实且可靠三处缺失：终局失败，不自动重投。"""
    decision = decide_handoff(
        _facts(status=status),
        _locations(),
    )
    assert decision.outcome is DeliveryHandoffOutcome.UNCONFIRMED_FINAL
    assert decision.failure is not None
    assert decision.failure.code == "delivery_handoff_unconfirmed"
    assert decision.failure.stage == "publication"
    assert decision.failure.details == {"delivery_id": "7"}
    assert decision.republishes == 0


@pytest.mark.parametrize("place", ["staging", "ready", "processing"])
def test_observation_failure_is_not_missing_failure(place):
    """任一位置检查失败不能归入三处均无的终局分支。"""
    broken = LocationObservation(observed=False, error=OSError("busy"))
    decision = decide_handoff(
        _facts(),
        _locations(**{place: broken}),
    )
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE
    assert decision.failure is None
    assert decision.republishes == 0


# ---- staging 完整副本继续原身份 ----


def test_staging_copy_continues_same_identity():
    decision = decide_handoff(_facts(), _locations(staging=_present()))
    assert decision.outcome is DeliveryHandoffOutcome.PUBLISH
    assert decision.republishes == 0


def test_staging_copy_holds_without_publication_conditions():
    decision = decide_handoff(
        _facts(publication_conditions_met=False), _locations(staging=_present()))
    assert decision.outcome is DeliveryHandoffOutcome.HOLD


def test_staging_length_mismatch_is_undecidable():
    decision = decide_handoff(
        _facts(), _locations(staging=_present(length=9)))
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE


def test_staging_digest_mismatch_is_undecidable():
    decision = decide_handoff(
        _facts(), _locations(staging=_present(sha256=_OTHER_DIGEST)))
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE


# ---- ready/processing 观察补存本地交付事实 ----


@pytest.mark.parametrize("place", ["ready", "processing"])
def test_observed_copy_confirms_local_delivery(place):
    decision = decide_handoff(_facts(), _locations(**{place: _present()}))
    assert decision.outcome is DeliveryHandoffOutcome.DELIVERED_LOCALLY
    assert decision.observed_location is not None
    assert decision.observed_location.value == place
    assert decision.republishes == 0


@pytest.mark.parametrize("place", ["ready", "processing"])
def test_observed_copy_without_digest_is_undecidable(place):
    """ready/processing 的副本必须按摘要确认是同一完整副本。"""
    decision = decide_handoff(
        _facts(), _locations(**{place: _present(sha256=None)}))
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE


@pytest.mark.parametrize("place", ["ready", "processing"])
def test_observed_copy_digest_mismatch_is_undecidable(place):
    decision = decide_handoff(
        _facts(), _locations(**{place: _present(sha256=_OTHER_DIGEST)}))
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE


def test_copies_in_both_receiving_places_are_undecidable():
    decision = decide_handoff(
        _facts(),
        _locations(ready=_present(), processing=_present()),
    )
    assert decision.outcome is DeliveryHandoffOutcome.UNDECIDABLE


# ---- 已有完成事实与已收场交付 ----


def test_saved_publication_survives_missing_files():
    """已保存的完成事实不因文件后来消失而撤销。"""
    decision = decide_handoff(
        _facts(status=5, published_event_id=901), _locations())
    assert decision.outcome is DeliveryHandoffOutcome.COMPLETED


@pytest.mark.parametrize("status", [6, 7, 8])
def test_settled_failure_states_are_not_reactivated(status):
    decision = decide_handoff(_facts(status=status), _locations(staging=_present()))
    assert decision.outcome is DeliveryHandoffOutcome.NOT_ACTIVE


def test_withdrawal_request_is_not_reactivated():
    decision = decide_handoff(
        _facts(withdrawal_requested=True), _locations(staging=_present()))
    assert decision.outcome is DeliveryHandoffOutcome.NOT_ACTIVE


@pytest.mark.parametrize("status", [1, 2])
def test_unprepared_delivery_is_rejected(status):
    with pytest.raises(ConsistencyError):
        decide_handoff(_facts(status=status), _locations())


def test_completed_with_published_flag_but_other_status_is_rejected():
    with pytest.raises(ConsistencyError):
        decide_handoff(_facts(status=4, published_event_id=901), _locations())


def test_missing_prepared_record_is_rejected():
    with pytest.raises(ConsistencyError):
        decide_handoff(
            _facts(prepared_size=None, prepared_sha256=None),
            _locations(staging=_present()),
        )


# ---- 输入类型校验 ----


def test_location_observation_unreliable_carries_no_facts():
    with pytest.raises(ValueError):
        LocationObservation(observed=False, present=True)
    with pytest.raises(ValueError):
        LocationObservation(observed=False, error=None)


def test_location_observation_present_needs_length():
    with pytest.raises(ValueError):
        LocationObservation(observed=True, present=True)
    with pytest.raises(ValueError):
        LocationObservation(observed=True, present=True, length=-1)
    with pytest.raises(ValueError):
        LocationObservation(observed=True, present=True, length=True)


def test_location_observation_absent_carries_no_file_facts():
    with pytest.raises(ValueError):
        LocationObservation(observed=True, present=False, length=10)
    with pytest.raises(ValueError):
        LocationObservation(observed=True, present=False, sha256=_DIGEST)


def test_location_observation_digest_must_be_hex():
    with pytest.raises(ValueError):
        LocationObservation(
            observed=True, present=True, length=10, sha256="zz")


def test_delivery_facts_status_validation():
    with pytest.raises(ValueError):
        _facts(status=True)
    with pytest.raises(ValueError):
        _facts(status=0)
    with pytest.raises(ValueError):
        _facts(status=9)
    with pytest.raises(ValueError):
        _facts(prepared_size=-1)
    with pytest.raises(ValueError):
        _facts(prepared_sha256="short")


def test_delivery_locations_must_use_observations():
    with pytest.raises(TypeError):
        DeliveryLocations(
            staging=_absent(), ready=_absent(), processing="processing")


def test_decide_handoff_requires_facts_type():
    with pytest.raises(TypeError):
        decide_handoff({"status": 4}, _locations())


# ---- 公共错误登记约束 ----


def test_delivery_failure_validates_against_registry():
    assert DeliveryFailure(
        code="delivery_handoff_unconfirmed", stage="publication",
        details={"delivery_id": "7"},
    ).code == "delivery_handoff_unconfirmed"
    with pytest.raises(ValueError):
        DeliveryFailure(
            code="delivery_handoff_unconfirmed", stage="wrong-stage",
            details={"delivery_id": "7"})
    with pytest.raises(ValueError):
        DeliveryFailure(
            code="unknown_code", stage="publication",
            details={"delivery_id": "7"})
    with pytest.raises(ValueError):
        DeliveryFailure(
            code="delivery_handoff_unconfirmed", stage="publication",
            details={"delivery_id": 7})
