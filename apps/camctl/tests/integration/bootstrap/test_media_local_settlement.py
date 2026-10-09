"""已取得媒体申请在实际拥有者结束之后仍属于本会话收场责任。"""

import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from camctl.bootstrap import lifecycle
from camctl.capture.media_flow import DriverReadSessions, run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.locks import acquire_session, probe_admission
from camctl.session.service import StateDbFailure, run_session

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_media_flow import ReadDriverDouble
from ..capture.test_media_result_retry import _ActualTools
from ..session.test_session import FixedClock
from .test_binding_transactions import _CommitFailure
from .test_media_saved_result_consumers import _default_factories


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def held_media(pipeline, monkeypatch):
    owned, _roots, source_id = pipeline
    deps, factories, context = await _default_factories(pipeline, monkeypatch)
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools("check")
    flow = factories[0](owned, "cam-1").media
    assert flow is not None
    flow.sessions = DriverReadSessions(owned, driver, ticket=None)
    flow.tools = tools
    original_save = CaptureRepository.save_check_result
    attempts = []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        attempts.append((command, key))
        return original_save(repository, command, key,
            replace(current, connection=_CommitFailure(current.connection, True)))

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, 1, 1, source_id)
        assert len(attempts) == 1 and tools.probes == 1
        assert not deps.work_files.executor.unfinished_files()
        assert len(deps.capture_media_results) == 1
        assert deps.work_files.pending_media_results is deps.capture_media_results
        assert deps.work_files.supervisor.outstanding() == ()
        assert deps.work_files.file_supervisor.outstanding() == ()
        context.clock = FixedClock(_NOW + 100)
        yield deps, context, driver, tools, attempts
    finally:
        lifecycle.close_runtime(deps)


async def test_media_pending_save_is_required_after_actual_owner_has_ended(held_media):
    deps, _context, _driver, _tools, _attempts = held_media
    assert deps.work_files.required_settlements() == 1


async def test_media_pending_save_cannot_be_declared_settled_or_blindly_retried(held_media):
    deps, _context, driver, tools, attempts = held_media
    with pytest.raises(StateDbFailure) as failed:
        await deps.work_files.settle()
    assert "injected commit response failure" in str(failed.value)
    assert failed.value.__cause__ is not None
    assert len(attempts) == 1 and len(deps.capture_media_results) == 1
    assert tools.probes == 1 and len(driver.opens) == 1


@pytest.mark.parametrize("primary", ["state", "configuration", "clock", "cancel"])
async def test_media_unconfirmed_save_preserves_fatal_primary_and_secondary_state(held_media, primary):
    deps, context, driver, tools, attempts = held_media
    errors = {"state": StateDbFailure("primary-state"),
        "configuration": DeviceConfigurationError("primary-configuration"),
        "clock": ClockBecameUntrusted("primary-clock"),
        "cancel": asyncio.CancelledError("primary-cancel")}
    original = errors[primary]
    restricted = []

    async def fatal(_context):
        raise original

    async def later(_context):
        restricted.append(1)

    context.flows = {"fatal": fatal}
    context.restricted_flows = {"winddown": later}
    context.once_report = later
    if primary == "cancel":
        with pytest.raises(asyncio.CancelledError) as canceled:
            await run_session(context, None)
        assert canceled.value is original
        assert "injected commit response failure" in str(getattr(canceled.value, "__notes__", ()))
    else:
        outcome = await run_session(context, None)
        assert outcome.reason == {"state": "state_db_error", "configuration": "configuration_error",
            "clock": "clock_invalid"}[primary]
        assert f"primary-{primary}" in str(outcome.details)
        secondary = outcome.details.get("secondary_errors", ())
        assert any(item["reason"] == "state_db_error" and
            "injected commit response failure" in str(item["details"]) for item in secondary)
    assert not restricted
    assert len(attempts) == 1 and len(deps.capture_media_results) == 1
    assert tools.probes == 1 and len(driver.opens) == 1
    assert probe_admission(context.paths.admission_lock).is_free
    lease = acquire_session(context.paths.session_lock)
    lease.close()
