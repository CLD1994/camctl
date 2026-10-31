"""报告场景通过公开拍摄处理器建立录像和延时前置事实。"""

from unittest.mock import create_autospec

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.persistence.directory_switch import canonical_to_path
from camctl.persistence.models import DbOutcomeKind

from ..capture.public_assembly import assemble_capture
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned
from ..capture.test_capture_contract import ResultsDouble, _NOW
from ..capture.test_record_file_completion_wait import _result_port


async def reporting_capture_world(tmp_path, consumer, catalog):
    database = None
    clock = {"wall": _NOW, "monotonic": 5_000_000_000}

    async def assemble(owned, *, driver):
        nonlocal database
        runtime, database = await assemble_capture(
            owned, driver=driver, evidence=RESULT_EVIDENCE, results=ResultsDouble({}),
            staging=canonical_to_path(owned.metadata.staging_path),
            wall_us=lambda: clock["wall"], monotonic_ns=lambda: clock["monotonic"])
        return runtime

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, catalog=catalog, start=False, assemble=assemble)
    return owned, runtime, action_id, handler, clock


async def stopped_record_world(tmp_path, catalog, read):
    owned, runtime, action_id, handler, clock = await reporting_capture_world(
        tmp_path, "record", catalog)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        await capture_handler(handler)(action_id, runtime)
        stopper = create_autospec(StopDriver, instance=True)
        stopper.stop.return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", activity_id))
        runtime.stopper = stopper
        async def list_results(request, batch):
            return await read(request, batch, owned, action_id, clock)
        _result_port(runtime, list_results)
        clock.update(wall=_NOW + 12_000_000, monotonic=17_000_000_000)
        await capture_handler(handler)(action_id, runtime)
        stopper.stop.assert_awaited_once()
        clock.update(wall=_NOW + 17_000_000, monotonic=22_000_000_000)
        return owned, runtime, action_id, handler, clock
    except BaseException:
        await runtime.database.close()
        owned.connection.close()
        raise
