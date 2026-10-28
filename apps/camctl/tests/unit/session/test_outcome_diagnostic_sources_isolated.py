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
    merged = lifecycle._shutdown_state_outcome(outcome, failure)
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
