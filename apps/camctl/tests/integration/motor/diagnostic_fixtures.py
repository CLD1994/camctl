"""电机诊断经生产流程工厂与真实业务数据库线程消费。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest_asyncio

from camctl.bootstrap import motor_assembly
from camctl.contracts.values import new_operation_key
from camctl.motor.notification import NotificationWriter, NotificationWriteResult, WriteKind
from camctl.persistence.repositories.motor import MotorRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import ClockCheckInput
from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest
from ..support.database import business_database, create_test_database
from ..persistence.test_motor_transactions import _admit, _body, NOW

@pytest_asyncio.fixture
async def motor_database(tmp_path):
    path = tmp_path / "motor.db"
    create_test_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    body["actions"][0]["policy"] = {"max_delay_ms": 1}
    _admit(owned, body)
    observed = SchedulingRepository().observe_window(
        ObserveWindowRequest(1, NOW, NOW), new_operation_key(), owned)
    assert observed.kind.value == "completed", observed.error
    try:
        async with business_database(path) as database:
            yield owned, database
    finally:
        owned.connection.close()


def motor_flow_world(monkeypatch, motor_database, times=(1000, 1000)):
    """在公开仓储端口控制诊断分区，实际读写仍由原worker拥有。"""
    owned, database = motor_database
    repository = MotorRepository()
    repo = Mock(spec=MotorRepository, wraps=repository)
    writer = Mock(spec=NotificationWriter)
    writer.available = True
    writer.send.return_value = NotificationWriteResult(WriteKind.WRITTEN, 76)
    clock = SimpleNamespace(utc_micros=Mock(side_effect=(NOW, *(NOW + value - 1000 for value in times))))
    context = SimpleNamespace(database=database, clock=clock,
        clock_policy=lambda connection: ClockCheckInput(None, 0, 0, 0))
    monkeypatch.setattr(motor_assembly, "MotorRepository", lambda: repo)
    flow = motor_assembly.motor_flow(writer, {})
    async def advance():
        await flow(context)
    runtime = SimpleNamespace(database=database, advance=advance)
    return runtime, repo, writer, repository.read_facts(1, owned).action
