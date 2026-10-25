"""会话终端与运行日志保留完整异常图，控制异常仍按原身份传播。"""
import asyncio
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import application, lifecycle
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.drivers import builtin
from camctl.operations.error_diagnostics import DiagnosticError
from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import AdmissionResult, LogChannel
from camctl.session.outcome import SessionOutcome


def _failure(kind):
    read = DiagnosticError("read failed", diagnostic_details={"stderr_hex": "ff00", "remaining": 17})
    if kind == "ordinary":
        return read
    if kind == "read_close":
        return ExceptionGroup("read and close", [read, OSError("close failed")])
    if kind == "wide":
        return ExceptionGroup("wide", [ValueError(f"leaf-{index}") for index in range(23)])
    if kind == "deep":
        error = ValueError("deepest leaf")
        for index in range(16):
            error = ExceptionGroup(f"depth-{index}", [error])
        return error
    if kind == "cancel":
        return asyncio.CancelledError("original cancel")
    return BaseExceptionGroup("read and cancel", [read, asyncio.CancelledError("original cancel")])


def _document(message):
    return json.loads(message.split("异常诊断: ", 1)[1].splitlines()[0])


def _assert_complete(document, kind):
    if kind == "ordinary":
        assert document["diagnostic_details"] == {"stderr_hex": "ff00", "remaining": 17}
    elif kind == "read_close":
        assert document["exceptions"][0]["diagnostic_details"]["stderr_hex"] == "ff00"
        assert document["exceptions"][1]["message"] == "close failed"
    elif kind == "wide":
        assert [item["message"] for item in document["exceptions"]] == [f"leaf-{index}" for index in range(23)]
    elif kind == "deep":
        node = document
        for _ in range(16):
            node = node["exceptions"][0]
        assert node["message"] == "deepest leaf"
    elif kind == "cancel":
        assert document["type"] == "asyncio.exceptions.CancelledError"
        assert document["message"] == "original cancel"
    else:
        assert document["exceptions"][0]["diagnostic_details"]["stderr_hex"] == "ff00"
        assert document["exceptions"][1]["type"] == "asyncio.exceptions.CancelledError"


def _cli_world(monkeypatch, failure):
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: object())
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    monkeypatch.setattr(builtin, "register_builtin_drivers", lambda config: None)
    monkeypatch.setattr(lifecycle, "build_runtime", lambda *args, **kwargs: SimpleNamespace(log_runtime=None))
    monkeypatch.setattr(lifecycle, "execute_command", AsyncMock(side_effect=failure))
    close = Mock()
    monkeypatch.setattr(lifecycle, "close_runtime", close)
    return close, StringIO(), StringIO()


@pytest.mark.parametrize("kind", ["ordinary", "read_close", "wide", "deep", "cancel", "cancel_group"])
def test_cli_stderr_keeps_every_exception_before_original_control_propagates(monkeypatch, kind):
    failure = _failure(kind)
    close, out, err = _cli_world(monkeypatch, failure)

    if isinstance(failure, Exception):
        assert cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, err, None) == 1
    else:
        with pytest.raises(BaseException) as caught:
            cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, err, None)
        assert caught.value is failure

    assert out.getvalue() == ""
    assert "会话错误" in err.getvalue()
    _assert_complete(_document(err.getvalue()), kind)
    close.assert_called_once()
    assert close.call_args.kwargs["primary_error"] is failure


class _FailedStderr:
    def __init__(self, failure):
        self.failure = failure

    def write(self, value):
        raise self.failure


@pytest.mark.parametrize("primary_control", [False, True])
def test_cli_diagnostic_failure_preserves_control_priority_and_full_secondary_error(monkeypatch, primary_control):
    original = asyncio.CancelledError("original control") if primary_control else ValueError("original failure")
    diagnostic = OSError("stderr failed") if primary_control else asyncio.CancelledError("stderr canceled")
    _, out, _ = _cli_world(monkeypatch, original)

    with pytest.raises(BaseException) as caught:
        cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, _FailedStderr(diagnostic), None)

    selected, attached = (original, diagnostic) if primary_control else (diagnostic, original)
    assert caught.value is selected
    assert any(json.loads(note.split("异常诊断: ", 1)[1])["message"] == str(attached)
               for note in selected.__notes__ if "异常诊断: " in note)
    assert out.getvalue() == ""


def test_runtime_close_control_is_diagnosed_before_the_same_object_propagates(monkeypatch):
    original = _failure("ordinary")
    close, out, err = _cli_world(monkeypatch, original)
    selected = asyncio.CancelledError("close canceled")
    close.side_effect = selected

    with pytest.raises(asyncio.CancelledError) as caught:
        cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, err, None)

    assert caught.value is selected
    assert _document(err.getvalue())["message"] == "close canceled"
    assert any("ff00" in note for note in selected.__notes__)
    assert out.getvalue() == ""


def test_actual_runtime_close_diagnostic_cannot_replace_an_existing_control_group(monkeypatch):
    actual_close = lifecycle.close_runtime
    original = _failure("cancel_group")
    _, out, err = _cli_world(monkeypatch, original)
    secondary = asyncio.CancelledError("close diagnostic canceled")
    deps = SimpleNamespace(closed=False, failure_channel=SimpleNamespace(
        close=Mock(side_effect=OSError("close failed"))), log_runtime=None, stderr_diagnostic_failed=False)
    monkeypatch.setattr(lifecycle, "close_runtime", actual_close)
    monkeypatch.setattr(lifecycle, "build_runtime", lambda *args, **kwargs: deps)
    monkeypatch.setattr("sys.stderr", _FailedStderr(secondary))

    with pytest.raises(BaseExceptionGroup) as caught:
        cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, err, None)

    assert caught.value is original
    assert any(json.loads(note.split("异常诊断: ", 1)[1])["message"] == "close diagnostic canceled"
               for note in original.__notes__ if "异常诊断: " in note)
    _assert_complete(_document(err.getvalue()), "cancel_group")
    assert out.getvalue() == ""


def test_runtime_close_note_keeps_full_diagnostics_before_modifying_the_target():
    target = asyncio.CancelledError("original cancel")
    target.add_note("existing note")
    failure = DiagnosticError("close failed", diagnostic_details={"stderr_hex": "fe00"})
    failure.add_note("close note")
    failure.__context__ = target

    lifecycle._attach_runtime_close_note(target, "cache close", failure)

    assert target.__notes__[:3] == ["existing note", "cache close: DiagnosticError: close failed", "cache close附注: close note"]
    complete = [note for note in target.__notes__ if "异常诊断: " in note]
    assert len(complete) == 1
    document = _document(complete[0])
    assert document["diagnostic_details"] == {"stderr_hex": "fe00"}
    assert document["context"]["notes"] == ["existing note"]


def test_actual_runtime_close_stderr_keeps_a_secondary_ordinary_error(monkeypatch):
    actual_close = lifecycle.close_runtime
    original = _failure("ordinary")
    _, out, err = _cli_world(monkeypatch, original)
    failure = DiagnosticError("copy channel close failed", diagnostic_details={"errno": 28})
    deps = SimpleNamespace(closed=False, failure_channel=SimpleNamespace(
        close=Mock(side_effect=failure)), log_runtime=None, stderr_diagnostic_failed=False)
    monkeypatch.setattr(lifecycle, "close_runtime", actual_close)
    monkeypatch.setattr(lifecycle, "build_runtime", lambda *args, **kwargs: deps)
    closing_stderr = StringIO()
    monkeypatch.setattr("sys.stderr", closing_stderr)

    assert cli._execute_session_command(cli.Command(cli.CommandKind.RUN, None, None), out, err, None) == 1

    assert "异常诊断: " in closing_stderr.getvalue()
    assert _document(closing_stderr.getvalue())["diagnostic_details"] == {"errno": 28}
    assert _document(err.getvalue())["diagnostic_details"]["stderr_hex"] == "ff00"
    assert out.getvalue() == ""


@pytest.mark.asyncio
async def test_shared_log_close_exception_keeps_the_full_object_on_stderr(monkeypatch):
    from camctl.logging_runtime import lifecycle as logging_lifecycle

    actual_close = lifecycle._close_log_runtime
    deps, _, _, stderr = _lifecycle_world(monkeypatch)
    failure = DiagnosticError("log close failed", diagnostic_details={"signal_failures": ["permission denied"]})
    failure.add_note("original close note")
    monkeypatch.setattr(logging_lifecycle, "close_logging", AsyncMock(side_effect=failure))

    with pytest.raises(DiagnosticError) as caught:
        await actual_close(deps)

    assert caught.value is failure
    assert "异常诊断: " in stderr.getvalue()
    assert _document(stderr.getvalue())["diagnostic_details"] == {"signal_failures": ["permission denied"]}
    assert _document(stderr.getvalue())["notes"] == ["original close note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, delivered", [
    (AdmissionKind.ACCEPTED, True), (AdmissionKind.ACCEPTED, False),
    (AdmissionKind.ACCEPTED, None), (AdmissionKind.CHANNEL_FAILED, None),
])
async def test_real_session_log_entry_falls_back_without_a_delivery_receipt(monkeypatch, kind, delivered):
    deps, _, _, stderr = _lifecycle_world(monkeypatch)
    channel = deps.log_runtime.channel
    result = AdmissionResult(AdmissionDecision(kind, LogLevel.ERROR), 4, delivered)
    channel.alog.return_value = result
    channel.deliver_important.side_effect = None
    channel.deliver_important.return_value = result
    message = "read failed\n异常诊断: {\"stderr_hex\":\"ff00\"}"

    await lifecycle._emit_session_error(deps, message)

    assert stderr.getvalue() == ("" if delivered is True else message + "\n")
    channel.deliver_important.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["missing", "unsafe"])
async def test_unavailable_log_path_uses_stderr_without_reusing_the_channel(monkeypatch, unavailable):
    deps, _, _, stderr = _lifecycle_world(monkeypatch)
    channel = deps.log_runtime.channel
    if unavailable == "missing":
        deps.log_runtime = None
    else:
        deps.log_path_unsafe = True

    await lifecycle._emit_session_error(deps, "full original diagnostic")

    assert stderr.getvalue() == "full original diagnostic\n"
    channel.deliver_important.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_delivery_exception_fallback_keeps_both_full_diagnostics(monkeypatch):
    deps, _, _, stderr = _lifecycle_world(monkeypatch)
    deps.log_runtime.channel.deliver_important.side_effect = DiagnosticError(
        "log storage failed", diagnostic_details={"errno": 28, "path": "/log"})
    message = "read failed\n异常诊断: {\"stderr_hex\":\"ff00\"}"

    await lifecycle._emit_session_error(deps, message)

    assert message in stderr.getvalue()
    assert _document(stderr.getvalue()) == {"stderr_hex": "ff00"}
    last = stderr.getvalue().rsplit("异常诊断: ", 1)[1]
    assert json.loads(last)["diagnostic_details"] == {"errno": 28, "path": "/log"}


def _lifecycle_world(monkeypatch):
    records = []

    async def deliver(record):
        records.append(record)
        return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0, True)

    deps = lifecycle.RuntimeDeps(CommandMode.SUBMIT, load_config({}, ConfigDefaults()),
        Path("/state.db"), Path("/session.lock"), Path("/admission.lock"), None)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    channel.alog.return_value = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 0)
    channel.deliver_important.side_effect = deliver
    deps.log_runtime = SimpleNamespace(channel=channel)
    stderr = StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    monkeypatch.setattr(lifecycle, "_failure_log_wiring", lambda deps: (None, None))
    monkeypatch.setattr(lifecycle, "_close_log_runtime", AsyncMock())
    session = AsyncMock()
    monkeypatch.setattr(lifecycle, "run_session", session)
    return deps, records, session, stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "read_close", "wide", "deep", "cancel", "cancel_group"])
async def test_actual_session_failure_is_fully_logged_and_rethrows_the_same_object(monkeypatch, kind):
    deps, records, session, _ = _lifecycle_world(monkeypatch)
    failure = _failure(kind)
    session.side_effect = failure

    with pytest.raises(BaseException) as caught:
        await lifecycle.execute_command(deps, None, flows={})

    assert caught.value is failure
    assert len(records) == 1
    _assert_complete(_document(records[0].message), kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True])
async def test_failed_outcome_log_retains_the_existing_details(monkeypatch, startup):
    deps, records, session, _ = _lifecycle_world(monkeypatch)
    details = {"stage": "execution", "exception": {"type": "read", "message": "full raw diagnostic"}}
    outcome = SessionOutcome(succeeded=False, reason="state_db_error", details=details)
    if startup:
        deps.startup_error = outcome
    else:
        session.return_value = outcome

    assert await lifecycle.execute_command(deps, None, flows={}) is outcome
    assert len(records) == 1
    assert json.loads(records[0].message.split("诊断详情: ", 1)[1]) == details


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_control, diagnostic_control", [(True, False), (False, True), (False, False)])
async def test_logging_failure_preserves_selected_control_and_full_originals(monkeypatch, primary_control, diagnostic_control):
    deps, _, session, stderr = _lifecycle_world(monkeypatch)
    original = asyncio.CancelledError("original control") if primary_control else ValueError("original failure")
    diagnostic = asyncio.CancelledError("log canceled") if diagnostic_control else OSError("log failed")
    deps.log_runtime.channel.deliver_important.side_effect = diagnostic
    session.side_effect = original

    with pytest.raises(BaseException) as caught:
        await lifecycle.execute_command(deps, None, flows={})

    selected, attached = (diagnostic, original) if diagnostic_control else (original, diagnostic)
    assert caught.value is selected
    if diagnostic_control:
        assert any(json.loads(note.split("异常诊断: ", 1)[1])["message"] == str(attached)
                   for note in selected.__notes__ if "异常诊断: " in note)
    else:
        assert json.loads(stderr.getvalue().rsplit("异常诊断: ", 1)[1])["message"] == str(diagnostic)
