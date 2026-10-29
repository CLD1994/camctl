"""正式受理、调度与会话装配共同保存拍摄和重开后的核实结果。"""

from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..support.capture import capture_runtime, capture_test_session
from .result_consumer_fixtures import consumer_world
from .test_capture_contract import ResultsDouble, _PAGE_EVIDENCE, _entry

pytestmark = pytest.mark.asyncio


def _photo_factory(results, controls, *, now=None):
    async def factory(owned, *, driver):
        controls.append(driver)
        return await capture_runtime(
            owned,
            driver=driver,
            evidence=_PAGE_EVIDENCE,
            results=results,
            wall_us=lambda: 1_750_000_000_000_000,
            monotonic_ns=lambda: 5_000_000_000 if now is None else now[0],
        )
    return factory


async def test_accepted_photo_registers_file_through_session_assembly(tmp_path):
    results = ResultsDouble({1: (_entry("photo", kind=FileKind.PHOTO),)})
    controls = []
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "photo", start=False,
        runtime_factory=_photo_factory(results, controls))
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT source_action_id,completion_state,size_bytes FROM device_files"
        ).fetchone() == (action_id, 3, 4096)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert controls[0].control.await_count == 1
    finally:
        owned.connection.close()


async def test_reopened_photo_keeps_start_and_recovers_incomplete_results(tmp_path):
    controls = []
    async with capture_test_session():
        owned, runtime, action_id, handler = await consumer_world(tmp_path, "photo", start=False,
            runtime_factory=_photo_factory(ResultsDouble({}, set_finalized=False), controls))
        try:
            await capture_handler(handler)(action_id, runtime)
            before = owned.connection.execute(
                "SELECT t.id,t.run_id,t.attempt_no,t.result_json FROM operation_attempts t ORDER BY t.id"
            ).fetchall()
            assert len(before) == 2
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
            path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        finally:
            owned.connection.close()
    # 前一作用域的文件责任和数据库 worker 已实际结束，新作用域重开原文件。
    async with capture_test_session():
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        now = [5_000_000_000]
        try:
            recovered = await capture_runtime(
        owned,
        driver=controls[0],
        evidence=_PAGE_EVIDENCE,
        results=ResultsDouble({action_id: (_entry("photo", kind=FileKind.PHOTO),)}),
        wall_us=lambda: 1_750_000_000_000_000,
        monotonic_ns=lambda: now[0],
    )
            await capture_handler(handler)(action_id, recovered)
            assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (2,)
            now[0] += 3_000_000_000
            await capture_handler(handler)(action_id, recovered)
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
            assert owned.connection.execute(
                "SELECT t.id,t.run_id,t.attempt_no,t.result_json FROM operation_attempts t ORDER BY t.id LIMIT 2"
            ).fetchall() == before
            assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (3,)
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
            assert controls[0].control.await_count == 1
        finally:
            owned.connection.close()


async def test_injected_results_are_independent_of_driver_capability(tmp_path, monkeypatch):
    from unittest.mock import create_autospec
    from camctl.devices.ports import ControlDriver
    from camctl.devices.drivers.registry import CapabilityNotDeclaredError, port_for
    from ..support import capture as support

    assembled = []
    production_assembly = support.session_capture_assembly

    def inspect_assembly(**options):
        assembled.append(options)
        return production_assembly(**options)

    monkeypatch.setattr(support, "session_capture_assembly", inspect_assembly)
    results = ResultsDouble({})
    support.capture_assembly(driver=create_autospec(ControlDriver, instance=True),
        evidence=_PAGE_EVIDENCE, staging=tmp_path, results=results)
    options, = assembled
    entry = options["drivers"].entry("camctl-adb")
    assert entry.declaration.result_supported is False
    with pytest.raises(CapabilityNotDeclaredError):
        port_for(entry, "result")
    assert options["results"] is results
