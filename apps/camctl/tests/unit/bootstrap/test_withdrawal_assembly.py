"""正常运行和受限取消共用同一次撤回会话。"""

from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import RuntimeDeps, execute_command
from camctl.outputs.withdrawal import WithdrawalSession
from camctl.session.outcome import SessionOutcome


@pytest.mark.asyncio
async def test_command_assembles_both_cancel_flows_with_same_withdrawal_session(mocker):
    received = []

    def make_cancel_flow(**kwargs):
        session = kwargs['withdrawal_session']
        assert isinstance(session, WithdrawalSession)
        received.append((kwargs.get('unscheduled_only', False), session))
        async def cancel(_context):
            pass
        return cancel

    async def run_session(context, source):
        assert context.flows['cancel'] is not context.restricted_flows['cancel']
        return SessionOutcome(succeeded=True)

    mocker.patch('camctl.bootstrap.flows.cancel_flow', side_effect=make_cancel_flow)
    mocker.patch('camctl.bootstrap.lifecycle.run_session', side_effect=run_session)
    deps = RuntimeDeps(CommandMode.RUN, load_config({}, ConfigDefaults()),
                       Path('/state.db'), Path('/session.lock'), Path('/admission.lock'), None)
    result = await execute_command(deps, None)
    assert result.succeeded
    assert [restricted for restricted, _ in received] == [False, True]
    assert received[0][1] is received[1][1]
