"""默认入口在筛选业务终态前，核实原录像共同收场申请。"""

from dataclasses import replace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure, _WaitingProbe, _attempts
from .test_read_default_consumers import _default_world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
async def test_default_entry_confirms_original_recording_transaction_after_terminal(
        pipeline, monkeypatch, entrance):
    from decimal import Decimal

    owned, roots, _source_id = pipeline
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, context, reader, driver, _ends, wall, factory_calls = world
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)
        original_attempts = _attempts(owned)
        original_save = CaptureRepository.finish_recording_results
        inputs, outcomes = [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, True)
                result = original_save(repository, request, key, replace(current, connection=fault))
                assert fault.commit_calls == 1
            else:
                result = original_save(repository, request, key, current)
            outcomes.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
        wall[0] = _NOW + 200_000_000
        with pytest.raises((ConsistencyError, AssertionError)) as first_error:
            await capture_handler("camera_record")(1, runtime)
        assert len(inputs) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
        request, key = inputs[0]
        assert request.capture.occurred_at == wall[0]
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        assert saved_transaction_events(reopened.connection, key) is not None
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert _attempts(reopened) == original_attempts
        before = tuple(reopened.connection.iterdump())
        before_calls = tuple(driver.calls)
        original_open, opened = context.open_connection, []

        def open_connection():
            current = original_open()
            assert current.metadata == reopened.metadata and current.connection is not reopened.connection
            opened.append(current)
            return current

        context.open_connection = open_connection
        wall[0] = _NOW + 300_000_000
        selected = (context.flows["scheduling"] if entrance == "normal" else
                    context.flows["residual"] if entrance == "residual" else
                    context.restricted_flows["winddown"])
        await selected(context)

        assert inputs == [(request, key), (request, key)]
        assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
        assert len(opened) == 1 and len(factory_calls) == 1
        assert not runtime.pending_recording_results
        assert tuple(reopened.connection.iterdump()) == before
        assert len(reader.requests) == 1 and tuple(driver.calls) == before_calls
        assert runtime.results.calls == [1] and tools.calls == ["probe", "probe"]
        assert isinstance(first_error.value, ConsistencyError)
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)
