"""设备检查失败交付的仓储存储格式与真实事务测试。

按 SQL 格式准备已失败的检查和仍占用机身的旧活动；真实数据库线程调用
正式保存入口。不包含设备检查、协调派发或错误诊断链。
"""

import json
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import action_error_id
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.availability_failure import (
    FailureDisposition,
    fail_photo_availability,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.reporting.policy import register_report_guards

from ..support.database import business_database, create_test_database

register_acceptance_guards()
register_report_guards()
register_outputs_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000
_DETAILS = {"device_id": "cam-1", "operation_run_id": "1", "attempts_used": 2, "max_attempts": 2}


def _prepare(path, *, unfinished_sibling):
    create_test_database(path)
    identity = CameraIdentity(serial="camera-1", product_id=1234)
    fixed_identity = json.dumps({"serial": identity.serial, "product_id": identity.product_id})
    params = {"type": "hasselblad_photo", "capture_format": "raw",
              "exposure": {"mode": "P", "iso": 100},
              "white_balance": {"mode": "auto"}, "focus": {"mode": "MF"}}
    input_fields = {"params": params, "policy": {"max_delay_ms": 1000}}
    execution_spec = {"output_storage": "host", "start_call_timeout_s": 180}
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO history_transactions VALUES (1, ?, 1, 1)", ("f" * 32,))
        db.execute(
            "INSERT INTO history_events VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
            (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
        )
        for plan_id, status in ((1, 1), (2, 2)):
            db.execute(
                "INSERT INTO plans (id, request_id, name, created_at, status, created_event_id,"
                " last_event_id, change_count) VALUES (?, ?, ?, ?, ?, 1, 1, 1)",
                (plan_id, 4000 + plan_id, f"plan-{plan_id}", _NOW, status),
            )
        # 动作 1 是触发检查的单张拍摄，动作 2 属于旧计划且占用同一机身。
        actions = [(1, 1, 0, 1, 0), (2, 2, 0, 2, 1)]
        if unfinished_sibling:
            actions.append((3, 1, 1, 1, 0))
        for action_id, plan_id, index, status, started in actions:
            db.execute(
                "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
                " scheduled_at, input_fields_json, effective_params_json, driver_id, max_delay_ms,"
                " execution_spec_json, status, execution_started, cancel_requested,"
                " created_event_id, last_event_id, change_count)"
                " VALUES (?, ?, ?, ?, 1, 'cam-1', ?, ?, ?,"
                " 'hasselblad_cfv100c', 1000, ?,"
                " ?, ?, 0, 1, 1, 1)",
                (action_id, plan_id, index, f"photo-{action_id}", _NOW, json.dumps(input_fields),
                 json.dumps(params), json.dumps(execution_spec), status, started),
            )
        db.execute(
            "INSERT INTO device_activities (id, action_id, task_key, expected_identity_json,"
            " resource_key, state_query_supported, stop_supported, safe_repeat_stop,"
            " start_return_meaning, completion_mode, ownership_mode, output_scope_json,"
            " baseline_state, dispatch_state, activity_state, occupancy_state, result_set_state)"
            " VALUES (1, 2, ?, ?, ?, 0, 0, 0, 3, 1, 1, '{}', 1, 2, 2, 1, 1)",
            ("a" * 32, fixed_identity, identity.resource_key),
        )
        db.execute(
            "INSERT INTO device_availability_checks (id, action_id, device_id, driver_id,"
            " expected_identity_json, resource_key, rule_version, target_boundary_event_id)"
            " VALUES (1, 1, 'cam-1', 'hasselblad_cfv100c', ?, ?, 1, 1)",
            (fixed_identity, identity.resource_key),
        )
        db.execute("INSERT INTO device_availability_targets VALUES (1, 1, 1, 1)")
        error = {"code": "device_availability_unconfirmed", "stage": "execution", "details": _DETAILS}
        db.execute(
            "INSERT INTO operation_runs (id, action_id, kind, responsibility_key,"
            " availability_check_id, status, attempts_used, max_attempts_used, timeout_s_json,"
            " retry_interval_s_json, retry_wait_required, error_json)"
            " VALUES (1, 1, 12, 'availability/1', 1, 6, 2, 2, '5', '1', 0, ?)",
            (json.dumps(error),),
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unfinished_sibling", [True, False], ids=["sibling-pending", "last-action"])
async def test_photo_availability_failure_saves_action_and_completes_only_last_plan_member(
    tmp_path, unfinished_sibling
):
    path = tmp_path / "state.db"
    _prepare(path, unfinished_sibling=unfinished_sibling)
    key = new_operation_key()
    async with business_database(path) as database:
        async def save():
            return await database.write(
                lambda owned: fail_photo_availability(1, _NOW, _NOW, key, owned), key=key
            )

        outcome = await save()
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value is FailureDisposition.FAILED
        with sqlite3.connect(path) as db:
            action = db.execute(
                "SELECT status, execution_started, error_code, error_details_json FROM actions WHERE id=1"
            ).fetchone()
            assert action[:3] == (4, 0, action_error_id("device_availability_unconfirmed"))
            assert json.loads(action[3]) == _DETAILS
            assert db.execute("SELECT status FROM plans WHERE id=1").fetchone() == (
                1 if unfinished_sibling else 3,
            )
            assert db.execute("SELECT status FROM plans WHERE id=2").fetchone() == (2,)
            if unfinished_sibling:
                assert db.execute("SELECT status FROM actions WHERE id=3").fetchone() == (1,)
            events = db.execute(
                "SELECT e.transaction_id, e.event_type, json_extract(e.body_json, '$.reason')"
                " FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id"
                " WHERE t.operation_key=? ORDER BY e.id", (key,),
            ).fetchall()
            assert [(event_type, reason) for _, event_type, reason in events] == (
                [(8, 7)] if unfinished_sibling else [(8, 7), (9, 2)]
            )
            assert len({transaction_id for transaction_id, _, _ in events}) == 1
            before = tuple(db.iterdump())
        repeated = await save()
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert repeated.value is FailureDisposition.FAILED
        with sqlite3.connect(path) as db:
            assert tuple(db.iterdump()) == before
