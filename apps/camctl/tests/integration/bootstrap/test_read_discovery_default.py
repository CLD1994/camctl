"""读取发现限额进入实际会话装配，后续运行重新加载。"""

from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config

from .test_output_binding_changes import (  # noqa: F401
    Catalog, _CONTENT, _accept, _changed, _registry, _save_photos, environment,
)
from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401


def test_read_discovery_default():
    assert load_config(None, ConfigDefaults()).session.read_discovery_batch_size == 100


@pytest.mark.parametrize("size", [1, 100000])
def test_read_discovery_override_and_reload(size):
    first = load_config({"session": {"read_discovery_batch_size": size}}, ConfigDefaults())
    second = load_config({"session": {"read_discovery_batch_size": size + 1}}, ConfigDefaults())
    assert first.session.read_discovery_batch_size == size
    assert second.session.read_discovery_batch_size == size + 1


@pytest.mark.parametrize("size", [True, False, 0, -1, "1", 1.0])
def test_invalid_read_discovery_size(size):
    with pytest.raises(ConfigError):
        load_config({"session": {"read_discovery_batch_size": size}}, ConfigDefaults())


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size,total", [(1, 3), (2, 5)])
async def test_default_obtain_reaches_candidates_beyond_first_batch(
        environment, monkeypatch, batch_size, total):
    import asyncio
    from dataclasses import replace

    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from camctl.outputs import read_discovery
    import weakref

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{
        "name": f"取回{index}", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}},
    } for index in range(total)])
    cfg = replace(cfg, session=replace(cfg.session, read_discovery_batch_size=batch_size))
    real_candidates = read_discovery.read_candidates
    previous, sizes = [], []

    def bounded_candidates(*args, **kwargs):
        assert all(reference() is None for reference in previous)
        page = real_candidates(*args, **kwargs)
        assert kwargs["limit"] == batch_size and len(page.items) <= batch_size
        previous[:] = [weakref.ref(candidate) for candidate in page.items]
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
    assert all(reference() is None for reference in previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_default_binding_failures_are_paged(environment, monkeypatch, state):
    import asyncio
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{
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
    _accept(owned, "2", actions)
    entered = False
    real_open = driver.open_read

    async def submit_then_read(*args, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            assert not owned.connection.in_transaction
            _accept(owned, "3", [{**actions[0], "name": "新提交",
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
    from .test_cancel_obtain_settlement_transactions import _apply

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": f"取回{index}", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}} for index in range(3)])
    canceled = []
    original_open = driver.open_read

    async def cancel_then_read(source, offset, ticket, **kwargs):
        if not canceled:
            assert not owned.connection.in_transaction
            target = owned.connection.execute("SELECT action_id FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()[0]
            _apply(owned, target)
            canceled.append(target)
        return await original_open(source, offset, ticket, **kwargs)

    driver.open_read = cancel_then_read
    cfg = replace(cfg, session=replace(cfg.session, read_discovery_batch_size=1))
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: _registry(driver))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        result = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.005), 30)
        assert result.succeeded, result.details
    finally:
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
    _accept(owned, "1", [{"name": name, "type": "camera_take_photo", "device_id": "cam-a",
        "scheduled_at": scheduled, "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 10000}} for name, scheduled in (
            ("晚计划时间先登记", "2026-01-15 09:00:00"),
            ("早计划时间后登记", "2026-01-15 08:59:59"),
            ("窗口已过", "2026-01-15 08:59:40"),
            ("已经取消", "2026-01-15 08:59:41"))])
    owned.connection.execute("UPDATE actions SET status=6,cancel_requested=1 WHERE id=4")
    owned.connection.commit()
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
    assert owned.connection.execute("SELECT status FROM actions ORDER BY id").fetchall() == [(3,), (3,), (5,), (6,)]


@pytest.mark.asyncio
async def test_default_internal_read_uses_session_batch_limit(pipeline, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal
    from camctl.bootstrap.lifecycle import close_runtime
    from camctl.capture.media_flow import run_recording_media
    from camctl.host_files.media import MediaProbe
    from . import test_read_default_consumers as consumers
    from ..capture.test_media_flow import ProbeTools

    original_config = consumers._config
    def config(root):
        cfg = original_config(root)
        return replace(cfg, session=replace(cfg.session, read_discovery_batch_size=1))

    tools = ProbeTools(MediaProbe(duration_s=Decimal("61"), error=None))
    monkeypatch.setattr(consumers, "_config", config)
    monkeypatch.setattr(consumers, "_NoNewMedia", lambda: tools)
    deps, factories, context, reader, *_ = await consumers._default_world(pipeline, monkeypatch)
    try:
        owned = pipeline[0]
        runtime = factories[0](owned, "cam-1")
        await run_recording_media(runtime.media, 1, 1, pipeline[2])
        assert len(reader.requests) == 1
        assert tools.calls == ["probe"]
        assert owned.connection.execute("SELECT verification_state,slot_device_id FROM file_copies").fetchone() == (3, None)
        assert owned.connection.execute("SELECT check_state FROM recording_processing").fetchone() == (3,)
        # 正常与残留工厂重建后仍采用本次加载的限额。
        for factory in factories[:2]:
            assert factory(owned, "cam-1").media.copies().repository.read_batch_size == 1
    finally:
        close_runtime(deps)
