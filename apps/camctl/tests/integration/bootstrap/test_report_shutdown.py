"""报告进程收场期间的致命依据进入最终会话结果，所有资源仍须关闭。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.supervisor import WorkerSupervisor
from camctl.reporting.supervisor import WorkerShutdown
from camctl.session.outcome import SessionOutcome

from .test_initialization import _config


def _assemble_with_supervisor(mocker, supervisor):
    """保留真实会话协作者，只替换需要注入迟到结果的监督方。"""
    original = lifecycle._report_assembly

    def assemble(deps, failure_log):
        flows, _lazy_supervisor = original(deps, failure_log)
        return flows, supervisor

    mocker.patch("camctl.bootstrap.lifecycle._report_assembly", side_effect=assemble)


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [None, "report_error", "state_db_error", "clock_error"])
async def test_shutdown_state_failure_is_preserved_in_session_outcome(tmp_path, mocker, earlier):
    cfg = _config(tmp_path)
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, cfg)
    supervisor = mocker.create_autospec(WorkerSupervisor, instance=True, spec_set=True)
    failure = ResultFailureMessage(
        job_id="shutdown-job", instance_id="a" * 32,
        error_kind=ErrorKind.STATE, error_code="history_read_failed",
        error_message="frozen history unavailable")
    supervisor.stop.return_value = SimpleNamespace(
        exitcode=3, forced=False, state_failure=failure, configuration_failure=None)
    _assemble_with_supervisor(mocker, supervisor)
    original = SessionOutcome(succeeded=True) if earlier is None else SessionOutcome(
        succeeded=False, reason=earlier, details={"message": "original failure"})
    mocker.patch("camctl.bootstrap.lifecycle.run_session", return_value=original)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == (earlier if earlier in ("state_db_error", "clock_error")
                              else "state_db_error")
    primary_or_secondary = [dict(outcome.details), *outcome.details.get("secondary_errors", ())]
    assert any(
        "frozen history unavailable" in str(item) for item in primary_or_secondary)
    if earlier is not None:
        assert any("original failure" in str(item) for item in primary_or_secondary)
    supervisor.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [None, "report_error", "state_db_error", "clock_invalid"])
async def test_shutdown_configuration_failure_is_preserved(tmp_path, mocker, earlier):
    cfg = _config(tmp_path)
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, cfg)
    supervisor = mocker.create_autospec(WorkerSupervisor, instance=True, spec_set=True)
    failure = ResultFailureMessage(
        job_id="shutdown-job", instance_id="a" * 32, error_kind=ErrorKind.REPORT,
        error_code="configuration_error", error_message="ready: 原路径 /old，配置新路径 /new")
    supervisor.stop.return_value = WorkerShutdown(
        exitcode=3, forced=False, configuration_failure=failure)
    _assemble_with_supervisor(mocker, supervisor)
    original = SessionOutcome(succeeded=True) if earlier is None else SessionOutcome(
        succeeded=False, reason=earlier, details={"message": "original failure"})
    mocker.patch("camctl.bootstrap.lifecycle.run_session", return_value=original)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == (earlier if earlier in ("state_db_error", "clock_invalid")
                              else "configuration_error")
    primary_or_secondary = [dict(outcome.details), *outcome.details.get("secondary_errors", ())]
    assert any("/old" in str(item) and "/new" in str(item) for item in primary_or_secondary)
    if earlier is not None:
        assert any("original failure" in str(item) for item in primary_or_secondary)
    supervisor.stop.assert_awaited_once()
