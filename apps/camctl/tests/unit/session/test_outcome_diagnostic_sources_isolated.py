"""会话保留原异常关联；所有协作方和外部资源均在加载前隔离。"""
import ast
import builtins
import importlib.resources
import io
import json
from contextlib import asynccontextmanager
import importlib.abc
import importlib.util
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[3] / "src" / "camctl"


class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"不允许加载真实 camctl 协作者: {fullname}")
        return None


def _forbidden(*args, **kwargs):
    raise AssertionError("单元测试不能使用真实资源")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(error):
    result = {"type": type(error).__name__, "message": str(error)}
    if error.__cause__ is not None:
        result["cause"] = _snapshot(error.__cause__)
    return result


def _project(value, exception_projector=_snapshot):
    if isinstance(value, BaseException):
        return exception_projector(value)
    if isinstance(value, dict):
        return {key: _project(item, exception_projector) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_project(item, exception_projector) for item in value]
    return value


@asynccontextmanager
async def _scope():
    yield


@pytest.fixture
def modules():
    source = ROOT / "session" / "service.py"
    lifecycle_source = ROOT / "bootstrap" / "lifecycle.py"
    tree = ast.parse(source.read_text() + "\n" + lifecycle_source.read_text().replace("from __future__ import annotations", ""))
    dependencies = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("camctl."):
            module = dependencies.setdefault(node.module, ModuleType(node.module))
            for alias in node.names:
                if alias.name != "*":
                    setattr(module, alias.name, type(alias.name, (Exception,), {}))
    dependencies["camctl.logging_runtime.formatting"].encode_record = lambda level, value: json.dumps(value)
    dependencies["camctl.logging_runtime.models"].LogLevel = SimpleNamespace(ERROR="error")
    dependencies["camctl.capture.recovery"].RecoveryBoundary = SimpleNamespace(UNCONFIRMED=object())
    dependencies["camctl.persistence.models"].DbOutcomeKind = SimpleNamespace(
        NOT_EXECUTED=object(), ROLLED_BACK=object(), UNKNOWN=object(), COMPLETED=object())
    dependencies["camctl.operations.error_diagnostics"].exception_diagnostic = _snapshot
    dependencies["camctl.persistence.transaction"].write_receipt_for = lambda error: None
    dependencies["camctl.session.locks"].lock_error_facts = lambda error: None
    dependencies["camctl.logging_runtime.diagnostics"] = ModuleType("camctl.logging_runtime.diagnostics")
    dependencies["camctl.logging_runtime.diagnostics"].diagnostic_scope = _scope
    dependencies["camctl.logging_runtime.diagnostics"].diagnostic_reference_for = lambda value: None
    with patch.dict(sys.modules, dependencies), patch.object(sys, "meta_path", [BlockCamctl(), *sys.meta_path]), \
            patch.object(socket, "socket", _forbidden), patch.object(sqlite3, "connect", _forbidden), \
            patch.object(subprocess, "Popen", _forbidden), patch.object(threading.Thread, "start", _forbidden), \
            patch.object(importlib.resources, "files", _forbidden), patch.object(builtins, "open", _forbidden), \
            patch.object(io, "open", _forbidden):
        outcome = _load("camctl.session.outcome", ROOT / "session" / "outcome.py")
        service = _load("camctl.session._isolated_diagnostic_service", source)
        sys.modules["camctl.session.service"] = service
        lifecycle = _load("camctl.bootstrap._isolated_diagnostic_lifecycle", lifecycle_source)
        yield outcome, service, lifecycle


def _run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("测试替身不能挂起")


@pytest.mark.parametrize("entry", ["flow", "background", "restricted"])
def test_exception_survives_session_conversion_without_changing_machine_details(modules, entry):
    _, service, _ = modules
    original = RuntimeError("read failed")
    async def fail(_):
        raise original
    def fail_completed():
        raise original
    context = SimpleNamespace(flows={"capture": fail}, restricted_flows={"capture": fail},
                              local_work=SimpleNamespace(check_completed=fail_completed) if entry == "background" else None)
    outcome = _run(service._run_restricted_flows(context) if entry == "restricted" else service._drive_flows(context))
    assert outcome.reason == "state_db_error"
    assert outcome.details["exception"] == {"type": "RuntimeError", "message": "read failed"}
    assert any(source is original for _, source in outcome._diagnostic_sources)


def test_merged_failure_preserves_both_actual_sources(modules):
    _, service, _ = modules
    first, second = RuntimeError("capture"), OSError("close")
    current = service._transaction_error_outcome(first, None, stage="capture")
    added = service._transaction_error_outcome(second, None, stage="shutdown")
    merged = service._merge_failure_outcome(current, added)
    assert merged.reason == current.reason
    assert merged.details["secondary_errors"][0]["details"]["message"] == "OSError: close"
    assert {id(source) for _, source in merged._diagnostic_sources} == {id(first), id(second)}


def test_final_summary_reuses_frozen_original_and_retains_unrecorded_wrapper(modules):
    _, service, lifecycle = modules
    original = RuntimeError("raw original")
    wrapper = RuntimeError("new wrapper")
    wrapper.__cause__ = original
    outcome = service._transaction_error_outcome(wrapper, None, stage="execution")
    unchanged = dict(outcome.details)
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_snapshot = lambda value: _project(value, lambda error:
        {"type": "RuntimeError", "message": str(error), "cause": {"diagnostic_ref": "frozen-1"}}
        if error is wrapper else {"diagnostic_ref": "frozen-1"})
    summary = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert summary["reason"] == "state_db_error"
    assert summary["details"]["exception"]["message"] == "new wrapper"
    assert summary["details"]["exception"]["cause"] == {"diagnostic_ref": "frozen-1"}
    assert outcome.details == unchanged


def test_worker_failure_merge_retains_original_association(modules):
    _, service, lifecycle = modules
    original = RuntimeError("capture")
    outcome = service._transaction_error_outcome(original, None, stage="capture")
    failure = SimpleNamespace(error_code="db_error", error_message="worker failed",
                              error_kind=SimpleNamespace(value="db"))
    merged = lifecycle._shutdown_state_outcome(outcome, failure,
        worker_details=lifecycle._worker_failure_details(failure))
    assert merged.details["secondary_errors"][0]["details"]["worker_failure"]["error_message"] == "worker failed"
    assert any(source is original for _, source in merged._diagnostic_sources)


@pytest.mark.parametrize("frozen", [False, True])
def test_summary_keeps_full_new_exception_or_references_frozen_record(modules, frozen):
    _, service, lifecycle = modules
    original = RuntimeError("original detail")
    outcome = service._transaction_error_outcome(original, None, stage="execution")
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_snapshot = lambda value: _project(value, lambda error:
        {"diagnostic_ref": "prepared"} if frozen else _snapshot(error))
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert document["details"]["exception"] == ({"diagnostic_ref": "prepared"}
        if frozen else {"type": "RuntimeError", "message": "original detail"})
    assert outcome.details["exception"]["message"] == "original detail"


def test_startup_exception_kept_separately_from_machine_details(modules):
    outcome_type, _, lifecycle = modules
    original = OSError("opening failed")
    outcome = outcome_type.SessionOutcome(succeeded=False, reason="state_db_error",
        details={"error": "opening failed"}, _diagnostic_sources=((None, original),))
    sys.modules["camctl.logging_runtime.diagnostics"].diagnostic_snapshot = _project
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert document["details"] == {"error": "opening failed"}
    assert document["exceptions"] == [{"type": "OSError", "message": "opening failed"}]


def test_emergency_failure_keeps_new_error_and_existing_association(modules):
    _, service, lifecycle = modules
    original, secondary = RuntimeError("capture failed"), OSError("emergency failed")
    outcome = service._transaction_error_outcome(original, None, stage="execution")
    async def stop():
        raise secondary
    result = _run(service._merge_emergency_stops(SimpleNamespace(emergency_settlement=stop), outcome))
    assert result.details["emergency_stops"][0]["error"] == "应急收场失败: emergency failed"
    assert {id(source) for _, source in result._diagnostic_sources} == {id(original), id(secondary)}
    sys.modules["camctl.logging_runtime.diagnostics"].diagnostic_snapshot = _project
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), result))
    assert document["exceptions"][0]["message"] == "emergency failed"


def test_startup_without_log_channel_still_delivers_to_stderr(modules):
    outcome_type, _, lifecycle = modules
    original = OSError("startup failed")
    outcome = outcome_type.SessionOutcome(succeeded=False, reason="configuration_error",
        details={"error": str(original)}, _diagnostic_sources=((None, original),))
    mode = object()
    lifecycle.CommandMode = SimpleNamespace(RUN=object())
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_snapshot = _project
    captured = []
    async def emit(deps, message, *, structured=False):
        captured.append((json.loads(message), structured))
    lifecycle._emit_session_error = emit
    deps = SimpleNamespace(mode=mode, startup_error=outcome, log_runtime=None)
    # 日志记录中的命令名称与运行分支独立。
    deps.mode = SimpleNamespace(value="submit")
    result = _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
        poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert result is outcome
    assert captured[0][1] is True
    assert captured[0][0]["exceptions"][0]["message"] == "startup failed"


@pytest.mark.parametrize("channel", ["missing", "failed", "written"])
def test_terminal_delivery_preserves_summary_and_log_failure(modules, channel):
    _, _, lifecycle = modules
    messages, records = [], []
    async def deliver(record):
        records.append(record)
        if channel == "failed":
            raise OSError("log channel failed")
        return SimpleNamespace(delivered=True, fallback_written=False)
    deps = SimpleNamespace(log_path_unsafe=False, log_runtime=None if channel == "missing" else
                           SimpleNamespace(channel=SimpleNamespace(deliver_important=deliver)))
    sys.modules["camctl.logging_runtime.service"].LogRecord = lambda **kwargs: SimpleNamespace(**kwargs)
    sys.modules["camctl.logging_runtime.service"].admission_result_for = lambda error: None
    lifecycle._write_stderr_diagnostic = lambda deps, message: messages.append(message)
    _run(lifecycle._emit_session_error(deps, '{"reason":"state_db_error"}', structured=True))
    if channel == "written":
        assert not messages
        assert records[0].structured is True
    else:
        assert '{"reason":"state_db_error"}' in messages[0]
        if channel == "failed":
            assert "log channel failed" in messages[0]


@pytest.mark.parametrize("frozen", [False, True])
def test_direct_exception_only_skips_a_prepared_record(modules, frozen):
    _, _, lifecycle = modules
    original = RuntimeError("direct failure")
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: {"diagnostic_ref": "id"} if frozen else None
    diagnostics.diagnostic_snapshot = _project
    message = lifecycle._direct_exception_message(SimpleNamespace(mode=SimpleNamespace(value="run")), original)
    if frozen:
        assert message is None
    else:
        assert json.loads(message)["exception"]["message"] == "direct failure"


def test_all_exception_positions_share_one_projection_context(modules):
    _, service, lifecycle = modules
    first, second = ValueError("first"), OSError("second")
    group = ExceptionGroup("group", [first, second])
    outcome = service._transaction_error_outcome(group, None, stage="execution")
    seen = []
    def snapshot(document):
        seen.append(document)
        return {"reason": document["reason"]}
    sys.modules["camctl.logging_runtime.diagnostics"].diagnostic_snapshot = snapshot
    lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome)
    assert len(seen) == 1
    assert seen[0]["details"]["exception"] is group
    assert seen[0]["details"]["secondary_errors"][0]["details"]["exception"] is second


@pytest.mark.parametrize("reason", ["state_db_error", "configuration_error"])
@pytest.mark.parametrize("prepared", [False, True])
def test_worker_shutdown_references_same_prepared_diagnostic_without_losing_machine_facts(modules, reason, prepared):
    outcome_type, _, lifecycle = modules
    initial = outcome_type.SessionOutcome(succeeded=True)
    failure = SimpleNamespace(error_code="db_error", error_message="worker failed",
                              error_kind=SimpleNamespace(value="db"), raw="precise worker facts")
    snapshot = lifecycle._worker_failure_details(failure)
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    state = object()
    diagnostics.diagnostic_reference_for = lambda value: (
        {"diagnostic_ref": "worker-id"} if value is state and prepared else None)
    diagnostics.diagnostic_snapshot = _project
    build = lifecycle._shutdown_state_outcome if reason == "state_db_error" else lifecycle._shutdown_configuration_outcome
    outcome = build(initial, failure, worker_details=snapshot)
    outcome = lifecycle._attach_worker_diagnostic(outcome, snapshot, state)
    assert outcome.reason == reason
    assert outcome.details["worker_failure"] is snapshot
    assert outcome.details["worker_failure"]["raw"] == "precise worker facts"
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert document["details"]["worker_failure"] == (
        {"diagnostic_ref": "worker-id"} if prepared else snapshot)


def test_new_cleanup_error_is_recorded_even_when_original_has_diagnostic(modules):
    _, service, _ = modules
    primary, cleanup = RuntimeError("original"), OSError("close failed")
    calls = []
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = lambda error, **kw: calls.append((error, kw))
    _, selected = service._merge_cleanup_errors(None, primary, [(cleanup, "shutdown", "state_db_error")])
    assert selected is primary
    assert calls == [(cleanup, {"stage": "shutdown"})]
    assert any("close failed" in note for note in selected.__notes__)


@pytest.fixture
def terminal_world(modules):
    import asyncio
    from contextlib import nullcontext

    outcome_type, _, lifecycle = modules
    actual = _load("camctl.operations._isolated_exception_diagnostics", ROOT / "operations" / "error_diagnostics.py")
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: None
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    lifecycle.CommandMode = SimpleNamespace(RUN=object())
    lifecycle._failure_log_wiring = lambda deps: (None, None)
    lifecycle.SessionContext = lambda **kwargs: SimpleNamespace(**kwargs)
    lifecycle.SystemClock = lambda: object()
    lifecycle.AcceptanceRepository = lambda: object()
    lifecycle.SessionRepository = lambda: object()
    lifecycle._clock_policy_factory = lambda config: object()
    lifecycle._local_work = lambda *args: None
    lifecycle.measure_stage = lambda *args: nullcontext()
    lifecycle.record_event = lambda *args, **kwargs: None
    delivered = []
    async def emit(deps, message, *, structured=False):
        delivered.append((json.loads(message), structured))
    lifecycle._emit_session_error = emit
    mode = SimpleNamespace(value="submit")
    deps = SimpleNamespace(mode=mode, startup_error=None, log_runtime=None,
        config=SimpleNamespace(session=SimpleNamespace(plan_poll_interval_ms=500, wakeup_margin_ms=5)),
        catalog=None, session_lock=None, admission_lock=None, notifier=None,
        capture_baselines={}, capture_result_scans={})
    return outcome_type, lifecycle, actual, asyncio, deps, delivered


def _terminal_exception(kind, actual, asyncio):
    original = actual.DiagnosticError("read failed", diagnostic_details={"stderr_hex": "ff00", "remaining": 17})
    if kind == "ordinary":
        return original
    if kind == "read_close":
        return ExceptionGroup("read and close", [original, OSError("close failed")])
    if kind == "wide":
        return ExceptionGroup("wide", [ValueError(f"leaf-{index}") for index in range(23)])
    if kind == "deep":
        for index in range(16):
            original = ExceptionGroup(f"depth-{index}", [original])
        return original
    if kind == "cancel":
        return asyncio.CancelledError("original cancel")
    return BaseExceptionGroup("read and cancel", [original, asyncio.CancelledError("original cancel")])


@pytest.mark.parametrize("kind", ["ordinary", "read_close", "wide", "deep", "cancel", "cancel_group"])
def test_terminal_actual_entry_retains_exception_graph_and_control_identity(terminal_world, kind):
    _, lifecycle, actual, asyncio, deps, delivered = terminal_world
    failure = _terminal_exception(kind, actual, asyncio)
    async def session(*args):
        raise failure
    lifecycle.run_session = session
    with pytest.raises(BaseException) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
             poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is failure
    assert len(delivered) == 1 and delivered[0][1] is True
    document = delivered[0][0]["exception"]
    if kind == "ordinary":
        assert document["diagnostic_details"] == {"stderr_hex": "ff00", "remaining": 17}
    elif kind == "read_close":
        assert document["exceptions"][0]["diagnostic_details"]["stderr_hex"] == "ff00"
        assert document["exceptions"][1]["message"] == "close failed"
    elif kind == "wide":
        assert [leaf["message"] for leaf in document["exceptions"]] == [f"leaf-{index}" for index in range(23)]
    elif kind == "deep":
        for _ in range(16):
            document = document["exceptions"][0]
        assert document["diagnostic_details"]["stderr_hex"] == "ff00"
    elif kind == "cancel":
        assert document["type"] == "asyncio.exceptions.CancelledError"
        assert document["message"] == "original cancel"
    else:
        assert document["exceptions"][0]["diagnostic_details"]["stderr_hex"] == "ff00"
        assert document["exceptions"][1]["type"] == "asyncio.exceptions.CancelledError"


@pytest.mark.parametrize("startup", [False, True])
def test_terminal_actual_entry_preserves_machine_details_for_both_failure_paths(terminal_world, startup):
    outcome_type, lifecycle, _, _, deps, delivered = terminal_world
    details = {"stage": "execution", "exception": {"type": "read", "message": "full raw diagnostic"}}
    outcome = outcome_type.SessionOutcome(succeeded=False, reason="state_db_error", details=details)
    if startup:
        deps.startup_error = outcome
    async def session(*args):
        return outcome
    lifecycle.run_session = session
    result = _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
        poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert result is outcome
    assert delivered[0][0]["details"] == details
    assert outcome.details is details


@pytest.mark.parametrize("primary_control", [False, True])
def test_cleanup_diagnostic_control_preserves_prior_control_priority(modules, primary_control):
    import asyncio
    _, service, _ = modules
    primary = asyncio.CancelledError("primary control") if primary_control else RuntimeError("primary failure")
    cleanup = OSError("close failed")
    logging_control = asyncio.CancelledError("diagnostic canceled")
    def fail(*args, **kwargs):
        raise logging_control
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    _, selected = service._merge_cleanup_errors(None, primary, [(cleanup, "shutdown", "state_db_error")])
    assert selected is (primary if primary_control else logging_control)
    assert any("close failed" in note for note in selected.__notes__)
    assert any(("diagnostic canceled" if primary_control else "primary failure") in note
               for note in selected.__notes__)


def test_equal_worker_failures_keep_independent_references_and_priority(modules):
    outcome_type, _, lifecycle = modules
    initial = outcome_type.SessionOutcome(succeeded=False, reason="report_error", details={"report": "pending"})
    failure = SimpleNamespace(error_code="db_error", error_message="worker failed",
                              error_kind=SimpleNamespace(value="db"), raw="same facts")
    first, second = lifecycle._worker_failure_details(failure), lifecycle._worker_failure_details(failure)
    assert first == second and first is not second
    first_state, second_state = object(), object()
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda value: ({"diagnostic_ref": "first"}
        if value is first_state else {"diagnostic_ref": "second"} if value is second_state else None)
    diagnostics.diagnostic_snapshot = _project
    outcome = lifecycle._shutdown_state_outcome(initial, failure, worker_details=first)
    outcome = lifecycle._attach_worker_diagnostic(outcome, first, first_state)
    outcome = lifecycle._shutdown_configuration_outcome(outcome, failure, worker_details=second)
    outcome = lifecycle._attach_worker_diagnostic(outcome, second, second_state)
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert outcome.reason == "state_db_error"
    assert outcome.details["worker_failure"] == first
    assert outcome.details["secondary_errors"][-1]["details"]["worker_failure"] == second
    assert document["details"]["worker_failure"] == {"diagnostic_ref": "first"}
    assert document["details"]["secondary_errors"][-1]["details"]["worker_failure"] == {"diagnostic_ref": "second"}


def test_terminal_actual_entry_records_new_cleanup_after_prepared_primary(terminal_world):
    _, lifecycle, _, _, deps, delivered = terminal_world
    primary, cleanup = RuntimeError("already prepared"), OSError("new close error")
    async def session(*args):
        raise primary
    lifecycle.run_session = session
    class Pending:
        in_call = False
        def close(self):
            raise cleanup
    deps.capture_baselines = {"first": Pending()}
    deps.background_flows = ()
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "primary"}
        if error is primary else None)
    records = []
    diagnostics.record_error = lambda error, **kwargs: records.append((error, kwargs))
    with pytest.raises(RuntimeError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is primary
    assert records == [(cleanup, {"stage": "shutdown", "context": {"resource": "基准读取缓存关闭异常"}})]
    assert any("new close error" in note for note in primary.__notes__)
    assert not delivered


@pytest.mark.parametrize("original_control,cleanup_control,diagnostic_control,selection", [
    (False, True, True, "cleanup"),
    (True, True, True, "original"),
    (False, False, True, "diagnostic"),
    (False, False, False, "original"),
])
def test_cleanup_control_precedes_new_diagnostic_control(modules, original_control, cleanup_control, diagnostic_control, selection):
    import asyncio
    _, service, _ = modules
    original = asyncio.CancelledError("original") if original_control else RuntimeError("original")
    cleanup = asyncio.CancelledError("cleanup") if cleanup_control else OSError("cleanup")
    diagnostic = asyncio.CancelledError("diagnostic") if diagnostic_control else OSError("diagnostic")
    calls = []
    def fail(error, **kwargs):
        calls.append(error)
        raise diagnostic
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    _, selected = service._merge_cleanup_errors(None, original, [(cleanup, "shutdown", "state_db_error")])
    assert selected is {"original": original, "cleanup": cleanup, "diagnostic": diagnostic}[selection]
    assert calls == [cleanup]
    notes = "\n".join(getattr(selected, "__notes__", ()))
    assert all(str(error) in notes for error in (original, cleanup, diagnostic) if error is not selected)


def test_later_real_cleanup_control_precedes_earlier_diagnostic_control(modules):
    import asyncio
    _, service, _ = modules
    original = RuntimeError("original")
    first = OSError("first cleanup")
    second = asyncio.CancelledError("second cleanup")
    diagnostic = asyncio.CancelledError("diagnostic")
    calls = []
    def fail(error, **kwargs):
        calls.append(error)
        raise diagnostic
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    _, selected = service._merge_cleanup_errors(None, original, [
        (first, "shutdown", "state_db_error"), (second, "shutdown", "state_db_error")])
    assert selected is second
    assert calls == [first, second]
    notes = "\n".join(selected.__notes__)
    assert all(str(error) in notes for error in (original, first, diagnostic))


@pytest.mark.parametrize("first_is_control", [False, True])
def test_cleanup_without_original_keeps_business_order(modules, first_is_control):
    import asyncio
    _, service, _ = modules
    first = asyncio.CancelledError("first control") if first_is_control else OSError("first ordinary")
    second = asyncio.CancelledError("second control")
    diagnostic = asyncio.CancelledError("diagnostic control")
    calls = []
    def fail(error, **kwargs):
        calls.append(error)
        raise diagnostic
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    outcome, selected = service._merge_cleanup_errors(None, None, [
        (first, "shutdown", "state_db_error"), (second, "shutdown", "state_db_error")])
    assert selected is (first if first_is_control else second)
    assert calls == ([second] if first_is_control else [first])
    assert (outcome is None) is first_is_control
    notes = "\n".join(selected.__notes__)
    assert str(second if first_is_control else first) in notes
    if first_is_control:
        assert "diagnostic control" in notes


def test_cleanup_control_selection_spans_both_closure_batches(modules):
    import asyncio
    _, service, _ = modules
    original = RuntimeError("original")
    local = OSError("local cleanup")
    lease = asyncio.CancelledError("lease control")
    diagnostic = asyncio.CancelledError("diagnostic control")
    calls, pending = [], []
    def fail(error, **kwargs):
        calls.append(error)
        raise diagnostic
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    outcome, chosen = service._merge_cleanup_errors(None, original, [(local, "shutdown", "state_db_error")],
                                                     pending_diagnostics=pending)
    assert chosen is original and not calls
    outcome, chosen = service._merge_cleanup_errors(outcome, chosen, [(lease, "shutdown", "internal_error")],
                                                     pending_diagnostics=pending)
    assert chosen is lease and not calls
    chosen = service._deliver_cleanup_diagnostics(chosen, pending)
    assert chosen is lease
    assert calls == [local, lease]
    notes = "\n".join(chosen.__notes__)
    assert all(str(error) in notes for error in (original, local, diagnostic))


def test_handoff_diagnostic_waits_for_all_actual_closures_and_retains_its_error(modules):
    import asyncio
    _, service, _ = modules
    handoff = OSError("handoff failure")
    closing = asyncio.CancelledError("lease control")
    diagnostic = asyncio.CancelledError("diagnostic control")
    events = []
    def fail(error, **kwargs):
        events.append(("record", error))
        raise diagnostic
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = fail
    pending = [service._PendingCleanupDiagnostic(handoff, "diagnostic_result_handoff", True)]
    events.append(("lease closed", closing))
    _, chosen = service._merge_cleanup_errors(None, None, [(closing, "shutdown", "internal_error")],
                                               pending_diagnostics=pending)
    chosen = service._deliver_cleanup_diagnostics(chosen, pending)
    assert chosen is closing
    assert events == [("lease closed", closing), ("record", handoff)]
    notes = "\n".join(chosen.__notes__)
    assert "handoff failure" in notes and "diagnostic control" in notes


def test_prior_ordinary_cleanup_is_recorded_when_later_control_may_already_be_frozen(modules):
    import asyncio
    _, service, _ = modules
    ordinary, control = OSError("ordinary cleanup"), asyncio.CancelledError("control already frozen")
    calls, pending = [], []
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = lambda error, **kwargs: calls.append(error)
    outcome, selected = service._merge_cleanup_errors(None, None, [(ordinary, "shutdown", "state_db_error")],
                                                       pending_diagnostics=pending)
    assert selected is None and not calls
    _, selected = service._merge_cleanup_errors(outcome, selected, [(control, "shutdown", "internal_error")],
                                                pending_diagnostics=pending)
    assert service._deliver_cleanup_diagnostics(selected, pending) is control
    assert calls == [ordinary]


def test_ordinary_cleanup_result_does_not_create_redundant_record(modules):
    _, service, _ = modules
    ordinary = OSError("ordinary cleanup")
    calls = []
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = lambda error, **kwargs: calls.append(error)
    outcome, selected = service._merge_cleanup_errors(None, None, [(ordinary, "shutdown", "state_db_error")])
    assert selected is None and outcome.reason == "state_db_error"
    assert not calls


def test_late_handoff_diagnostic_control_also_delivers_prior_ordinary_result(modules):
    import asyncio
    _, service, _ = modules
    ordinary, handoff = OSError("ordinary"), OSError("handoff")
    control = asyncio.CancelledError("diagnostic control")
    calls, pending = [], []
    def record(error, **kwargs):
        calls.append(error)
        if error is handoff:
            raise control
    sys.modules["camctl.logging_runtime.diagnostics"].record_error = record
    outcome, original = service._merge_cleanup_errors(None, None, [(ordinary, "shutdown", "state_db_error")],
                                                      pending_diagnostics=pending)
    pending.append(service._PendingCleanupDiagnostic(handoff, "diagnostic_result_handoff", True))
    selected = service._deliver_cleanup_diagnostics(original, pending)
    assert selected is control and outcome.reason == "state_db_error"
    assert calls == [handoff, ordinary]
    assert any("handoff" in note for note in selected.__notes__)


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("diagnostic_control", [False, True])
def test_prepared_primary_still_outputs_new_cleanup_and_diagnostic_failures(terminal_world, modules, handoff, diagnostic_control):
    _, service, _ = modules
    _, lifecycle, actual, asyncio, deps, delivered = terminal_world
    primary = asyncio.CancelledError("prepared primary")
    cleanup = OSError("new handoff failure" if handoff else "new cleanup failure")
    diagnostic = (asyncio.CancelledError("diagnostic interrupted") if diagnostic_control
                  else OSError("diagnostic write failed"))
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "primary-id"} if error is primary else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail(*args, **kwargs):
        raise diagnostic
    diagnostics.record_error = fail
    async def session(*args):
        if handoff:
            pending = [service._PendingCleanupDiagnostic(cleanup, "diagnostic_result_handoff", True)]
            selected = service._deliver_cleanup_diagnostics(primary, pending)
        else:
            _, selected = service._merge_cleanup_errors(None, primary, [(cleanup, "shutdown", "state_db_error")])
        raise selected
    lifecycle.run_session = session
    with pytest.raises(asyncio.CancelledError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is primary
    assert len(delivered) == 1 and delivered[0][1] is True
    document = delivered[0][0]
    assert document["exception"] == {"diagnostic_ref": "primary-id"}
    failure = document["cleanup_diagnostic_failures"][0]
    assert failure["error"]["message"] == str(cleanup)
    assert failure["diagnostic_error"]["message"] == str(diagnostic)
    assert failure["stage"] == ("diagnostic_result_handoff" if handoff else "shutdown")


def test_new_prepared_diagnostic_control_keeps_the_previous_ordinary_error(terminal_world, modules):
    _, service, _ = modules
    _, lifecycle, actual, asyncio, deps, delivered = terminal_world
    previous, cleanup = RuntimeError("original failure"), OSError("cleanup failure")
    selected_control = asyncio.CancelledError("prepared diagnostic control")
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "diagnostic-id"}
        if error is selected_control else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail(*args, **kwargs):
        raise selected_control
    diagnostics.record_error = fail
    async def session(*args):
        _, selected = service._merge_cleanup_errors(None, previous, [(cleanup, "shutdown", "state_db_error")])
        raise selected
    lifecycle.run_session = session
    with pytest.raises(asyncio.CancelledError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is selected_control
    document = delivered[0][0]
    assert document["exception"] == {"diagnostic_ref": "diagnostic-id"}
    assert document["cleanup_diagnostic_failures"][0]["previous_error"]["message"] == "original failure"


@pytest.mark.parametrize("channel_state", ["missing", "failed"])
def test_prepared_primary_new_failures_use_remaining_stderr_channel(modules, channel_state):
    import asyncio
    _, service, lifecycle = modules
    actual = _load("camctl.operations._isolated_exception_diagnostics", ROOT / "operations" / "error_diagnostics.py")
    original, cleanup, failure = asyncio.CancelledError("primary"), OSError("cleanup"), OSError("diagnostic prepare")
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda value: ({"diagnostic_ref": "primary-id"}
        if value is original else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail_record(*args, **kwargs):
        raise failure
    diagnostics.record_error = fail_record
    _, selected = service._merge_cleanup_errors(None, original, [(cleanup, "shutdown", "state_db_error")])
    assert selected is original
    message = lifecycle._direct_exception_message(SimpleNamespace(mode=SimpleNamespace(value="run")), selected)
    captured = []
    lifecycle._write_stderr_diagnostic = lambda deps, text: captured.append(text)
    async def fail_delivery(record):
        raise OSError("log channel write failed")
    deps = SimpleNamespace(log_path_unsafe=False, log_runtime=None if channel_state == "missing" else
        SimpleNamespace(channel=SimpleNamespace(deliver_important=fail_delivery)))
    sys.modules["camctl.logging_runtime.service"].LogRecord = lambda **kwargs: SimpleNamespace(**kwargs)
    sys.modules["camctl.logging_runtime.service"].admission_result_for = lambda error: None
    _run(lifecycle._emit_session_error(deps, message, structured=True))
    assert len(captured) == 1
    summary = json.loads(captured[0].split("\n", 1)[0])
    assert summary["exception"] == {"diagnostic_ref": "primary-id"}
    assert summary["cleanup_diagnostic_failures"][0]["error"]["message"] == "cleanup"
    assert summary["cleanup_diagnostic_failures"][0]["diagnostic_error"]["message"] == "diagnostic prepare"
    if channel_state == "failed":
        assert "log channel write failed" in captured[0]


def test_bootstrap_cleanup_diagnostic_failure_does_not_interrupt_other_closures(terminal_world):
    _, lifecycle, actual, asyncio, deps, delivered = terminal_world
    primary, cleanup = asyncio.CancelledError("primary"), OSError("cache close")
    diagnostic = asyncio.CancelledError("diagnostic canceled")
    events = []
    async def session(*args):
        raise primary
    lifecycle.run_session = session
    class Pending:
        in_call = False
        def __init__(self, name, error=None): self.name, self.error = name, error
        def close(self):
            events.append(self.name)
            if self.error is not None:
                raise self.error
    deps.capture_baselines = {"first": Pending("first", cleanup), "second": Pending("second")}
    deps.background_flows = ()
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "primary-id"} if error is primary else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail_record(error, **kwargs):
        events.append("record")
        raise diagnostic
    diagnostics.record_error = fail_record
    with pytest.raises(asyncio.CancelledError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is primary
    assert events == ["first", "second", "record"]
    assert delivered[0][0]["exception"] == {"diagnostic_ref": "primary-id"}
    extra = delivered[0][0]["cleanup_diagnostic_failures"][0]
    assert extra["error"]["message"] == "cache close"
    assert extra["diagnostic_error"]["message"] == "diagnostic canceled"


def test_bootstrap_new_cleanup_without_primary_keeps_failed_diagnostic(terminal_world):
    outcome_type, lifecycle, actual, _, deps, delivered = terminal_world
    cleanup, diagnostic = OSError("cache close"), OSError("diagnostic failed")
    async def session(*args):
        return outcome_type.SessionOutcome(succeeded=True)
    lifecycle.run_session = session
    class Pending:
        in_call = False
        def close(self):
            raise cleanup
    deps.capture_baselines = {"first": Pending()}
    deps.background_flows = ()
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "cleanup-id"} if error is cleanup else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail_record(*args, **kwargs):
        raise diagnostic
    diagnostics.record_error = fail_record
    with pytest.raises(OSError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is cleanup
    assert len(delivered) == 1
    assert delivered[0][0]["exception"] == {"diagnostic_ref": "cleanup-id"}
    assert delivered[0][0]["cleanup_diagnostic_failures"][0]["diagnostic_error"]["message"] == "diagnostic failed"


def test_worker_record_and_state_link_happen_after_business_control_selection(modules):
    import asyncio
    outcome_type, service, lifecycle = modules
    failure = SimpleNamespace(error_code="db", error_message="worker failed",
        error_kind=SimpleNamespace(value="db"), raw="original worker facts")
    snapshot = lifecycle._worker_failure_details(failure)
    outcome = lifecycle._shutdown_state_outcome(outcome_type.SessionOutcome(succeeded=True),
                                                failure, worker_details=snapshot)
    state, events = object(), []
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    def record(error, **kwargs):
        events.append(("record", error))
        return state
    diagnostics.record_error = record
    diagnostics.diagnostic_reference_for = lambda value: {"diagnostic_ref": "worker"} if value is state else None
    diagnostics.diagnostic_snapshot = _project
    def attach(value):
        nonlocal outcome
        events.append(("link", value))
        outcome = lifecycle._attach_worker_diagnostic(outcome, snapshot, value)
    pending = [service._PendingCleanupDiagnostic(snapshot, "report_shutdown", on_record=attach)]
    control = asyncio.CancelledError("business control")
    events.append(("close", control))
    _, selected = service._merge_cleanup_errors(None, None, [(control, "shutdown", "state_db_error")],
                                                  pending_diagnostics=pending)
    assert service._deliver_cleanup_diagnostics(selected, pending) is control
    assert events == [("close", control), ("record", snapshot), ("link", state)]
    document = json.loads(lifecycle._failed_outcome_message(SimpleNamespace(mode=SimpleNamespace(value="run")), outcome))
    assert document["details"]["worker_failure"] == {"diagnostic_ref": "worker"}
    assert outcome.details["worker_failure"]["raw"] == "original worker facts"


def test_worker_record_failure_preserves_original_mapping_in_terminal_summary(terminal_world, modules):
    _, service, _ = modules
    _, lifecycle, actual, asyncio, deps, delivered = terminal_world
    primary, diagnostic = asyncio.CancelledError("primary"), OSError("worker diagnostic failed")
    snapshot = {"error_message": "worker failed", "raw": "complete facts"}
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "primary-id"} if error is primary else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail(*args, **kwargs): raise diagnostic
    diagnostics.record_error = fail
    async def session(*args):
        raise service._deliver_cleanup_diagnostics(primary, [service._PendingCleanupDiagnostic(snapshot, "report_shutdown")])
    lifecycle.run_session = session
    with pytest.raises(asyncio.CancelledError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is primary
    detail = delivered[0][0]["cleanup_diagnostic_failures"][0]
    assert detail["error"] == snapshot
    assert detail["diagnostic_error"]["message"] == "worker diagnostic failed"


def test_stderr_control_failure_after_log_close_keeps_original_control(terminal_world):
    _, lifecycle, actual, asyncio, deps, _ = terminal_world
    primary = asyncio.CancelledError("primary control")
    close_error, diagnostic = OSError("log close"), OSError("diagnostic prepare")
    stderr_control = asyncio.CancelledError("stderr canceled")
    async def session(*args): raise primary
    async def close(*args, **kwargs): raise close_error
    lifecycle.run_session = session
    lifecycle._close_log_runtime = close
    def stderr(*args): raise stderr_control
    lifecycle._write_stderr_diagnostic = stderr
    deps.log_runtime = object()
    diagnostics = sys.modules["camctl.logging_runtime.diagnostics"]
    diagnostics.diagnostic_reference_for = lambda error: ({"diagnostic_ref": "primary-id"} if error is primary else None)
    diagnostics.diagnostic_snapshot = lambda value: _project(value, actual.exception_diagnostic)
    def fail(*args, **kwargs): raise diagnostic
    diagnostics.record_error = fail
    with pytest.raises(asyncio.CancelledError) as caught:
        _run(lifecycle._execute_command(deps, None, flows={}, restricted_flows={}, wake=None,
            poll_interval_s=None, finish_command=lambda **kwargs: None))
    assert caught.value is primary
    assert any("stderr canceled" in note for note in primary.__notes__)
