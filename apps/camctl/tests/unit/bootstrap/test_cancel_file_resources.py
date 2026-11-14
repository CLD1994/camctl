"""取消流程装配时校验文件操作的共享资源。"""

from pathlib import Path

import pytest

from camctl.bootstrap.flows import cancel_flow
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.values import ConsistencyError
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.withdrawal import WithdrawalSession
from camctl.outputs.work_files import WorkFileLimits
from camctl.session.supervision import Supervisor


def test_cancel_flow_requires_and_checks_shared_file_resources():
    staging, ready, processing = map(Path, ("/staging", "/ready", "/processing"))
    executor = FileTaskExecutor(Supervisor())
    other = FileTaskExecutor(Supervisor())
    cleanup = WorkFileRuntime(staging=staging, limits=WorkFileLimits(1, 2), executor=executor)
    withdrawal = WithdrawalSession(ready=ready, processing=processing, executor=executor)
    paths = dict(staging=staging, ready=ready, processing=processing)

    with pytest.raises(TypeError):
        cancel_flow(**paths, work_files=cleanup)
    with pytest.raises(TypeError):
        cancel_flow(**paths, withdrawal_session=withdrawal)
    with pytest.raises(ConsistencyError):
        cancel_flow(**paths, file_executor=other, work_files=cleanup)
    with pytest.raises(ConsistencyError):
        cancel_flow(**paths, file_executor=other, withdrawal_session=withdrawal)
    with pytest.raises(ConsistencyError):
        cancel_flow(staging=Path("/other"), ready=ready, processing=processing,
                    file_executor=executor, work_files=cleanup)
    with pytest.raises(ConsistencyError):
        cancel_flow(staging=staging, ready=Path("/other"), processing=processing,
                    file_executor=executor, withdrawal_session=withdrawal)
    assert callable(cancel_flow(**paths, file_executor=executor, work_files=cleanup,
                                withdrawal_session=withdrawal))
