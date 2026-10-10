"""会话在派发下一流程前交付后台已形成的错误。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.session.service import StateDbFailure, _drive_flows

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("failure", [StateDbFailure("original save failed"), ValueError("original call failed")])
async def test_completed_background_failure_stops_first_report_and_other_work(failure):
    calls = []
    async def report(context):
        calls.append("report")
    async def device(context):
        calls.append("device")
    owner = SimpleNamespace(check_completed=Mock(side_effect=failure))
    context = SimpleNamespace(local_work=owner, flows={"report": report, "device": device})

    fatal = await _drive_flows(context)

    assert str(failure) in fatal
    assert calls == []


async def test_completed_background_error_is_checked_again_between_flows():
    calls = []
    failure = StateDbFailure("save completed while reporting")
    owner = SimpleNamespace(check_completed=Mock(side_effect=[None, failure]))
    async def report(context):
        calls.append("report")
    async def device(context):
        calls.append("device")
    context = SimpleNamespace(local_work=owner, flows={"report": report, "device": device})

    fatal = await _drive_flows(context)

    assert str(failure) in fatal
    assert calls == ["report"]
