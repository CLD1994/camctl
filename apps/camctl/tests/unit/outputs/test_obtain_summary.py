"""X10 取回统一发布汇总的单元测试。

任一来源未判定、显式条目未判定或准备未定都继续等待，不发布已
有成功文件；全部来源判定与准备结果确定后发布成功项，整次有失
败仍保留成功交付。条目阶段由已保存的选择与交付状态映射，待判
定不计入准备汇总。
"""

from __future__ import annotations

import pytest

from camctl.outputs.obtain_summary import (
    ObtainFacts,
    ObtainItemStage,
    ObtainPhase,
    ObtainSourceFacts,
    decide_obtain_finish,
    obtain_item_stage,
)

_ITEM_UNRESOLVED = 1
_ITEM_SELECTED = 2
_ITEM_DELIVERY_CREATED = 3
_ITEM_FAILED = 4
_ITEM_CANCELED = 5

_DELIVERY_PENDING = 1
_DELIVERY_PREPARING = 2
_DELIVERY_PREPARED = 3
_DELIVERY_PUBLISHING = 4
_DELIVERY_PUBLISHED = 5
_DELIVERY_FAILED = 6
_DELIVERY_CANCELED = 7
_DELIVERY_WITHDRAWN = 8


def _sources(*fixed: bool) -> tuple[ObtainSourceFacts, ...]:
    return tuple(ObtainSourceFacts(selection_fixed=value) for value in fixed)


# ---- 决策表 ----


def test_publish_waits_for_complete_selection() -> None:
    """一来源已准备、另一仍未选：不发布已有成功文件。"""
    decision = decide_obtain_finish(ObtainFacts(
        sources=_sources(True, False),
        items=(ObtainItemStage.PREPARED,),
    ))
    assert decision.phase is ObtainPhase.WAIT_SOURCES
    assert decision.may_publish is False


def test_unresolved_explicit_item_waits() -> None:
    decision = decide_obtain_finish(ObtainFacts(
        sources=(ObtainSourceFacts(selection_fixed=True, unresolved_items=1),),
        items=(),
    ))
    assert decision.phase is ObtainPhase.WAIT_ITEMS
    assert decision.may_publish is False


@pytest.mark.parametrize("stage", [
    ObtainItemStage.PENDING, ObtainItemStage.PROCESSING,
])
def test_undetermined_preparation_waits(stage) -> None:
    """产物等待准备、正在处理或可继续重试：继续处理不发布。"""
    decision = decide_obtain_finish(ObtainFacts(
        sources=_sources(True),
        items=(ObtainItemStage.PREPARED, stage),
    ))
    assert decision.phase is ObtainPhase.WAIT_PREPARATION
    assert decision.may_publish is False


def test_determined_selection_publishes_successes_with_failures() -> None:
    """全部判定与准备结果确定：发布成功项，失败结果保留。"""
    decision = decide_obtain_finish(ObtainFacts(
        sources=_sources(True, True),
        items=(ObtainItemStage.PREPARED, ObtainItemStage.FAILED,
               ObtainItemStage.PREPARED),
    ))
    assert decision.phase is ObtainPhase.READY_TO_PUBLISH
    assert decision.may_publish is True
    assert (decision.prepared, decision.failed) == (2, 1)


def test_all_failed_is_determined_without_success_files() -> None:
    """没有成功文件：发布阶段确定，不创建交付文件由调用方执行。"""
    decision = decide_obtain_finish(ObtainFacts(
        sources=_sources(True),
        items=(ObtainItemStage.FAILED,),
    ))
    assert decision.phase is ObtainPhase.READY_TO_PUBLISH
    assert (decision.prepared, decision.failed) == (0, 1)


def test_empty_sources_are_determined() -> None:
    """无固定来源：判定完成且没有条目（无来源失败由选择层表达）。"""
    decision = decide_obtain_finish(ObtainFacts(sources=(), items=()))
    assert decision.phase is ObtainPhase.READY_TO_PUBLISH
    assert (decision.prepared, decision.failed) == (0, 0)


def test_source_wait_precedes_item_wait() -> None:
    """来源未判定优先：此时条目汇总不完整也不进入后续分支。"""
    decision = decide_obtain_finish(ObtainFacts(
        sources=(ObtainSourceFacts(selection_fixed=False, unresolved_items=2),),
        items=(ObtainItemStage.PENDING,),
    ))
    assert decision.phase is ObtainPhase.WAIT_SOURCES


# ---- 条目阶段映射 ----


def test_selected_item_without_delivery_waits_for_qualification() -> None:
    assert obtain_item_stage(_ITEM_SELECTED, None) is ObtainItemStage.PENDING


@pytest.mark.parametrize("delivery, expected", [
    (_DELIVERY_PENDING, ObtainItemStage.PENDING),
    (_DELIVERY_PREPARING, ObtainItemStage.PROCESSING),
    (_DELIVERY_PREPARED, ObtainItemStage.PREPARED),
    (_DELIVERY_PUBLISHING, ObtainItemStage.PREPARED),
    (_DELIVERY_PUBLISHED, ObtainItemStage.PREPARED),
    (_DELIVERY_FAILED, ObtainItemStage.FAILED),
    (_DELIVERY_CANCELED, ObtainItemStage.FAILED),
    (_DELIVERY_WITHDRAWN, ObtainItemStage.FAILED),
])
def test_delivery_status_maps_to_stage(delivery, expected) -> None:
    assert obtain_item_stage(_ITEM_DELIVERY_CREATED, delivery) is expected


@pytest.mark.parametrize("item", [_ITEM_FAILED, _ITEM_CANCELED])
def test_final_item_failure_ignores_delivery(item) -> None:
    assert obtain_item_stage(item, _DELIVERY_PREPARED) is ObtainItemStage.FAILED


def test_unresolved_item_is_not_a_preparation_stage() -> None:
    """待判定条目计入来源事实，不进入准备汇总。"""
    with pytest.raises(ValueError):
        obtain_item_stage(_ITEM_UNRESOLVED, None)


@pytest.mark.parametrize("item, delivery", [
    (0, None), (9, None), (_ITEM_SELECTED, 0), (_ITEM_SELECTED, 9),
])
def test_unknown_states_are_rejected(item, delivery) -> None:
    with pytest.raises(ValueError):
        obtain_item_stage(item, delivery)


# ---- 事实校验 ----


def test_facts_reject_non_boolean_fixation() -> None:
    with pytest.raises(TypeError):
        ObtainSourceFacts(selection_fixed=1)  # type: ignore[arg-type]


def test_facts_reject_negative_unresolved() -> None:
    with pytest.raises(ValueError):
        ObtainSourceFacts(selection_fixed=True, unresolved_items=-1)


def test_facts_reject_invalid_stage() -> None:
    with pytest.raises(TypeError):
        ObtainFacts(sources=_sources(True), items=("prepared",))  # type: ignore[arg-type]
