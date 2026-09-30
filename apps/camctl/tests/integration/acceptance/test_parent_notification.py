"""A5 受理通知的组件集成测试：真实提交组合。

并发同请求提交恰好产生一次新工作通知；提交后通知前中断时，后
续会话从持久化状态重新发现工作，通知不依赖原等待者存在。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.notification import notify_acceptance
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.application import query_work_facts
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.session.work import WorkDecisionKind, classify_work

from ..persistence.test_runtime import _create_valid_database  # noqa: F401

pytestmark = pytest.mark.asyncio


def _plan(request_id: str) -> dict:
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


class RecordingNotifier:
    def __init__(self) -> None:
        self.notifications: list[int] = []

    def work_available(self, plan_id: int) -> None:
        self.notifications.append(plan_id)


class _Reader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


async def _submit(deps, tmp_path: Path, body: dict):
    target = tmp_path / f"plan-{body['request_id']}.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    read = await read_input(str(target), _Reader())
    return await execute_command(deps, parse_input(read))


@pytest.fixture()
def environment(tmp_path: Path):
    config = load_config(
        {"paths": {"state_db": str(tmp_path / "state.db")}}, ConfigDefaults()
    )
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    notifier = RecordingNotifier()
    deps = build_runtime(
        CommandMode.SUBMIT, config, catalog=None, notifier=notifier
    )
    yield deps, notifier, config
    close_runtime(deps)


class TestAcceptanceNotification:
    async def test_concurrent_same_request_notifies_once(self, environment, tmp_path) -> None:
        deps, notifier, _ = environment
        # 同一请求并发重送：数据库事务串行化，恰一次首次注册。
        outcomes = await asyncio.gather(
            _submit(deps, tmp_path, _plan("42")),
            _submit(deps, tmp_path, _plan("42")),
        )
        assert all(outcome.succeeded for outcome in outcomes)
        assert notifier.notifications == [1]

    async def test_distinct_requests_each_notify(self, environment, tmp_path) -> None:
        deps, notifier, _ = environment
        await _submit(deps, tmp_path, _plan("1"))
        await _submit(deps, tmp_path, _plan("2"))
        assert notifier.notifications == [1, 2]

    async def test_crash_before_notification_rediscovered_by_next_run(
        self, environment, tmp_path
    ) -> None:
        deps, notifier, config = environment
        # 模拟提交成功后、通知前进程退出：通知未发生。
        target = tmp_path / "plan-x.json"
        target.write_text(json.dumps(_plan("9")), encoding="utf-8")
        read = await read_input(str(target), _Reader())
        from camctl.acceptance.service import AcceptanceContext, accept_input
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

        owned = open_existing(
            Path(config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()
        )
        try:
            result = await accept_input(
                parse_input(read),
                AcceptanceContext(
                    mode=CommandMode.SUBMIT,
                    catalog=deps.catalog,
                    repository=AcceptanceRepository(),
                    clock=deps.clock if hasattr(deps, "clock") else None,
                ),
                new_operation_key(),
                owned,
            )
        finally:
            owned.connection.close()
        assert notifier.notifications == []

        # 后续会话从持久化状态重新发现工作（不依赖原通知）。
        from camctl.persistence.runtime import DbOpenMode

        owned = open_existing(
            Path(config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()
        )
        try:
            facts = query_work_facts(owned.connection)
        finally:
            owned.connection.close()
        assert classify_work(facts).kind is WorkDecisionKind.NEEDS_DRIVER
