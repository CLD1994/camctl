"""基准保存组件测试通过真实业务数据库准备活动与原目录意图。"""

from decimal import Decimal

import pytest_asyncio

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.capture.baseline_models import BaselineReadIntent
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..acceptance.test_acceptance import RealFileReader, _create_valid_database, _plan_body
from ..support.database import business_database
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..scheduling.test_start_action import _NOW, _SCHEDULED


@pytest_asyncio.fixture
async def database(tmp_path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    async with business_database(target) as database:
        yield database



async def baseline_activity(database, tmp_path, *, request_id="42", device_id="cam-1"):
    import json

    body = _plan_body(request_id=request_id)
    body["actions"][0].update(type="camera_timelapse", device_id=device_id,
        params=_params("dji-action6", "camera_timelapse"))
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    source = parse_input(await read_input(str(target), RealFileReader()))
    result = await accept_input(source, AcceptanceContext(mode=CommandMode.RUN,
        catalog=_catalog("dji-action6", device_id=device_id), repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: _NOW)})()),
        new_operation_key(), database)
    assert result.plan_id is not None
    action_id = await database.read(lambda owned: owned.connection.execute(
        "SELECT MAX(id) FROM actions").fetchone()[0])
    key = new_operation_key()
    outcome = await database.write(lambda owned: SchedulingRepository().start_action(
        StartActionRequest(action_id=action_id, trusted_wall_now=_SCHEDULED,
            occurred_at=_SCHEDULED), key, owned), key=key)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.activity_id


async def baseline_ref(database, activity_id):
    return await database.read(lambda owned: CaptureRepository().baseline_ref(activity_id, owned))


async def baseline_read_intent(database, tmp_path):
    activity = await baseline_activity(database, tmp_path)
    action_id = await database.read(lambda owned: owned.connection.execute(
        "SELECT action_id FROM device_activities WHERE id=?", (activity,)).fetchone()[0])
    return BaselineReadIntent(action_id, activity, DeviceBinding("cam-1", "dji-action6"),
        ("/mnt/media_rw/emulated/DCIM", "/mnt/media_rw/sd/DCIM"), None,
        Decimal("1.0000000000000001"), await baseline_ref(database, activity), 1,
        new_operation_key(), new_operation_key(), _SCHEDULED)
