"""Hasselblad 的原存储选择和本地归档由正式会话工厂消费。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import capture_handler
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.host_files.models import BoundDirectories
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.host_capture import HostCaptureRepository

from ..capture.test_host_photo_flow import _ManagedCaptureTransport, _world, _NOW
from ..support.bootstrap_driver_cases import interrupt_start_grant, reopen_driver_database
from ..support.capture import capture_database, capture_runtime

pytestmark = pytest.mark.asyncio


def _driver(config, transport):
    return HasselbladDriver(config.devices, BoundDirectories(Path(config.paths.staging)),
        monotonic_ns=lambda: 1, transport=transport)


def _entry(driver):
    return DriverEntry(DRIVER_ID, driver, driver.declaration, EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)


@pytest.mark.parametrize('first,current', [('host', 'device'), ('device', 'host')])
async def test_hasselblad_assembled_start_grant_keeps_original_storage_and_deadline(tmp_path, monkeypatch, first, current):
    transport = _ManagedCaptureTransport()
    config, owned, _original, _runtime, _clock = await _world(tmp_path, storage=first,
        driver_factory=lambda cfg: _driver(cfg, transport))
    changed = replace(config, devices={'cam-1': {**config.devices['cam-1'],
        'hasselblad': {**config.devices['cam-1']['hasselblad'], 'output_storage': current}}})
    driver = _driver(changed, transport)
    try:
        runtime = await capture_runtime(owned, driver=driver, evidence=EVIDENCE,
            devices=changed.devices, drivers=DriverRegistry((_entry(driver),)),
            staging=Path(changed.paths.staging), wall_us=lambda: _NOW,
            monotonic_ns=lambda: 1, wait_config=lambda _: None)
        action = await runtime.action(1)
        assert action['execution_spec_json'] == {'output_storage': first, 'start_call_timeout_s': 180}
        ticket = await interrupt_start_grant(runtime, 1, 'camera_take_photo', monkeypatch)
        timeout, = owned.connection.execute(
            'SELECT timeout_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert Decimal(str(timeout)) == Decimal('180')
        assert not any(call.argv[3] == 'capture' for call in transport.calls)
    finally:
        owned.connection.close()


async def test_hasselblad_assembly_keeps_local_host_recovery_without_result_port(tmp_path, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    transport = _ManagedCaptureTransport()
    config, owned, driver, runtime, _clock = await _world(tmp_path,
        driver_factory=lambda cfg: _driver(cfg, transport))
    def interrupted(repository, request, key, current):
        return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError('归档后的正式产物保存中断'))
    try:
        with monkeypatch.context() as fault:
            fault.setattr(HostCaptureRepository, 'save_host_photo_complete', interrupted)
            with pytest.raises(ConsistencyError):
                await capture_handler('camera_take_photo')(1, runtime)
        calls = tuple(transport.calls)
        assert sum(call.argv[3] == 'capture' for call in calls) == 1
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (2,)
        owned = await reopen_driver_database(owned)
        database = await capture_database(owned)
        resumed = await session_capture_assembly(devices={}, drivers=DriverRegistry((_entry(driver),)),
            staging=Path(config.paths.staging), wait_config=lambda _: None,
            wall_us=lambda: _NOW, monotonic_ns=lambda: 1)(database, 'cam-1')
        assert resumed.results is None and resumed.driver is None
        assert resumed.roots == BoundDirectories(Path(config.paths.staging))
        assert isinstance(resumed.host_capture, HostCaptureRepository)
        await capture_handler('camera_take_photo')(1, resumed)
        assert tuple(transport.calls) == calls
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (3,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
        archived = owned.connection.execute('SELECT relative_path FROM intermediate_files WHERE owner_action_id=1').fetchall()
        assert len(archived) == 2
        assert all((Path(config.paths.staging) / path).is_file() for path, in archived)
    finally:
        owned.connection.close()


async def test_runtime_deps_shares_one_start_entry_coordinator_with_every_capture_and_cancel_factory(tmp_path, monkeypatch):
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap import capture_assembly, flows
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from ..capture.test_host_photo_flow import _config
    config = _config(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    capture_entries, cancel_entries, capture_factories = [], [], []
    capture = capture_assembly.session_capture_assembly
    cancel = flows.cancel_flow
    def observed_capture(**options):
        capture_entries.append(options['start_entries'])
        factory = capture(**options)
        capture_factories.append(factory)
        return factory
    def observed_cancel(**options):
        cancel_entries.append(options['start_entries'])
        return cancel(**options)
    monkeypatch.setattr(capture_assembly, 'session_capture_assembly', observed_capture)
    monkeypatch.setattr(flows, 'cancel_flow', observed_cancel)
    from camctl.devices.drivers import runtime as driver_runtime
    registered = DriverRegistry((_entry(_driver(config, _ManagedCaptureTransport())),))
    monkeypatch.setattr(driver_runtime, 'current_registry', lambda: registered)
    catalog = build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()}))
    deps = build_runtime(CommandMode.RUN, config, catalog=catalog)
    try:
        assert deps.startup_error is None
        result = await execute_command(deps, None)
        assert result.succeeded, result
        assert len(capture_entries) == 3 and all(value is deps.start_entries for value in capture_entries)
        assert len(cancel_entries) == 2 and all(value is deps.start_entries for value in cancel_entries)
        database = await capture_database(Path(config.paths.state_db))
        ordinary, residual, restricted = capture_factories
        for factory, device_id in ((ordinary, 'cam-1'), (residual, 'cam-1'),
                (restricted, 'cam-1'), (ordinary, 'missing')):
            runtime = await factory(database, device_id)
            assert runtime.start_entries is deps.start_entries
    finally:
        close_runtime(deps)
