"""候选发现沿会话缓存动态切换首次及后续准备时段。"""

from decimal import Decimal

import pytest

from camctl.bootstrap.capture_assembly import _PreparationLeads
from camctl.bootstrap.flows import CaptureFlow
from camctl.contracts.pages import Page
from test_capture_window_progress import _CaptureState


def test_preparation_leads_follow_setup_completion_and_keep_disabled_advance():
    pending = [Decimal("30")]
    leads = _PreparationLeads(
        {"camera": Decimal("10"), "disabled": Decimal("0"), "photo": Decimal("10")},
        {"camera": lambda: pending[0], "disabled": lambda: pending[0]})

    assert dict(leads) == {"camera": Decimal("40"), "disabled": Decimal("0"), "photo": Decimal("10")}
    pending[0] = Decimal("0")
    assert dict(leads) == {"camera": Decimal("10"), "disabled": Decimal("0"), "photo": Decimal("10")}


@pytest.mark.asyncio
async def test_capture_discovery_uses_updated_setup_lead_without_rebuilding_flow(mocker):
    state = _CaptureState(mocker, ())
    pending = [Decimal("30")]
    state.factory.preparation_leads = _PreparationLeads(
        {"cam-1": Decimal("10")}, {"cam-1": lambda: pending[0]})
    flow = CaptureFlow(state.factory)
    flow._discovery = state.discovery
    seen = []

    async def discover(database, now, leads):
        seen.append(dict(leads))
        return Page((), None)

    state.discovery.captures.side_effect = discover
    mocker.patch("camctl.outputs.read_discovery.next_preparation_at", autospec=True, return_value=None)

    await flow(state.context)
    pending[0] = Decimal("0")
    await flow(state.context)

    assert seen == [{"cam-1": Decimal("40")}, {"cam-1": Decimal("10")}]
