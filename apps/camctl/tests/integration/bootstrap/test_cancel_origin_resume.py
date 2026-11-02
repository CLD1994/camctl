"""真实 cancel_flow 以新数据库线程核实原取消来源的收场阶段。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import cancel_flow
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.directory_switch import canonical_to_path
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..support.bootstrap_recovery import TransactionFault
from ..support.cancel_origin_recovery import NOW, origin_world
from ..support.database import business_database

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("phase", ["pending_finish", "fixed_stop", "fixed_finish"])
@pytest.mark.parametrize("receipt", ["before_commit", "after_commit", "exception"])
async def test_real_flow_resumes_original_phase_before_current_terminal(tmp_path, monkeypatch, phase, receipt):
    async with origin_world(tmp_path, fixed=phase != "pending_finish") as world:
        selected = "stop_wait_cancel_items" if phase == "fixed_stop" else "finish_origin_canceled"
        actual = getattr(CancellationRepository, selected)
        calls, outcomes = [], []

        def save(repository, request, key, owned):
            if request.action_id != 3:
                return actual(repository, request, key, owned)
            calls.append((request, key, owned.metadata.instance_id, repository))
            if len(calls) == 1:
                if receipt == "exception":
                    raise OSError("取消来源原保存回执中断")
                fault = TransactionFault(owned.connection, after_commit=receipt == "after_commit")
                result = actual(repository, request, key, replace(owned, connection=fault))
                assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            else:
                result = actual(repository, request, key, owned)
            outcomes.append(result)
            return result

        monkeypatch.setattr(CancellationRepository, selected, save)
        flow = cancel_flow(staging=canonical_to_path(world.database.metadata.staging_path),
            ready=tmp_path / "ready", processing=tmp_path / "processing")
        context = SimpleNamespace(database=world.database, clock=world.clock)
        with pytest.raises((StateDbFailure, ValueError, OSError)):
            await flow(context)
        assert len(calls) == 1
        command, key = calls[0][:2]
        assert command.occurred_at == NOW
        first = saved_transaction_events(world.owned.connection, key)
        assert (first is not None) is (receipt == "after_commit")
        await world.database.close()
        async with business_database(world.path) as database:
            context.database = database
            context.clock = SimpleNamespace(utc_micros=lambda: NOW + 1000)
            await flow(context)
            assert len(calls) == 2 and calls[1][:3] == calls[0][:3]
            assert calls[1][0] is command and calls[1][3] is not calls[0][3]
            assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
            events = saved_transaction_events(world.owned.connection, key)
            assert events and {event["occurred_at"] for event in events} == {NOW}
            if first is not None:
                assert events == first
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=3").fetchone() == (6,)
            assert world.owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (int(phase != "pending_finish"),)
            assert world.owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=2").fetchone() == (0,)
            assert world.owned.connection.execute("SELECT id FROM operation_runs WHERE kind IN (1,2)").fetchall() == []
            before = tuple(world.owned.connection.iterdump())
            await flow(context)
            assert len(calls) == 2 and tuple(world.owned.connection.iterdump()) == before


async def test_flow_rejects_other_database_then_resumes_original_pending_finish(tmp_path, monkeypatch):
    async with origin_world(tmp_path / "first") as world, origin_world(tmp_path / "second") as other:
        actual = CancellationRepository.finish_origin_canceled
        calls = []

        def save(repository, request, key, owned):
            if request.action_id != 3:
                return actual(repository, request, key, owned)
            calls.append((request, key, owned.metadata.instance_id))
            if len(calls) == 1:
                from camctl.persistence.models import DbOutcome
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("取消来源回执丢失"))
            return actual(repository, request, key, owned)

        monkeypatch.setattr(CancellationRepository, "finish_origin_canceled", save)
        flow = cancel_flow(staging=canonical_to_path(world.database.metadata.staging_path),
            ready=tmp_path / "ready", processing=tmp_path / "processing")
        context = SimpleNamespace(database=world.database, clock=world.clock)
        with pytest.raises(ValueError):
            await flow(context)
        context.database = other.database
        before = tuple(other.owned.connection.iterdump())
        with pytest.raises(StateDbFailure):
            await flow(context)
        assert len(calls) == 1 and tuple(other.owned.connection.iterdump()) == before
        context.database = world.database
        await flow(context)
        assert len(calls) == 2 and calls[0] == calls[1]
