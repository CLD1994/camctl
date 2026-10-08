from types import SimpleNamespace

import pytest

from camctl.session.clock import ClockBecameUntrusted
from camctl.session.service import _drive_flows


@pytest.mark.asyncio
async def test_untrusted_runtime_clock_is_not_a_database_failure():
    async def motor(context):
        raise ClockBecameUntrusted("final clock check")

    context = SimpleNamespace(flows={"motor": motor}, failure_log=None)
    with pytest.raises(ClockBecameUntrusted):
        await _drive_flows(context)
