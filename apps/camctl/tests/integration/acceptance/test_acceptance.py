"""A4 请求复用、独立 ACK 与原子提交的组件集成测试。

真实 SQLite 与 P3 事务内核组合：六分区决策表、重送跳过正文校验、
拒绝不阻断 ACK、全失败动作原子注册。正式业务守卫在导入时注册。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import (
    AcceptanceContext,
    AckDisposition,
    CommandMode,
    PlanDisposition,
    accept_input,
)
from camctl.acceptance.ports import ParameterDefinition
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000

CAMERA_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "single_shot"},
        "shots": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["type"],
    "additionalProperties": False,
}


class Catalog:
    """受静态目录端口约束的替身。"""

    def action_types(self):
        return frozenset(
            {"camera_take_photo", "camera_record", "camera_timelapse", "obtain_action_outputs", "delete_action_outputs", "cancel_task", "report_status"}
        )

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type.startswith("camera_")

    def parameter_definition(self, device_id, action_type, parameter_type):
        if device_id == "cam-1" and action_type.startswith("camera_"):
            return ParameterDefinition(
                schema=CAMERA_DEFINITION, defaults={"shots": 1}
            )
        return None


class RealFileReader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


@pytest.fixture()
def environment(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    context = AcceptanceContext(
        mode=CommandMode.RUN,
        catalog=Catalog(),
        repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
    )
    yield owned.connection, context
    owned.connection.close()


def _write_input(tmp_path: Path, body: dict) -> Path:
    import json

    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return target


def _plan_body(*, request_id: str = "42", ack: str | None = None, actions=None) -> dict:
    body = {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": actions
        if actions is not None
        else [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 1000},
            }
        ],
    }
    if ack is not None:
        body["last_report_id"] = ack
    return body


async def _accept(environment, tmp_path: Path, body: dict):
    connection, context = environment
    path = _write_input(tmp_path, body)
    read = await read_input(str(path), RealFileReader())
    return await accept_input(parse_input(read), context, new_operation_key(), _owned(environment))


def _owned(environment):
    from camctl.persistence.runtime import OwnedConnection

    connection, _ = environment
    return OwnedConnection(connection=connection, metadata=None)


async def _seed_report(environment, tmp_path: Path, report_id: int) -> None:
    """提交一次填充受理，再用真实冻结入口分配所需报告身份。"""
    await _accept(environment, tmp_path, _plan_body(request_id=str(900 + report_id)))
    _freeze_reports(environment, report_id)


def _freeze_reports(environment, report_id: int) -> None:
    from camctl.reporting.policy import (
        ReportDecision, ReportDecisionKind, ReportingRepository, register_report_guards,
    )

    register_report_guards()
    connection, _ = environment
    previous = connection.execute("SELECT MAX(id) FROM reports").fetchone()[0] or 0
    latest = connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone()[0] or 0
    for expected_id in range(previous + 1, report_id + 1):
        outcome = ReportingRepository().freeze_report(
            ReportDecision(ReportDecisionKind.GENERATE, 0, latest),
            new_operation_key(), _owned(environment), occurred_at=_NOW,
        )
        assert outcome.kind.value == "completed"
        assert outcome.value.report_id == expected_id


class TestFirstAcceptance:
    async def test_register_plan_actions_links_and_catalogs(self, environment, tmp_path) -> None:
        connection, _ = environment
        body = _plan_body(
            actions=[
                {
                    "name": "shoot",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": "2026-01-15 09:00:00",
                    "params": {"type": "single_shot", "shots": 2},
                    "policy": {"max_delay_ms": 1500},
                },
                {
                    "name": "fetch",
                    "type": "obtain_action_outputs",
                    "scheduled_at": "2026-01-15 10:00:00",
                    "params": {
                        "source": {"action_name": "shoot"},
                        "purpose": "manual",
                    },
                },
            ]
        )
        result = await _accept(environment, tmp_path, body)
        assert result.plan_disposition is PlanDisposition.REGISTERED
        assert result.plan_id == 1
        assert result.ack_disposition is AckDisposition.NOT_PROVIDED

        plan = connection.execute(
            "SELECT request_id, status, change_count FROM plans"
        ).fetchone()
        assert plan == (42, 1, 1)
        actions = connection.execute(
            "SELECT id, name, status, source_resolution_state, resolved_source_plan_id"
            " FROM actions ORDER BY id"
        ).fetchall()
        assert actions == [(1, "shoot", 1, None, None), (2, "fetch", 1, 2, 1)]
        dep = connection.execute(
            "SELECT action_id, depends_on_action_id FROM action_dependencies"
        ).fetchone()
        assert dep == (2, 1)
        links = connection.execute(
            "SELECT entity_type, entity_id, change_count FROM entity_event_links ORDER BY id"
        ).fetchall()
        # J-04：成员事件与接纳事件分别推进取回动作的自身计数。
        assert links == [(1, 1, 1), (1, 2, 1), (1, 2, 2), (4, 1, 1)]
        # 事件顺序：动作、来源成员、计划。
        events = connection.execute(
            "SELECT id, event_type FROM history_events ORDER BY id"
        ).fetchall()
        assert events == [(1, 2), (2, 2), (3, 3), (4, 1)]
        assert connection.execute(
            "SELECT COUNT(*) FROM report_entity_changes"
        ).fetchone()[0] == 4

    async def test_all_failed_actions_register_atomically(self, environment, tmp_path) -> None:
        connection, _ = environment
        body = _plan_body(
            actions=[
                {
                    "name": "bad-1",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": "2026-01-15 09:00:00",
                    "params": {"type": "single_shot", "shots": 99},
                    "policy": {"max_delay_ms": 1000},
                },
                {
                    "name": "bad-2",
                    "type": "camera_take_photo",
                    "device_id": "cam-x",
                    "scheduled_at": "2026-01-15 09:00:00",
                    "params": {"type": "single_shot"},
                    "policy": {"max_delay_ms": 1000},
                },
            ]
        )
        result = await _accept(environment, tmp_path, body)
        assert result.plan_disposition is PlanDisposition.REGISTERED
        assert connection.execute("SELECT status FROM plans").fetchone()[0] == 3
        statuses = connection.execute("SELECT status, error_code FROM actions ORDER BY id").fetchall()
        assert statuses == [(4, 1), (4, 1)]

    async def test_whole_rejection_saves_diagnostic_only(self, environment, tmp_path) -> None:
        connection, _ = environment
        body = _plan_body(actions=[])
        result = await _accept(environment, tmp_path, body)
        assert result.plan_disposition is PlanDisposition.REJECTED
        assert result.diagnostic_id == 1
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        errors = connection.execute("SELECT errors_json FROM plan_file_diagnostics").fetchone()[0]
        assert "plan_body_rejected" in errors

    async def test_register_with_absorbing_ack_saves_both(self, environment, tmp_path) -> None:
        connection, _ = environment
        await _accept(environment, tmp_path, _plan_body(request_id="1"))
        await _seed_report(environment, tmp_path, 9)
        result = await _accept(
            environment, tmp_path, _plan_body(request_id="88", ack="9")
        )
        assert result.plan_disposition is PlanDisposition.REGISTERED
        # _seed_report 的填充受理也占用一个计划身份。
        assert result.plan_id == 3
        assert result.ack_disposition is AckDisposition.ABSORBED
        assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 3

    async def test_register_with_invalid_ack_saves_plan_and_diagnostic(
        self, environment, tmp_path
    ) -> None:
        connection, _ = environment
        result = await _accept(
            environment, tmp_path, _plan_body(request_id="89", ack="404")
        )
        assert result.plan_disposition is PlanDisposition.REGISTERED
        assert result.ack_disposition is AckDisposition.INVALID
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
        errors = connection.execute(
            "SELECT errors_json FROM plan_file_diagnostics"
        ).fetchone()[0]
        assert "invalid_ack" in errors
        assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == 0


class TestRequestReuse:
    async def test_retry_skips_body_validation(self, environment, tmp_path) -> None:
        connection, _ = environment
        first = await _accept(environment, tmp_path, _plan_body())
        assert first.plan_disposition is PlanDisposition.REGISTERED
        original_actions = connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

        # 同一 request_id 重送：正文缺失 actions 也不参与校验。
        again = await _accept(environment, tmp_path, {"request_id": "42"})
        assert again.plan_disposition is PlanDisposition.REUSED
        assert again.plan_id == first.plan_id
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == original_actions
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
        # 只读路径不产生新历史事件。
        assert connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == 2

    async def test_reuse_with_advancing_ack_saves_watermark(self, environment, tmp_path) -> None:
        connection, _ = environment
        await _accept(environment, tmp_path, _plan_body())
        await _seed_report(environment, tmp_path, 11)
        again = await _accept(environment, tmp_path, _plan_body(request_id="42", ack="11"))
        assert again.plan_disposition is PlanDisposition.REUSED
        assert again.ack_disposition is AckDisposition.ABSORBED
        assert again.ack_watermark == 4
        assert connection.execute(
            "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state"
        ).fetchone() == (4, 11)


class TestAckIndependence:
    async def test_rejection_does_not_block_ack(self, environment, tmp_path) -> None:
        connection, _ = environment
        # 先建立已提交历史，为报告登记提供冻结与创建依据。
        await _accept(environment, tmp_path, _plan_body(request_id="1"))
        await _seed_report(environment, tmp_path, 3)
        body = _plan_body(request_id="77", ack="3", actions=[])
        result = await _accept(environment, tmp_path, body)
        assert result.plan_disposition is PlanDisposition.REJECTED
        assert result.ack_disposition is AckDisposition.ABSORBED
        assert result.ack_watermark == 4
        assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == 4

    async def test_invalid_ack_saved_as_diagnostic(self, environment, tmp_path) -> None:
        connection, _ = environment
        body = _plan_body(request_id="78", ack="404", actions=[])
        result = await _accept(environment, tmp_path, body)
        assert result.ack_disposition is AckDisposition.INVALID
        assert result.ack_watermark == 0
        errors = connection.execute("SELECT errors_json FROM plan_file_diagnostics").fetchone()[0]
        assert "invalid_ack" in errors
        assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == 0

    async def test_valid_ack_not_advancing_keeps_watermark(self, environment, tmp_path) -> None:
        connection, _ = environment
        await _seed_report(environment, tmp_path, 5)
        _freeze_reports(environment, 6)
        body = _plan_body(request_id="79", ack="5")
        await _accept(environment, tmp_path, body)
        again = await _accept(environment, tmp_path, _plan_body(request_id="79", ack="6"))
        assert again.plan_disposition is PlanDisposition.REUSED
        assert again.ack_disposition is AckDisposition.VALID_NOT_ADVANCING
        assert again.ack_watermark == 2
        assert connection.execute(
            "SELECT acknowledged_report_id FROM runtime_state"
        ).fetchone()[0] == 5

    async def test_parse_failure_saves_diagnostic_without_ack(self, environment, tmp_path) -> None:
        connection, context = environment
        target = tmp_path / "broken.json"
        target.write_bytes(b'{"request_id": "42", "last_report_id": "9"')
        read = await read_input(str(target), RealFileReader())
        parsed = parse_input(read)
        result = await accept_input(parsed, context, new_operation_key(), _owned(environment))
        assert result.plan_disposition is PlanDisposition.REJECTED
        assert result.ack_disposition is AckDisposition.NOT_PROCESSED
        assert result.ack_watermark == 0
        assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


@pytest.mark.parametrize("identity", [None, True, 42, [], {}, "", "0", "042", "+42", "-42", " 42 ", "4.2", "4e1", "４２", "9223372036854775808"])
async def test_identity_rejected_before_reuse(environment, tmp_path, identity):
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    result = await _accept(environment, tmp_path, {"request_id": identity})
    assert result.plan_disposition is PlanDisposition.REJECTED
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert connection.execute("SELECT request_id FROM plan_file_diagnostics").fetchone()[0] is None


@pytest.mark.parametrize("plan_partition", ["new", "reuse", "rejected", "missing_identity"])
@pytest.mark.parametrize("ack_partition", ["missing", "invalid", "known", "old"])
async def test_plan_ack_matrix(environment, tmp_path, plan_partition, ack_partition):
    connection, _ = environment
    await _seed_report(environment, tmp_path, 1)
    if plan_partition == "reuse":
        await _accept(environment, tmp_path, _plan_body())
    if ack_partition == "old":
        await _accept(environment, tmp_path, _plan_body(request_id="51", ack="1"))
    before_count = connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    body = _plan_body()
    if plan_partition == "reuse":
        body = {"request_id": "42"}
    elif plan_partition == "rejected":
        body["actions"] = []
    elif plan_partition == "missing_identity":
        del body["request_id"]
    if ack_partition != "missing":
        body["last_report_id"] = None if ack_partition == "invalid" else "1"
    result = await _accept(environment, tmp_path, body)
    assert result.plan_disposition is {
        "new": PlanDisposition.REGISTERED, "reuse": PlanDisposition.REUSED,
        "rejected": PlanDisposition.REJECTED, "missing_identity": PlanDisposition.REJECTED,
    }[plan_partition]
    assert result.ack_disposition is {
        "missing": AckDisposition.NOT_PROVIDED, "invalid": AckDisposition.INVALID,
        "known": AckDisposition.ABSORBED, "old": AckDisposition.VALID_NOT_ADVANCING,
    }[ack_partition]
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == before_count + (plan_partition == "new")
    assert result.ack_watermark == (2 if ack_partition in {"known", "old"} else 0)


@pytest.mark.parametrize("ack", [True, 1, "01", "+1", " 1 ", "x", {}, "9223372036854775808"])
async def test_invalid_ack_keeps_valid_plan(environment, tmp_path, ack):
    connection, _ = environment
    await _seed_report(environment, tmp_path, 1)
    body = _plan_body()
    body["last_report_id"] = ack
    result = await _accept(environment, tmp_path, body)
    assert result.plan_disposition is PlanDisposition.REGISTERED
    assert result.ack_disposition is AckDisposition.INVALID
    assert result.ack_watermark == 0


async def test_root_array_rejected_without_ack_processing(environment, tmp_path):
    result = await _accept(environment, tmp_path, [])
    assert result.plan_disposition is PlanDisposition.REJECTED
    assert result.ack_disposition.value == "not_processed"
