"""本次会话统一固定恢复水位，生产工厂共享实际启动边界。"""

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.recovery import RecoveryBoundary
from camctl.persistence.initialization import initialize_state

from .test_composition import _config_for


def test_embedded_runtime_does_not_assume_previous_local_work_stopped(tmp_path):
    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    deps = build_runtime(CommandMode.RUN, config)
    try:
        assert deps.recovery_boundary is RecoveryBoundary.UNCONFIRMED
        assert deps.recovery_max_event_id is None
    finally:
        close_runtime(deps)


@pytest.mark.asyncio
async def test_production_factories_share_initial_boundary_before_new_session_events(tmp_path, monkeypatch):
    from camctl.bootstrap import capture_assembly

    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    factories = []
    original = capture_assembly.session_capture_assembly

    def observed(**kwargs):
        factories.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", observed)
    deps = build_runtime(CommandMode.RUN, config, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    try:
        outcome = await execute_command(deps, None)

        assert outcome.succeeded
        assert deps.recovery_max_event_id == 0
        assert len(factories) == 3
        assert all(factory["recovery_boundary"] is RecoveryBoundary.HOST_LOCAL_SETTLED for factory in factories)
        assert all(factory["recovery_max_event_id"]() == 0 for factory in factories)
    finally:
        close_runtime(deps)


@pytest.mark.asyncio
async def test_production_capture_factories_share_actual_pending_result_responsibility(tmp_path, monkeypatch):
    """普通、残留与受限装配必须接手同一未保存实际结果。"""
    from unittest.mock import create_autospec
    from camctl.bootstrap import capture_assembly, lifecycle
    from camctl.session.outcome import SessionOutcome

    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    runtimes = []
    original = capture_assembly.session_capture_assembly

    def observed(**kwargs):
        factory = original(**kwargs)
        runtimes.append(factory(object(), "undeclared-device"))
        return factory

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", observed)
    # 本用例验证 execute_command 的实际装配；业务推进由独立的会话
    # 集成测试验证，不能让文件维护的故障遮蔽此处责任集合的断言。
    monkeypatch.setattr(lifecycle, "run_session", create_autospec(
        lifecycle.run_session, return_value=SessionOutcome(succeeded=True)))
    deps = build_runtime(CommandMode.RUN, config)
    try:
        await execute_command(deps, None)
        ordinary, residual, restricted = runtimes
        actual_pending = object()
        ordinary.pending_start_results[(1, 1)] = actual_pending
        assert residual.pending_start_results.get((1, 1)) is actual_pending
        assert restricted.pending_start_results.get((1, 1)) is actual_pending
    finally:
        close_runtime(deps)
