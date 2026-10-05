"""报告维护流程装配进 run 会话的组件集成测试。

真实装配（execute_command 默认生产报告流程）驱动完整报告链：到
期同步动作开始、冻结依据登记、真实子进程生成 staging 文件、发布
与本地完成保存，报告责任清空后会话正常退出。覆盖诊断报告闭环、
生成失败保留责任并按报告错误退出、状态库错误停止会话。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.contracts.values import new_operation_key
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.publication import parse_report_file_name
from camctl.reporting.policy import (
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
)

from ..acceptance.test_acceptance import Catalog
from ..persistence.test_runtime import _create_valid_database

register_report_guards()
register_sync_guard()
pytestmark = pytest.mark.asyncio


def _config_for(home: Path):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            }
        },
        ConfigDefaults(),
    )


def _plan_body(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "status",
                "type": "report_status",
                "params": {"scope": "full"},
            }
        ],
    }


async def _parsed(tmp_path: Path, body: dict):
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body), encoding="utf-8")

    class Reader:
        def read(self, path: str) -> bytes:
            return Path(path).read_bytes()

    return parse_input(await read_input(str(target), Reader()))


async def _accept(owned, tmp_path: Path, request_id: str) -> None:
    from camctl.acceptance.service import AcceptanceContext, accept_input
    from camctl.persistence.repositories.acceptance import (
        AcceptanceRepository,
        register_acceptance_guards,
    )

    register_acceptance_guards()
    result = await accept_input(
        await _parsed(tmp_path, _plan_body(request_id)),
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=Catalog(),
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        ),
        new_operation_key(),
        owned,
    )
    assert result.plan_id is not None


def _scalar(db_path: Path, sql: str, parameters=()):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, parameters).fetchone()


def _all(db_path: Path, sql: str, parameters=()):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, parameters).fetchall()


@pytest.fixture
def environment(tmp_path):
    cfg = _config_for(tmp_path)
    assert initialize_state(
        cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    yield deps, cfg, tmp_path
    close_runtime(deps)


class TestReportFlowClosure:
    async def test_run_completes_report_chain_and_exits(
        self, environment,
    ) -> None:
        deps, cfg, tmp_path = environment
        db_path = Path(cfg.paths.state_db)
        source = await _parsed(tmp_path, _plan_body("1"))

        outcome = await asyncio.wait_for(
            execute_command(deps, source), 120)

        # 报告责任处理完毕：会话正常退出。
        assert outcome.succeeded is True, outcome.details
        # 同步动作开始、本地完成并成功结束。
        assert _scalar(db_path, "SELECT status FROM actions WHERE id = 1") == (3,)
        sync = _scalar(
            db_path,
            "SELECT mode, from_wm, local_report_id, status FROM state_syncs")
        assert sync == (1, 0, 1, 1)
        # 所有已建报告发布完成，字节与发布事实落库；覆盖本地保存的
        # 收尾事件可能追加第二份报告，同样发布。
        reports = _all(
            db_path, "SELECT id, status, size_bytes, sha256 FROM reports ORDER BY id")
        assert reports, "会话退出前应至少发布一份报告"
        for report_id, status, size, sha in reports:
            assert status == 4 and size > 0 and sha is not None
        # ready 中恰好一份报告文件；staging 无残留。
        ready_files = list((tmp_path / "ready").glob("status-report-*.json"))
        assert len(ready_files) == 1
        identity = parse_report_file_name(ready_files[0].name)
        assert identity is not None
        published = next(r for r in reports if r[0] == identity.report_id)
        assert identity.sha256 == published[3]
        staging_reports = tmp_path / "staging" / "reports"
        assert not any(staging_reports.iterdir())

    async def test_diagnostic_only_report_publishes_and_exits(
        self, environment,
    ) -> None:
        deps, cfg, tmp_path = environment
        db_path = Path(cfg.paths.state_db)
        broken = tmp_path / "broken.json"
        broken.write_bytes(b'{"request_id": "2", "actions": [')
        source = parse_input(await read_input(
            str(broken),
            type("R", (), {"read": staticmethod(lambda p: Path(p).read_bytes())})(),
        ))

        outcome = await asyncio.wait_for(
            execute_command(deps, source), 120)

        assert outcome.succeeded is True, outcome.details
        assert _scalar(
            db_path, "SELECT COUNT(*) FROM plan_file_diagnostics") == (1,)
        assert _scalar(db_path, "SELECT status FROM reports") == (4,)
        assert list((tmp_path / "ready").glob("status-report-*.json"))

    async def test_generation_failure_keeps_responsibility_and_exits_report_error(
        self, environment,
    ) -> None:
        deps, cfg, tmp_path = environment
        db_path = Path(cfg.paths.state_db)
        # 报告 staging 子目录的位置被普通文件占位：生成无法创建目录。
        staging = tmp_path / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "reports").write_text("blocked", encoding="utf-8")
        source = await _parsed(tmp_path, _plan_body("3"))

        outcome = await asyncio.wait_for(
            execute_command(deps, source), 120)

        # 生成失败保留责任：会话按报告错误退出，不轮询重试。
        assert outcome.succeeded is False
        assert outcome.reason == "report_error"
        row = _scalar(
            db_path, "SELECT status, last_error_json FROM reports")
        assert row[0] == 5 and row[1] is not None

    async def test_broken_frozen_basis_stops_session_as_state_error(
        self, tmp_path,
    ) -> None:
        cfg = _config_for(tmp_path)
        db_path = Path(cfg.paths.state_db)
        _create_valid_database(db_path)
        owned = open_existing(db_path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            await _accept(owned, tmp_path, "1")
            frozen = ReportingRepository().freeze_report(
                new_operation_key(), owned, occurred_at=1)
            assert frozen.kind.value == "completed"
            # 破坏冻结边界的完整性：指向所属事务的中间事件后，该位
            # 置不再是完整已提交边界，核验按状态库错误收场。
            mid_transaction = owned.connection.execute(
                "SELECT e.id FROM history_events e"
                " JOIN history_transactions t ON t.id = e.transaction_id"
                " WHERE e.id < (SELECT created_event_id FROM reports)"
                " AND e.id < t.last_event_id ORDER BY e.id LIMIT 1").fetchone()
            assert mid_transaction is not None
            owned.connection.execute(
                "UPDATE reports SET frozen_event_id = ?", (mid_transaction[0],))
            owned.connection.commit()
        finally:
            owned.connection.close()
        deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
        try:
            outcome = await asyncio.wait_for(
                execute_command(deps, None), 120)
        finally:
            close_runtime(deps)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"
