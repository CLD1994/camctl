"""公开残留流程的保存前缀：顺序、异常分类和独立连接关闭。"""

import asyncio
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.flows import residual_flow
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import ConsistencyError
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.session.service import (
    AcceptanceRepositoryPort, SessionContext, SessionPaths, SessionRepositoryPort, StateDbFailure,
)


pytestmark = pytest.mark.asyncio
_BEFORE_FAILURE = {
    "read": ["read"],
    "files": ["read", "files"],
    "media": ["read", "files", "media-start"],
}
_COMPLETE_PREFIX = ["read", "files", "media-start", "media-complete"]


class _BeforeBusinessQuery(Exception):
    """在任何真实业务查询之前截停，不解释业务 SQL 或伪造查询结果。"""


def _world(*, expected_prefix, failed_stage=None, failure=None):
    events = []
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    owned = OwnedConnection(connection, DatabaseMetadata(
        application_id="camctl", instance_id="a" * 32, format_version=1,
        staging_path="/staging", ready_path="/ready", processing_path="/processing"))

    def query(sql, parameters=()):
        assert events == expected_prefix, "残留流程必须完成保存前缀，再查询旧尝试或业务候选"
        events.append("query")
        raise _BeforeBusinessQuery

    connection.execute.side_effect = query
    opener = create_autospec(lambda: owned, return_value=owned)
    context = SessionContext(mode=CommandMode.RUN, catalog=None,
        clock=create_autospec(ClockPort, instance=True, spec_set=True),
        open_connection=opener,
        acceptance_repository=create_autospec(AcceptanceRepositoryPort, instance=True, spec_set=True),
        session_repository=create_autospec(SessionRepositoryPort, instance=True, spec_set=True),
        paths=SessionPaths("session.lock", "admission.lock"),
        clock_policy=lambda _owned: None, acquire_session=lambda: None,
        acquire_admission=lambda: None, facts_query=lambda _owned: None)

    def read(current):
        assert current is owned
        events.append("read")
        if failed_stage == "read":
            raise failure

    def files(current):
        assert current is owned
        events.append("files")
        if failed_stage == "files":
            raise failure

    async def media_complete():
        events.append("media-complete")

    async def media(current):
        assert current is owned
        events.append("media-start")
        if failed_stage == "media":
            raise failure
        await media_complete()

    factory = create_autospec(lambda current, device_id: None,
        side_effect=AssertionError("保存前缀及业务候选查询之前不能构造拍摄 runtime"))
    callbacks = {"resume_read_results": read, "resume_file_observations": files,
                 "resume_media_results": media}
    return context, owned, events, factory, callbacks


async def test_residual_recovery_completes_read_files_and_awaited_media_before_query():
    context, owned, events, factory, callbacks = _world(expected_prefix=_COMPLETE_PREFIX)
    with pytest.raises(_BeforeBusinessQuery):
        await residual_flow(factory, **callbacks)(context)
    assert events == [*_COMPLETE_PREFIX, "query"]
    context.open_connection.assert_called_once_with()
    owned.connection.execute.assert_called_once()
    factory.assert_not_called()
    owned.connection.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["read", "files", "media"])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, ConsistencyError],
                         ids=["sqlite-error", "consistency-error"])
async def test_residual_recovery_state_failure_stops_later_work_and_closes_connection(stage, error_type):
    failure = error_type("原事实保存未取得可靠结果")
    context, owned, events, factory, callbacks = _world(
        expected_prefix=_COMPLETE_PREFIX, failed_stage=stage, failure=failure)
    with pytest.raises(StateDbFailure) as caught:
        await residual_flow(factory, **callbacks)(context)
    assert caught.value.__cause__ is failure
    assert events == _BEFORE_FAILURE[stage]
    owned.connection.execute.assert_not_called()
    factory.assert_not_called()
    owned.connection.close.assert_called_once_with()


@pytest.mark.parametrize("stage,error_type", [
    ("read", StateDbFailure),
    ("files", ValueError),
    ("media", asyncio.CancelledError),
], ids=["existing-state-failure", "ordinary-value-error", "cancellation"])
async def test_residual_recovery_preserves_original_exception_class_and_identity(stage, error_type):
    failure = error_type("原协作者已经停止")
    context, owned, events, factory, callbacks = _world(
        expected_prefix=_COMPLETE_PREFIX, failed_stage=stage, failure=failure)
    with pytest.raises(error_type) as caught:
        await residual_flow(factory, **callbacks)(context)
    assert caught.value is failure and caught.value.__cause__ is None
    assert events == _BEFORE_FAILURE[stage]
    owned.connection.execute.assert_not_called()
    factory.assert_not_called()
    owned.connection.close.assert_called_once_with()


async def test_residual_flow_without_recovery_callbacks_keeps_optional_public_api():
    context, owned, events, factory, _callbacks = _world(expected_prefix=[])
    with pytest.raises(_BeforeBusinessQuery):
        await residual_flow(factory)(context)
    assert events == ["query"]
    factory.assert_not_called()
    owned.connection.close.assert_called_once_with()
