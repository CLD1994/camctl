"""R2 原子冻结的组件集成测试：真实 SQLite 与 P3 事务组合。

冻结取完整 H 与范围、不含本事务事件；并发受理后冻结；旧报告
内容不因后续变化改变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from unit.acceptance.helpers import StubCatalog
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.reporting.models import FrozenReport, validate_frozen_report
from camctl.reporting.policy import (
    ReportDecisionKind,
    ReportOpportunity,
    ReportingRepository,
    decide_report,
)

from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()

from camctl.reporting.policy import register_report_guards

register_report_guards()

pytestmark = pytest.mark.asyncio

CATALOG = StubCatalog()


def _plan_body(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
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


class _Reader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


async def _submit(owned: OwnedConnection, tmp_path: Path, request_id: str) -> None:
    target = tmp_path / f"plan-{request_id}.json"
    target.write_text(json.dumps(_plan_body(request_id)), encoding="utf-8")
    parsed = parse_input(await read_input(str(target), _Reader()))
    await accept_input(
        parsed,
        AcceptanceContext(
            mode=CommandMode.SUBMIT,
            catalog=CATALOG,
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    )


@pytest.fixture()
def environment(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    yield owned
    owned.connection.close()


def _latest_wm(connection) -> int:
    row = connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone()
    return int(row[0]) if row[0] is not None else 0


class TestFreeze:
    async def test_freeze_takes_complete_boundary_excluding_own_event(
        self, environment, tmp_path
    ) -> None:
        await _submit(environment, tmp_path, "1")
        latest = _latest_wm(environment.connection)
        decision = decide_report(
            ReportOpportunity(
                kind="normal", requested_from_wm=0, latest_change_wm=latest,
                acknowledged_wm=0,
            )
        )
        assert decision.kind is ReportDecisionKind.GENERATE
        outcome = ReportingRepository().freeze_report(
            decision, new_operation_key(), environment, occurred_at=1
        )
        assert outcome.kind.value == "completed"
        report: FrozenReport = outcome.value
        validate_frozen_report(report)
        # 冻结依据是受理事务的完整边界；REPORT_CHANGED 事件本身不进入 H。
        assert report.boundary.last_event_id >= 1
        later = _latest_wm(environment.connection)
        assert later >= latest
        row = environment.connection.execute(
            "SELECT frozen_event_id, from_wm, to_wm, status FROM reports WHERE id = 1"
        ).fetchone()
        assert row[1] == 0 and row[2] == latest and row[3] == 1

    async def test_frozen_report_excludes_later_changes(
        self, environment, tmp_path
    ) -> None:
        await _submit(environment, tmp_path, "1")
        latest = _latest_wm(environment.connection)
        decision = decide_report(
            ReportOpportunity(
                kind="normal", requested_from_wm=0, latest_change_wm=latest,
                acknowledged_wm=0,
            )
        )
        first = ReportingRepository().freeze_report(
            decision, new_operation_key(), environment, occurred_at=1
        )
        frozen_to = first.value.to_wm
        # 冻结后新提交：旧报告范围不变。
        await _submit(environment, tmp_path, "2")
        row = environment.connection.execute(
            "SELECT from_wm, to_wm FROM reports WHERE id = 1"
        ).fetchone()
        assert row == (0, frozen_to)
        later_wm = _latest_wm(environment.connection)
        assert later_wm > frozen_to

    async def test_wider_existing_report_satisfies_narrower_need(
        self, environment, tmp_path
    ) -> None:
        await _submit(environment, tmp_path, "1")
        latest = _latest_wm(environment.connection)
        repository = ReportingRepository()
        wide = repository.freeze_report(
            decide_report(
                ReportOpportunity(
                    kind="full_sync", requested_from_wm=0, latest_change_wm=latest,
                    acknowledged_wm=0,
                )
            ),
            new_operation_key(), environment, occurred_at=1,
        )
        assert wide.kind.value == "completed"
        # 更窄需求被已有宽报告满足：不再生成新报告。
        narrow = decide_report(
            ReportOpportunity(
                kind="normal",
                requested_from_wm=max(0, latest - 1),
                latest_change_wm=latest,
                acknowledged_wm=0,
                existing_report_coverages=((wide.value.report_id, 0, latest),),
            )
        )
        assert narrow.kind is ReportDecisionKind.REUSE
        assert narrow.reused_report_id == wide.value.report_id
        count = environment.connection.execute(
            "SELECT COUNT(*) FROM reports"
        ).fetchone()[0]
        assert count == 1
