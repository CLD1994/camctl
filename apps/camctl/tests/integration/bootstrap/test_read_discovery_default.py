"""读取发现限额进入实际会话装配，后续运行重新加载。"""

from pathlib import Path

from camctl.bootstrap.config import ConfigDefaults, load_config

import pytest

from .test_output_binding_changes import (  # noqa: F401
    Catalog, _CONTENT, _NOW, _accept, _changed, _registry, _save_photos, environment,
)
from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401


def test_read_discovery_default():
    assert load_config(None, ConfigDefaults()).session.read_discovery_batch_size == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size,total", [(1, 3), (2, 5)])
async def test_default_obtain_reaches_candidates_beyond_first_batch(
        environment, monkeypatch, batch_size, total):
    import asyncio
    from dataclasses import replace

    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from camctl.outputs import read_discovery

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    await _accept(owned, "2", [{
        "name": f"取回{index}", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}},
    } for index in range(total)])
    cfg = replace(cfg, session=replace(cfg.session, read_discovery_batch_size=batch_size))
    real_candidates = read_discovery.read_candidates
    sizes = []

    def bounded_candidates(*args, **kwargs):
        page = real_candidates(*args, **kwargs)
        assert kwargs["limit"] == batch_size and len(page.items) <= batch_size
        sizes.append(len(page.items))
        return page

    monkeypatch.setattr(read_discovery, "read_candidates", bounded_candidates)
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: _registry(driver))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        outcome = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.005), 30)
        assert outcome.succeeded, outcome.details
    finally:
        close_runtime(deps)
    assert len(driver.reads) == total * 2
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchall() == [(3,)] * total
    assert owned.connection.execute("SELECT COUNT(DISTINCT copy_id) FROM operation_runs WHERE kind=3").fetchone() == (total * 2,)
    files = tuple(Path(cfg.paths.ready).glob("*.jpg"))
    assert len(files) == total * 2
    assert all(path.read_bytes() == _CONTENT for path in files)
    assert max(sizes) <= batch_size and len(sizes) > 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_default_binding_failures_are_paged(environment, monkeypatch, state):
    import asyncio
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    await _accept(owned, "2", [{
        "name": f"取回{index}", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}},
    } for index in range(3)])
    cfg = _changed(replace(cfg, session=replace(cfg.session, read_discovery_batch_size=1)), state)
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: _registry(driver))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        outcome = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.005), 30)
        assert outcome.succeeded, outcome.details
    finally:
        close_runtime(deps)
    assert driver.reads == [("cam-b", "camctl-adb")] * 3
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchall() == [(4,)] * 3
    assert owned.connection.execute("SELECT COUNT(*) FROM obtain_items WHERE status=4").fetchone() == (3,)


@pytest.mark.asyncio
async def test_submit_during_initial_discovery_is_not_lost(environment, monkeypatch):
    import asyncio
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    actions = [{"name": f"取回{index}", "type": "obtain_action_outputs",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"source": {"plan_instance_id": "1", "group": "files"}}}
               for index in range(3)]
    await _accept(owned, "2", actions)
    entered = False
    real_open = driver.open_read

    async def submit_then_read(*args, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            assert not owned.connection.in_transaction
            await _accept(owned, "3", [{**actions[0], "name": "新提交",
                                  "scheduled_at": "2026-01-15 08:59:00"}])
        return await real_open(*args, **kwargs)

    driver.open_read = submit_then_read
    cfg = replace(cfg, session=replace(cfg.session, read_discovery_batch_size=1))
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: _registry(driver))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        outcome = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.005), 30)
        assert outcome.succeeded, outcome.details
    finally:
        close_runtime(deps)
    assert entered
    assert len(driver.reads) == 8
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchall() == [(3,)] * 4


@pytest.mark.asyncio
async def test_cancel_during_discovery_preserves_active_read_settlement(environment, monkeypatch):
    import asyncio
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    await _accept(owned, "2", [{"name": f"取回{index}", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}} for index in range(3)])
    import json
    import threading
    from camctl.devices.read_session import ReadSession
    from .test_obtain_flow import _MemoryStream

    canceled, submissions = [], []
    original_open = driver.open_read
    loop, released = asyncio.get_running_loop(), threading.Event()
    gate_started = False

    async def submit_cancel(ticket):
        target, = owned.connection.execute(
            "SELECT action_id FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()
        try:
            await _accept(owned, "3", [{"name": "取消取回", "type": "cancel_task",
                "scheduled_at": "2026-01-15 09:00:00", "params": {
                    "target": {"action_instance_id": str(target)}}}])
            async with asyncio.timeout(30):
                while owned.connection.execute(
                        "SELECT cancel_requested FROM actions WHERE id=?", (target,)).fetchone() != (1,):
                    await asyncio.sleep(.005)
            canceled.append(target)
        finally:
            released.set()

    class ActiveStream(_MemoryStream):
        def __init__(self, content, ticket):
            super().__init__(content)
            self.ticket, self.entered = ticket, False

        def read(self, limit):
            if not self.entered:
                self.entered = True
                loop.call_soon_threadsafe(lambda: submissions.append(
                    asyncio.create_task(submit_cancel(self.ticket))))
                if not released.wait(timeout=30):
                    raise TimeoutError("真实源读取尚未取得取消回执")
            return super().read(limit)

        def cancel(self):
            released.set()

    async def cancel_then_read(source, offset, ticket, *, idle_timeout_s):
        nonlocal gate_started
        if gate_started:
            return await original_open(source, offset, ticket, idle_timeout_s=idle_timeout_s)
        gate_started = True
        driver.reads.append(tuple(json.loads(source.file_id)[:2]))
        return ReadSession(source, offset, ActiveStream(_CONTENT[offset:], ticket), idle_timeout_s)

    driver.open_read = cancel_then_read
    cfg = replace(cfg, session=replace(cfg.session, read_discovery_batch_size=1))
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: _registry(driver))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        result = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.005), 30)
        assert result.succeeded, result.details
    finally:
        released.set()
        if submissions:
            await asyncio.gather(*submissions)
        close_runtime(deps)
    assert len(canceled) == 1
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4 ORDER BY id").fetchall() == [(6,), (3,), (3,)]
    assert owned.connection.execute("SELECT COUNT(*) FROM file_copies WHERE slot_device_id IS NOT NULL").fetchone() == (0,)
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [1, 2])
async def test_capture_batches_keep_full_business_order(environment, batch_size):
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.bootstrap.flows import capture_flow
    from camctl.capture.handlers import ObservedFile
    from camctl.capture.results import FileKind
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.evidence import DeviceObservation
    from camctl.devices.ports import DeviceCallResult
    from .test_output_binding_changes import _NOW
    from ..capture.test_capture_contract import ResultsDouble

    cfg, owned, context, driver = environment
    await _accept(owned, "1", [{"name": name, "type": "camera_take_photo", "device_id": "cam-a",
        "scheduled_at": scheduled, "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 10000}} for name, scheduled in (
            ("晚计划时间先登记", "2026-01-15 09:00:00"),
            ("早计划时间后登记", "2026-01-15 08:59:59"),
            ("窗口已过", "2026-01-15 08:59:40"),
            ("已经取消", "2026-01-15 08:59:41"))])
    from .test_read_execution_runtime import _apply_read_business_cancel
    await _apply_read_business_cancel(owned, 4, database=context.database)
    calls = []

    async def control(request):
        identity = int(request.ticket.target_id)
        action_id = owned.connection.execute("SELECT action_id FROM device_activities WHERE id=?", (identity,)).fetchone()[0]
        calls.append(action_id)
        return DeviceCallResult((DeviceObservation(type="photo_taken", version=1,
            data={"activity_id": str(identity)}),), None)

    driver.control = control
    results = ResultsDouble({identity: (ObservedFile(identity=f"shot-{identity}",
        locator={"path": f"/DCIM/{identity}"}, evidence={"listing": str(identity)}, complete=True,
        size_bytes=len(_CONTENT), kind=FileKind.PHOTO, original_name=f"{identity}.jpg", media_type="image/jpeg"),)
        for identity in (1, 2)})
    factory = session_capture_assembly(devices=cfg.devices, drivers=_registry(driver), results=results,
        staging=Path(cfg.paths.staging), wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda action: CaptureWaitConfig(target_duration_ms=1000, driver_margin_ms=0),
        read_discovery_batch_size=batch_size)
    flow = capture_flow(factory, read_discovery_batch_size=batch_size)
    for _ in range(6):
        await flow(context)
        await flow.settle()
    assert calls == [2, 1]
    assert owned.connection.execute("SELECT status FROM actions WHERE type=1 ORDER BY id").fetchall() == [(3,), (3,), (5,), (6,)]


@pytest.mark.asyncio
async def test_default_internal_read_uses_session_batch_limit(pipeline, capture_resources, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap import capture_assembly, lifecycle
    from ..capture.media_retry_fixtures import _Catalog
    from ..capture.test_capture_contract import ResultsDouble, _entry
    from ..capture.test_input_copy import _CONTENT as content
    from ..support.bootstrap_consumers import consumer_world
    from ..support.session import run_default_session

    results = ResultsDouble({1: (replace(_entry("original-recording", size=len(content)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
    world = await consumer_world(pipeline, capture_resources, duration="61", results=results)
    config = replace(world.config, session=replace(world.config.session, read_discovery_batch_size=1))
    actual_factory, limits = capture_assembly.session_capture_assembly, []

    def factory(**options):
        if options.get("media_enabled", True):
            limits.append(options["read_discovery_batch_size"])
        return actual_factory(**options, wall_us=lambda: world.wall[0],
            monotonic_ns=lambda: 7_000_000_000, probe_request=world.probe_request,
            repair_request=world.repair_request)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: SimpleNamespace(utc_micros=lambda: world.wall[0]))
    deps = lifecycle.build_runtime(CommandMode.RUN, config, catalog=_Catalog())
    try:
        assert deps.startup_error is None
        outcome = await run_default_session(deps, None, world.entry)
        assert outcome.succeeded, outcome.details
        assert limits and all(limit == 1 for limit in limits)
        assert len(world.reader.requests) == 1 and world.tools() == ["probe"]
        assert world.owned.connection.execute(
            "SELECT verification_state,slot_device_id FROM file_copies").fetchone() == (3, None)
        assert world.owned.connection.execute("SELECT check_state FROM recording_processing").fetchone() == (3,)
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
    finally:
        lifecycle.close_runtime(deps)
        await world.close()
