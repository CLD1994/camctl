"""取消目标完整寻址、自身包含检查与联动去重的单元测试。

覆盖《目标集合不得包含取消动作自身》四种入口：包含自身时整个取消
失败且不产生任何目标效果；可靠不存在使用登记的 cancel_target_not_
found；查询错误不归不存在；自动候选去重但拒绝取消的直接目标保留；
自身检查使用完整集合，不受目标当前可取消性提前缩小。
"""

from __future__ import annotations

import pytest

from camctl.cancellation.models import (
    CancelTarget,
    CancelTargetError,
    ResolvedTargets,
    TargetFacts,
)
from camctl.cancellation.targets import (
    CancelLookupError,
    Lookup,
    missing_target_error,
    prepare_cancel_set,
    resolve_cancel_target,
)

# 取消动作自身。
_ORIGIN = 50


def _facts(action_id, *, terminal=False, may_cancel=True):
    return TargetFacts(
        action_id=action_id, terminal=terminal, may_cancel=may_cancel)


class TestResolveCancelTarget:
    def test_action_entry_returns_single_action(self):
        resolution = resolve_cancel_target(
            CancelTarget(action_instance_id=11), Lookup(actions={11: True}))
        assert resolution.action_ids == (11,)
        assert not resolution.missing and resolution.lookup_failed is None

    def test_plan_entry_returns_plan_actions(self):
        resolution = resolve_cancel_target(
            CancelTarget(plan_instance_id=2),
            Lookup(plans={2: (11, 12)}))
        assert resolution.action_ids == (11, 12)

    def test_group_entry_returns_group_members_only(self):
        resolution = resolve_cancel_target(
            CancelTarget(plan_instance_id=2, group="g"),
            Lookup(groups={(2, "g"): (12,)}))
        assert resolution.action_ids == (12,)

    def test_request_entry_returns_first_accepted_plan(self):
        resolution = resolve_cancel_target(
            CancelTarget(request_id="1001"),
            Lookup(requests={"1001": (11, 12, 13)}))
        assert resolution.action_ids == (11, 12, 13)

    @pytest.mark.parametrize("target", [
        CancelTarget(action_instance_id=11),
        CancelTarget(plan_instance_id=2),
        CancelTarget(plan_instance_id=2, group="g"),
        CancelTarget(request_id="1001"),
    ])
    def test_reliable_missing_is_reported_per_entry(self, target):
        resolution = resolve_cancel_target(target, Lookup())
        assert resolution.missing
        assert resolution.action_ids == ()
        error = missing_target_error(target)
        assert error.code == "cancel_target_not_found"
        assert error.details["target"] == target.as_fields()

    def test_lookup_error_is_not_missing(self):
        def raising(*_args, **_kwargs):
            raise CancelLookupError("数据库不可读")

        resolution = resolve_cancel_target(
            CancelTarget(action_instance_id=11),
            Lookup(failure=CancelLookupError("数据库不可读")))
        assert resolution.lookup_failed is not None
        assert not resolution.missing
        assert resolution.action_ids == ()


class TestPrepareCancelSet:
    def test_self_target_has_no_partial_effect(self):
        """直接自身、所属计划、包含自身的组及 request_id 各入口。

        自身检查用完整集合：任何入口的完整范围包含取消动作自身时，
        不固定任何目标效果，按登记错误 cancel_self_target 失败。
        """
        direct_self = ResolvedTargets(
            direct=(_facts(_ORIGIN), _facts(12)), auto_candidates=())
        plan_self = ResolvedTargets(
            direct=(_facts(11), _facts(_ORIGIN), _facts(12)), auto_candidates=())
        group_self = ResolvedTargets(
            direct=(_facts(12), _facts(_ORIGIN)), auto_candidates=())
        request_self = ResolvedTargets(
            direct=(_facts(11), _facts(_ORIGIN)), auto_candidates=())
        for resolved in (direct_self, plan_self, group_self, request_self):
            outcome = prepare_cancel_set(_ORIGIN, resolved)
            assert isinstance(outcome, CancelTargetError), resolved
            assert outcome.code == "cancel_self_target"
            assert outcome.details == {"action_instance_id": str(_ORIGIN)}

    def test_self_in_full_scope_even_if_linkage_would_not_apply(self):
        """完整寻址范围不受可取消性提前缩小。

        联动候选包含自身时，即使其来源拍摄拒绝取消、联动本不会生
        效，自身检查仍按完整集合失败。
        """
        resolved = ResolvedTargets(
            direct=(_facts(11, may_cancel=False),),
            auto_candidates=((11, _ORIGIN),))
        outcome = prepare_cancel_set(_ORIGIN, resolved)
        assert isinstance(outcome, CancelTargetError)
        assert outcome.code == "cancel_self_target"

    def test_direct_targets_are_all_kept_with_effect_by_terminal(self):
        resolved = ResolvedTargets(
            direct=(_facts(11), _facts(12, terminal=True)), auto_candidates=())
        fixed = prepare_cancel_set(_ORIGIN, resolved)
        assert [(target.action_id, target.basis.value,
                 target.cancellation_effect.value)
                for target in fixed.targets] == [
            (11, 1, 1), (12, 1, 3)]

    def test_auto_preview_joins_only_linkable_sources(self):
        """拍摄允许取消或已终态时联动其自动预览取回。"""
        resolved = ResolvedTargets(
            direct=(_facts(11), _facts(12, terminal=True),
                    _facts(13, may_cancel=False)),
            auto_candidates=((11, 21), (12, 22), (13, 23)))
        fixed = prepare_cancel_set(_ORIGIN, resolved)
        assert [(target.action_id, target.basis.value)
                for target in fixed.targets] == [
            (11, 1), (12, 1), (13, 1), (21, 2), (22, 2)]

    def test_direct_and_auto_overlap_marks_both_and_dedupes(self):
        resolved = ResolvedTargets(
            direct=(_facts(11), _facts(21)),
            auto_candidates=((11, 21),))
        fixed = prepare_cancel_set(_ORIGIN, resolved)
        assert [(target.action_id, target.basis.value)
                for target in fixed.targets] == [
            (11, 1), (21, 3)]

    def test_auto_source_outside_direct_scope_is_not_linked(self):
        resolved = ResolvedTargets(
            direct=(_facts(11),),
            auto_candidates=((99, 21),))
        fixed = prepare_cancel_set(_ORIGIN, resolved)
        assert [target.action_id for target in fixed.targets] == [11]
