"""X2 来源固定与逐来源产物选择的单元测试。

六种来源引用形式解析为具体拍摄来源成员：动作与组引用要求真实
存在且可产生产物，计划范围允许合法空集合。默认选择以修复成品
替代原片且不含预览，修复不可用不回退原片；精确 ID 对不存在、
归属不匹配与已清理逐项失败；此前可靠确认存在的产物记录缺失是
状态库矛盾，不解释为普通不存在。
"""

from __future__ import annotations

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.catalog import OutputKind
from camctl.outputs.sources import (
    ActionFacts,
    CatalogEntry,
    ResolveFailure,
    ResolutionState,
    SelectionFacts,
    SelectionMode,
    SelectionSnapshot,
    SourceResolution,
    SourceSpec,
    select_outputs,
    resolve_source,
)

_CAPTURE = 2  # CAMERA_RECORD
_REPORT = 7  # REPORT_STATUS


class FakeLookup:
    """按内存事实回答来源解析查询的替身。"""

    def __init__(
        self,
        actions: tuple[ActionFacts, ...],
        *,
        owner_plan_id: int = 1,
        plans: set[int] | None = None,
    ) -> None:
        self._actions = {fact.action_id: fact for fact in actions}
        self._owner_plan_id = owner_plan_id
        self._plans = plans if plans is not None else {fact.plan_id for fact in actions}

    def owner_plan_id(self) -> int:
        return self._owner_plan_id

    def plan_exists(self, plan_id: int) -> bool:
        return plan_id in self._plans

    def action_by_id(self, action_id: int) -> ActionFacts | None:
        return self._actions.get(action_id)

    def plan_actions(self, plan_id: int) -> tuple[ActionFacts, ...]:
        return tuple(
            fact
            for fact in sorted(self._actions.values(), key=lambda item: item.action_id)
            if fact.plan_id == plan_id
        )


def _action(
    action_id: int,
    *,
    plan_id: int = 1,
    action_type: int = _CAPTURE,
    name: str | None = None,
    group_name: str | None = None,
) -> ActionFacts:
    return ActionFacts(
        action_id=action_id,
        plan_id=plan_id,
        action_type=action_type,
        name=name if name is not None else f"action-{action_id}",
        group_name=group_name,
    )


def _entry(
    output_id: int,
    kind: OutputKind,
    *,
    availability: int = 1,
    original_output_id: int | None = None,
    size_bytes: int | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        output_id=output_id,
        kind=kind,
        availability=availability,
        original_output_id=original_output_id,
        size_bytes=size_bytes,
    )


def _facts(
    entries: tuple[CatalogEntry, ...],
    *,
    source_action_id: int = 11,
    source_completed: bool = True,
    known: set[int] | None = None,
) -> SelectionFacts:
    return SelectionFacts(
        source_action_id=source_action_id,
        source_completed=source_completed,
        outputs=entries,
        known_output_ids=frozenset(known)
        if known is not None
        else frozenset(entry.output_id for entry in entries),
    )


# ---------------------------------------------------------------------------
# SourceSpec 六种形式
# ---------------------------------------------------------------------------


def test_spec_accepts_all_six_forms() -> None:
    """六种字段组合各自合法构造。"""
    SourceSpec(action_instance_id=11)
    SourceSpec(plan_instance_id=2, group="上午")
    SourceSpec(action_name="录像一")
    SourceSpec(group="上午")
    SourceSpec(current_plan=True)
    SourceSpec(plan_instance_id=2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"action_instance_id": 11, "group": "上午"},
        {"plan_instance_id": 2, "action_name": "录像一"},
        {"action_name": "录像一", "current_plan": True},
        {"group": "上午", "current_plan": True},
        {"action_instance_id": 0},
        {"plan_instance_id": 0, "group": "上午"},
        {"action_name": ""},
        {"group": ""},
    ],
)
def test_spec_rejects_invalid_combinations(kwargs) -> None:
    """非六种形式之一的组合或非法值拒绝构造。"""
    with pytest.raises(ValueError):
        SourceSpec(**kwargs)


# ---------------------------------------------------------------------------
# resolve_source
# ---------------------------------------------------------------------------


def test_resolve_action_reference_fixes_single_member() -> None:
    lookup = FakeLookup((_action(11, plan_id=3),))
    resolution = resolve_source(SourceSpec(action_instance_id=11), lookup)
    assert resolution.state is ResolutionState.FIXED
    assert resolution.member_action_ids == (11,)
    assert resolution.source_plan_id == 3
    assert resolution.failure is None


def test_resolve_action_reference_missing_fails() -> None:
    resolution = resolve_source(SourceSpec(action_instance_id=99), FakeLookup(()))
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.ACTION_NOT_FOUND


def test_resolve_action_reference_not_output_source_fails() -> None:
    lookup = FakeLookup((_action(11, action_type=_REPORT),))
    resolution = resolve_source(SourceSpec(action_instance_id=11), lookup)
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.NOT_OUTPUT_SOURCE


def test_resolve_plan_group_filters_capture_members() -> None:
    lookup = FakeLookup(
        (
            _action(11, plan_id=2, group_name="上午"),
            _action(12, plan_id=2, group_name="上午", action_type=_REPORT),
            _action(13, plan_id=2, group_name="下午"),
            _action(14, plan_id=2),
        )
    )
    resolution = resolve_source(
        SourceSpec(plan_instance_id=2, group="上午"), lookup
    )
    assert resolution.state is ResolutionState.FIXED
    assert resolution.member_action_ids == (11,)
    assert resolution.source_plan_id == 2


def test_resolve_group_without_members_fails() -> None:
    lookup = FakeLookup((_action(13, plan_id=2, group_name="下午"),))
    resolution = resolve_source(
        SourceSpec(plan_instance_id=2, group="上午"), lookup
    )
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.GROUP_NOT_FOUND


def test_resolve_group_with_only_non_capture_members_fails() -> None:
    lookup = FakeLookup((_action(12, plan_id=2, group_name="上午", action_type=_REPORT),))
    resolution = resolve_source(
        SourceSpec(plan_instance_id=2, group="上午"), lookup
    )
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.NOT_OUTPUT_SOURCE


def test_resolve_plan_scope_empty_members_is_fixed() -> None:
    lookup = FakeLookup((_action(12, plan_id=2, action_type=_REPORT),), plans={1, 2})
    by_plan = resolve_source(SourceSpec(plan_instance_id=2), lookup)
    assert by_plan.state is ResolutionState.FIXED
    assert by_plan.member_action_ids == ()
    assert by_plan.source_plan_id == 2

    whole_owner_plan = resolve_source(SourceSpec(current_plan=True), lookup)
    assert whole_owner_plan.state is ResolutionState.FIXED
    assert whole_owner_plan.member_action_ids == ()
    assert whole_owner_plan.source_plan_id == 1


def test_resolve_plan_not_found_fails() -> None:
    lookup = FakeLookup((), plans={1})
    resolution = resolve_source(SourceSpec(plan_instance_id=9), lookup)
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.PLAN_NOT_FOUND


def test_resolve_local_name_and_group_use_owner_plan() -> None:
    lookup = FakeLookup(
        (
            _action(11, plan_id=1, name="录像一", group_name="上午"),
            _action(12, plan_id=1, group_name="上午"),
            _action(13, plan_id=2, name="录像一"),
        )
    )
    by_name = resolve_source(SourceSpec(action_name="录像一"), lookup)
    assert by_name.state is ResolutionState.FIXED
    assert by_name.member_action_ids == (11,)
    assert by_name.source_plan_id == 1

    by_group = resolve_source(SourceSpec(group="上午"), lookup)
    assert by_group.state is ResolutionState.FIXED
    assert by_group.member_action_ids == (11, 12)

    whole_plan = resolve_source(SourceSpec(current_plan=True), lookup)
    assert whole_plan.state is ResolutionState.FIXED
    assert whole_plan.member_action_ids == (11, 12)


def test_resolve_local_name_missing_fails() -> None:
    lookup = FakeLookup((_action(11, plan_id=1, name="录像一"),))
    resolution = resolve_source(SourceSpec(action_name="另一个"), lookup)
    assert resolution.state is ResolutionState.FAILED
    assert resolution.failure is ResolveFailure.ACTION_NOT_FOUND


# ---------------------------------------------------------------------------
# select_outputs：默认方式
# ---------------------------------------------------------------------------


def test_empty_selection_is_fixed() -> None:
    """来源完成且无产物：合法空选择已固定，与尚未选定不同。"""
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(()),
        SelectionMode.DEFAULT,
    )
    assert isinstance(selection, SelectionSnapshot)
    assert selection.is_fixed is True
    assert selection.selected_output_ids == ()
    assert selection.source_error_code == 1  # no_outputs

    pending = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts((), source_completed=False),
        SelectionMode.DEFAULT,
    )
    assert pending.is_fixed is False
    assert pending.items == ()


def test_default_selection_prefers_repaired_and_excludes_previews() -> None:
    entries = (
        _entry(501, OutputKind.ORIGINAL),
        _entry(502, OutputKind.PREVIEW, original_output_id=501),
        _entry(503, OutputKind.REPAIRED, original_output_id=501),
        _entry(504, OutputKind.ORIGINAL),
    )
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.DEFAULT,
    )
    assert selection.is_fixed is True
    assert selection.selected_output_ids == (503, 504)
    preview_items = [
        item for item in selection.items if item.basis_name == "PREVIEW"
    ]
    assert preview_items == []
    repaired = selection.items[0]
    assert repaired.basis_name == "REPAIRED"
    assert repaired.output_id == 503
    assert repaired.original_output_id == 501


def test_default_repaired_unavailable_does_not_fall_back() -> None:
    entries = (
        _entry(501, OutputKind.ORIGINAL),
        _entry(503, OutputKind.REPAIRED, original_output_id=501, availability=3),
    )
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.DEFAULT,
    )
    assert selection.selected_output_ids == ()
    (item,) = selection.items
    assert item.status_name == "FAILED"
    assert item.error_code == 3  # output_unavailable
    assert item.error_details["output_id"] == 503
    assert item.error_details["availability"] == "cleaned"
    assert 501 not in selection.selected_output_ids


def test_default_restricted_output_fails_with_cleanup_started() -> None:
    entries = (_entry(501, OutputKind.ORIGINAL, availability=2),)
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.DEFAULT,
    )
    (item,) = selection.items
    assert item.status_name == "FAILED"
    assert item.error_code == 4  # output_cleanup_started


# ---------------------------------------------------------------------------
# select_outputs：精确 ID
# ---------------------------------------------------------------------------


def test_explicit_ids_fail_per_item_without_delivery() -> None:
    # 702 在目录中存在但不属于本来源；703 已清理；901 目录中不存在。
    entries = (
        _entry(703, OutputKind.ORIGINAL, availability=3),
        _entry(704, OutputKind.ORIGINAL),
    )
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries, known={702, 703, 704}),
        SelectionMode.EXPLICIT_IDS,
        requested_output_ids=(901, 702, 703, 704),
    )
    assert selection.is_fixed is True
    assert selection.source_error_code is None
    assert tuple(item.requested_output_id for item in selection.items) == (
        901,
        702,
        703,
        704,
    )
    not_found, mismatch, cleaned, selected = selection.items
    assert not_found.status_name == "FAILED"
    assert not_found.error_code == 1  # output_not_found
    assert not_found.output_id is None
    assert not_found.error_details == {"requested_output_id": 901}

    assert mismatch.status_name == "FAILED"
    assert mismatch.error_code == 2  # output_source_mismatch
    assert mismatch.output_id is None

    assert cleaned.status_name == "FAILED"
    assert cleaned.error_code == 3
    assert cleaned.error_details == {"output_id": 703, "availability": "cleaned"}

    assert selected.status_name == "SELECTED"
    assert selected.output_id == 704
    assert selected.basis_name == "EXPLICIT"
    assert all(item.delivery_created is False for item in selection.items)


def test_explicit_unknown_availability_stays_unresolved() -> None:
    entries = (_entry(704, OutputKind.ORIGINAL, availability=5),)
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.EXPLICIT_IDS,
        requested_output_ids=(704,),
    )
    (item,) = selection.items
    assert item.status_name == "UNRESOLVED"
    assert item.requested_output_id == 704
    assert item.output_id is None


def test_explicit_duplicate_request_rejected() -> None:
    entries = (_entry(704, OutputKind.ORIGINAL),)
    with pytest.raises(ValueError):
        select_outputs(
            SourceResolution(
                state=ResolutionState.FIXED,
                member_action_ids=(11,),
                source_plan_id=1,
            ),
            _facts(entries),
            SelectionMode.EXPLICIT_IDS,
            requested_output_ids=(704, 704),
        )


# ---------------------------------------------------------------------------
# 事实矛盾
# ---------------------------------------------------------------------------


def test_previously_confirmed_missing_record_is_consistency_error() -> None:
    with pytest.raises(ConsistencyError):
        select_outputs(
            SourceResolution(
                state=ResolutionState.FIXED,
                member_action_ids=(11,),
                source_plan_id=1,
            ),
            SelectionFacts(
                source_action_id=11,
                source_completed=True,
                outputs=(_entry(704, OutputKind.ORIGINAL),),
                known_output_ids=frozenset({704}),
                previously_confirmed_ids=frozenset({701}),
            ),
            SelectionMode.DEFAULT,
        )


def test_duplicate_derived_output_for_one_original_is_consistency_error() -> None:
    entries = (
        _entry(501, OutputKind.ORIGINAL),
        _entry(503, OutputKind.REPAIRED, original_output_id=501),
        _entry(505, OutputKind.REPAIRED, original_output_id=501),
    )
    with pytest.raises(ConsistencyError):
        select_outputs(
            SourceResolution(
                state=ResolutionState.FIXED,
                member_action_ids=(11,),
                source_plan_id=1,
            ),
            _facts(entries),
            SelectionMode.DEFAULT,
        )


# ---------------------------------------------------------------------------
# select_outputs：预览方式
# ---------------------------------------------------------------------------


def test_preview_mode_selects_preview_or_not_larger_repaired() -> None:
    entries = (
        _entry(601, OutputKind.ORIGINAL),
        _entry(602, OutputKind.PREVIEW, original_output_id=601, size_bytes=100),
        _entry(603, OutputKind.REPAIRED, original_output_id=601, size_bytes=100),
        _entry(611, OutputKind.ORIGINAL),
        _entry(612, OutputKind.PREVIEW, original_output_id=611, size_bytes=100),
        _entry(613, OutputKind.REPAIRED, original_output_id=611, size_bytes=150),
        _entry(621, OutputKind.ORIGINAL),
        _entry(622, OutputKind.PREVIEW, original_output_id=621, size_bytes=80),
        _entry(631, OutputKind.ORIGINAL),
    )
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.PREVIEW,
    )
    assert selection.is_fixed is True
    equal_repaired, larger_repaired, plain_preview, missing = selection.items
    assert equal_repaired.basis_name == "REPAIRED_NOT_LARGER"
    assert equal_repaired.output_id == 603
    assert equal_repaired.preview_output_id == 602
    assert equal_repaired.preview_size == 100
    assert equal_repaired.repaired_size == 100

    assert larger_repaired.basis_name == "PREVIEW"
    assert larger_repaired.output_id == 612
    assert larger_repaired.preview_size == 100
    assert larger_repaired.repaired_size == 150

    assert plain_preview.basis_name == "PREVIEW"
    assert plain_preview.output_id == 622
    assert plain_preview.preview_size is None

    assert missing.status_name == "FAILED"
    assert missing.error_code == 8  # preview_missing
    assert missing.original_output_id == 631
    assert missing.output_id is None


def test_preview_mode_without_any_preview_records_source_error() -> None:
    entries = (_entry(631, OutputKind.ORIGINAL),)
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.PREVIEW,
    )
    assert selection.source_error_code == 2  # preview_missing
    (item,) = selection.items
    assert item.error_code == 8


def test_preview_comparison_size_unknown_fails_without_guessing() -> None:
    entries = (
        _entry(601, OutputKind.ORIGINAL),
        _entry(602, OutputKind.PREVIEW, original_output_id=601, size_bytes=100),
        _entry(603, OutputKind.REPAIRED, original_output_id=601, size_bytes=None),
    )
    selection = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(11,),
            source_plan_id=1,
        ),
        _facts(entries),
        SelectionMode.PREVIEW,
    )
    (item,) = selection.items
    assert item.status_name == "FAILED"
    assert item.error_code == 6  # source_file_unconfirmed
