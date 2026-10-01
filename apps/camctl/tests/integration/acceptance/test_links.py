"""A3 公共 Schema、完整本计划关联与执行定义的组件集成测试。

期望独立来自受理与自动预览契约：先建全计划名称索引再解析引用；
自动预览冲突使全部冲突取回失败；跨计划来源不在受理时查询。
"""

from __future__ import annotations

import pytest

from camctl.acceptance.links import PlanIdentities, prepare_plan
from camctl.acceptance.rules import validate_new_body

from unit.acceptance.helpers import StubCatalog, auto_preview, camera_action, manual_obtain


def _prepare(actions: list[dict]):
    body = {
        "request_id": "42",
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": actions,
    }
    decision = validate_new_body(body, StubCatalog())
    assert not decision.is_whole_rejection, decision.rejection_reasons
    names = [action["name"] for action in actions]
    identities = PlanIdentities(
        plan_id=7,
        action_ids={name: 100 + index for index, name in enumerate(names)},
    )
    return prepare_plan(decision, identities)


class TestAutoPreviewConflicts:
    def test_conflict_keeps_other_known_action_errors(self):
        broken = auto_preview("preview-a", "shoot")
        broken["scheduled_at"] = "2026-02-30 09:00:00"
        prepared = _prepare([camera_action("shoot"), broken, auto_preview("preview-b","shoot")])
        first, second = prepared.actions[1:]
        assert first.failure_code == second.failure_code == "duplicate_auto_preview"
        assert first.failure_details["obtain_action_names"] == second.failure_details["obtain_action_names"] == ["preview-a","preview-b"]
        assert any(issue["field"] == "actions[1].scheduled_at" and issue.get("value") == "2026-02-30 09:00:00" for issue in first.failure_details["issues"])
        assert "issues" not in second.failure_details

    def test_all_duplicate_previews_fail(self) -> None:
        prepared = _prepare(
            [
                camera_action("shoot-a"),
                camera_action("shoot-b"),
                auto_preview("p-a", "shoot-a"),
                auto_preview("p-a2", "shoot-a"),
                auto_preview("p-b", "shoot-b"),
                manual_obtain("m-1", "shoot-a"),
            ]
        )
        failed = {a.name: a for a in prepared.actions if not a.ok}
        assert sorted(failed) == ["p-a", "p-a2"]
        assert failed["p-a"].failure_code == "duplicate_auto_preview"
        assert "shoot-a" in failed["p-a"].failure_reason
        # 拍摄、另一来源的唯一自动取回与手动取回不受冲突影响。
        assert [a.name for a in prepared.actions if a.ok] == [
            "shoot-a",
            "shoot-b",
            "p-b",
            "m-1",
        ]

    def test_manual_obtain_and_camera_unaffected_by_conflict(self) -> None:
        prepared = _prepare(
            [
                camera_action("shoot"),
                auto_preview("dup-1", "shoot"),
                auto_preview("dup-2", "shoot"),
                manual_obtain("manual", "shoot"),
            ]
        )
        assert [a.name for a in prepared.actions if a.ok] == ["shoot", "manual"]


class TestInPlanReferences:
    def test_forward_reference_resolves(self) -> None:
        # 取回出现在被引用拍摄之前：全计划索引先建立，引用合法。
        prepared = _prepare(
            [
                manual_obtain("early", "late-shot"),
                camera_action("late-shot"),
            ]
        )
        by_name = {a.name: a for a in prepared.actions}
        assert by_name["early"].ok
        assert by_name["early"].in_plan_dependencies == ("late-shot",)

    def test_missing_source_name_fails_the_obtain(self) -> None:
        prepared = _prepare([manual_obtain("orphan", "no-such-shot")])
        action = prepared.actions[0]
        assert not action.ok
        assert "no-such-shot" in action.failure_reason

    def test_cross_plan_sources_not_queried_at_acceptance(self) -> None:
        # 按实例 ID 与按组引用属跨计划来源：不在受理时查询或失败。
        by_id = manual_obtain("by-id", None)
        by_id["params"] = {
            "source": {"action_instance_id": "500"},
            "output_ids": ["900"],
            "purpose": "manual",
        }
        by_group = manual_obtain("by-group", None)
        by_group["params"] = {"source": {"plan_instance_id":"9", "group": "tripod"}, "purpose": "manual"}
        prepared = _prepare([by_id, by_group])
        assert all(action.ok for action in prepared.actions)
        assert all(not action.in_plan_dependencies for action in prepared.actions)


class TestGroups:
    def test_failed_camera_in_group_still_source(self) -> None:
        # 组内拍摄自身失败仍按类型作为组来源成员。
        broken = camera_action("broken-shot", group="tripod")
        broken["params"] = {"type": "single_shot", "shots": 99}
        by_group = manual_obtain("group-obtain", None)
        by_group["params"] = {"source": {"group": "tripod"}, "purpose": "manual"}
        prepared = _prepare(
            [
                camera_action("good-shot", group="tripod"),
                broken,
                by_group,
            ]
        )
        by_name = {a.name: a for a in prepared.actions}
        assert by_name["group-obtain"].ok
        assert by_name["broken-shot"].ok is False
        assert by_name["broken-shot"].group_name == "tripod"

    def test_obtain_with_group_field_is_action_failure(self) -> None:
        # 取回自身的 group 不适用；计划和其他动作仍受理。
        from camctl.acceptance.rules import validate_new_body
        from unit.acceptance.helpers import StubCatalog

        grouped_obtain = manual_obtain("grouped-obtain", "shoot")
        grouped_obtain["group"] = "tripod"
        decision = validate_new_body(
            {
                "request_id": "42",
                "created_at": "2026-01-15 08:00:00",
                "name": "plan",
                "actions": [camera_action("shoot", group="tripod"), grouped_obtain],
            },
            StubCatalog(),
        )
        assert not decision.is_whole_rejection
        assert not decision.actions[1].ok
        assert decision.actions[1].group_name is None


class TestExecutionDefinitions:
    def test_effective_params_and_identity_preserved(self) -> None:
        prepared = _prepare([camera_action("shoot")])
        action = prepared.actions[0]
        assert action.ok
        assert action.action_id == 100
        assert action.effective_params["shots"] == 1
        assert action.raw_fields["params"] == {"type": "single_shot"}
        assert action.scheduled_at_micros is not None

    def test_failed_action_keeps_original_fields(self) -> None:
        broken = camera_action("broken")
        broken["params"] = {"type": "single_shot", "shots": 99}
        prepared = _prepare([broken])
        action = prepared.actions[0]
        assert not action.ok
        assert action.raw_fields["params"] == {"type": "single_shot", "shots": 99}
        assert action.effective_params is None


@pytest.mark.parametrize("source,kind,expected_members", [
    ({"action_name":"shoot"},"obtain_action_outputs",["shoot"]),
    ({"group":"tripod"},"obtain_action_outputs",["shoot"]),
    ({"current_plan":True},"obtain_action_outputs",["shoot"]),
    ({"current_plan":True},"delete_action_outputs",["shoot"]),
    ({"action_name":"shoot"},"delete_action_outputs",["shoot"]),
])
@pytest.mark.asyncio
async def test_local_source_forms_fixed_at_acceptance(environment, tmp_path, source, kind, expected_members):
    from .test_acceptance import _accept, _plan_body
    connection, _ = environment
    shoot = camera_action("shoot", group="tripod")
    fetch = manual_obtain("fetch", None)
    fetch["type"] = kind
    fetch["params"] = {"source":source}
    result = await _accept(environment, tmp_path, _plan_body(actions=[fetch, shoot]))
    assert connection.execute("SELECT status,source_resolution_state,resolved_source_plan_id FROM actions WHERE name='fetch'").fetchone() == (1,2,result.plan_id)
    assert connection.execute("SELECT s.name FROM action_dependencies d JOIN actions s ON s.id=d.depends_on_action_id").fetchall() == [(name,) for name in expected_members]


@pytest.mark.asyncio
async def test_empty_current_plan_is_fixed(environment, tmp_path):
    from .test_acceptance import _accept, _plan_body
    connection, _ = environment
    fetch = manual_obtain("fetch", None)
    fetch["params"] = {"source":{"current_plan":True}}
    result = await _accept(environment, tmp_path, _plan_body(actions=[fetch]))
    assert connection.execute("SELECT source_resolution_state,resolved_source_plan_id FROM actions").fetchone() == (2,result.plan_id)
    assert connection.execute("SELECT COUNT(*) FROM action_dependencies").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["obtain_action_outputs", "delete_action_outputs"])
@pytest.mark.asyncio
async def test_missing_local_action_keeps_failed_action(environment, tmp_path, kind):
    from .test_acceptance import _accept, _plan_body
    from camctl.contracts.json_values import parse_exact_json
    connection, _ = environment
    fetch = manual_obtain("fetch", "missing")
    fetch["type"] = kind
    fetch["params"] = {"source":{"action_name":"missing"}}
    await _accept(environment, tmp_path, _plan_body(actions=[fetch]))
    row = connection.execute("SELECT status,error_code,error_details_json,source_resolution_state FROM actions").fetchone()
    assert row[:2] == (4,2)
    assert parse_exact_json(row[2]) == {"field":"actions[0].params.source.action_name","value":"missing"}
    assert row[3] is None


from .test_acceptance import environment


@pytest.mark.parametrize("supported,same_time,expected_status", [(True,True,1),(True,False,4),(False,True,4),(False,False,4)])
@pytest.mark.asyncio
async def test_preview_support_and_time_matrix(environment, tmp_path, supported, same_time, expected_status):
    from dataclasses import replace
    from .test_acceptance import _accept, _plan_body
    connection, context = environment
    original = context.catalog.parameter_definition
    context.catalog.parameter_definition = lambda *args: replace(original(*args), preview_supported=supported)
    shoot = camera_action("shoot")
    preview = auto_preview("preview", "shoot")
    preview["scheduled_at"] = shoot["scheduled_at"] if same_time else "2026-01-15 10:00:00"
    await _accept(environment, tmp_path, _plan_body(actions=[shoot,preview]))
    assert connection.execute("SELECT status FROM actions WHERE name='preview'").fetchone()[0] == expected_status
    assert connection.execute("SELECT source_action_id,preview_support,parameter_type,is_valid FROM auto_preview_links").fetchone() == (1,int(supported),"single_shot",int(expected_status==1))


@pytest.mark.asyncio
async def test_missing_auto_source_keeps_failed_action(environment, tmp_path):
    from .test_acceptance import _accept, _plan_body
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body(actions=[auto_preview("preview","missing")]))
    assert connection.execute("SELECT status,error_code FROM actions").fetchone() == (4,2)
    assert connection.execute("SELECT source_action_id,preview_support,parameter_type,is_valid FROM auto_preview_links").fetchone() == (None,None,None,0)


@pytest.mark.asyncio
async def test_all_conflicts_preserve_complete_details(environment, tmp_path):
    from dataclasses import replace
    from .test_acceptance import _accept, _plan_body
    from camctl.contracts.json_values import parse_exact_json
    connection, context = environment
    original = context.catalog.parameter_definition
    context.catalog.parameter_definition = lambda *args: replace(original(*args), preview_supported=True)
    a, b = auto_preview("a","shoot"), auto_preview("b","shoot")
    a["scheduled_at"] = b["scheduled_at"] = camera_action()["scheduled_at"]
    b["policy"] = {"max_delay_ms":1}
    await _accept(environment, tmp_path, _plan_body(actions=[a,camera_action(),b]))
    rows = connection.execute("SELECT error_code,error_details_json FROM actions WHERE name IN ('a','b') ORDER BY name").fetchall()
    assert [r[0] for r in rows] == [3,3]
    details = [parse_exact_json(r[1]) for r in rows]
    assert all(detail["source_action_name"] == "shoot" and detail["obtain_action_names"] == ["a","b"] for detail in details)
    assert "issues" not in details[0]
    assert any(issue["field"] == "actions[2].policy.max_delay_ms" and issue["reason"] == "unsupported" for issue in details[1]["issues"])
    assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("action_type", ["obtain_action_outputs", "delete_action_outputs"])
def test_manual_local_reference_error_survives_other_field_failure(action_type):
    action = manual_obtain("fetch", "missing")
    action["type"] = action_type
    if action_type == "delete_action_outputs":
        action["params"].pop("purpose")
    action["scheduled_at"] = "2026-02-30 09:00:00"
    prepared = _prepare([action, camera_action()]).actions[0]
    issues = prepared.failure_details["issues"]
    assert any(i["field"] == "actions[0].scheduled_at" for i in issues)
    assert any(i["field"] == "actions[0].params.source.action_name" and i["reason"] == "reference" and i["value"] == "missing" for i in issues)
    assert prepared.source_resolution_state is None and prepared.in_plan_dependencies == ()
