"""报告连接失效属于会话前提错误，不被普通报告失败吞掉。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.bootstrap.flows import report_flow
from camctl.persistence.runtime import DatabaseInvalidError
from camctl.reporting.supervisor import WorkerSupervisor
from camctl.session.service import StateDbFailure


@pytest.mark.asyncio
@pytest.mark.parametrize("start_actions", [False, True])
async def test_report_open_failure_is_state_database_failure(start_actions):
    opener = Mock(side_effect=DatabaseInvalidError("database_metadata 不可读"))
    context = SimpleNamespace(open_connection=opener)
    flow = report_flow(
        state_db=Path("/state.db"), staging=Path("/staging"), ready=Path("/ready"),
        processing=Path("/processing"), history=None, database=None,
        supervisor=create_autospec(WorkerSupervisor, instance=True, spec_set=True),
        start_actions=start_actions)

    with pytest.raises(StateDbFailure, match="database_metadata"):
        await flow(context)
