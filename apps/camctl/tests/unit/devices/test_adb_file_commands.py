"""单文件正式命令只接受完整、唯一且对应原目标的观察。"""
from dataclasses import replace
from decimal import Decimal
import json
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket, EffectState
from camctl.operations.process import LocalExit, RawToolOutcome

BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")
PATH = "/DCIM/a 'quote'\nzero.OSV"


def _request(kind, path=PATH):
    operation = "delete" if kind is CameraCall.DELETE else "query"
    return ControlRequest(operation, BINDING, {
        "cleanup_item_id": "7", "identity_key": json.dumps([BINDING.device_id, BINDING.driver_id, path]),
        "locator": {"path": path}}, AttemptTicket(1, operation, "7", "cleanup/7", 2), Decimal("3"))


def _command(kind=CameraCall.FILE_QUERY, request=None):
    from camctl.devices.drivers.adb_cameras.file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
    return file_command_factory(kind, tools=ShellFileTools(), evidence=EvidenceRegistry(FILE_COMMAND_EVIDENCE))(
        request or _request(kind), "serial-1", None, None)


def _frame(kind=CameraCall.FILE_QUERY, *, present=b"1", path=PATH, rm=b"-", scan=b"0"):
    operation = b"delete" if kind is CameraCall.DELETE else b"query"
    return b"\0".join((b"CAMCTL-FILE/1", operation, path.encode(), present, rm, scan, b"END", b""))


def _raw(data, *, code=0, error=None, failure=None):
    return RawToolOutcome(LocalExit(exit_code=code), data, error, None, output_failure=failure, stderr=b"")


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("kind", [CameraCall.FILE_QUERY, CameraCall.DELETE])
def test_complete_frame_proves_only_original_file_presence(kind, present):
    raw = _raw(_frame(kind, present=b"1" if present else b"0", rm=b"0" if kind is CameraCall.DELETE else b"-"))
    facts = _command(kind).interpreter.interpret(raw)
    name = "file_delete_presence" if kind is CameraCall.DELETE else "file_presence"
    assert facts.observations == (DeviceObservation(name, 1, {"cleanup_item_id": "7", "present": present}),)
    assert facts.error is None
    assert facts.effect is (EffectState.UNKNOWN if kind is CameraCall.DELETE and present else EffectState.CONFIRMED)


@pytest.mark.parametrize("data", [b"", _frame()[:-1], _frame() + _frame(), _frame(path="/DCIM/another.OSV"),
    _frame(present=b"2"), _frame().replace(b"query\0", b"delete\0"),
    _frame().replace(b"END\0", b"END\0suffix"), _frame(rm=b"0"),
    _frame().replace(PATH.encode(), b"\xff")])
def test_invalid_or_partial_frame_has_no_file_fact(data):
    facts = _command().interpreter.interpret(_raw(data))
    assert facts.observations == () and facts.effect is EffectState.UNKNOWN
    assert facts.error is not None


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_truncated_channel_invalidates_even_a_complete_retained_frame(channel):
    facts = _command().interpreter.interpret(_raw(_frame(), failure=channel + ": output_limit_exceeded"))
    assert facts.observations == () and facts.error is not None


@pytest.mark.parametrize("error,code", [("timeout", 0), ("cancelled", 0), (None, 1)])
def test_reliable_observation_survives_later_call_error(error, code):
    facts = _command().interpreter.interpret(_raw(_frame(), error=error, code=code))
    assert facts.observations == (DeviceObservation("file_presence", 1, {"cleanup_item_id": "7", "present": True}),)
    assert facts.error is not None


def test_failed_rm_does_not_erase_post_observation_or_prove_absence_by_itself():
    facts = _command(CameraCall.DELETE).interpreter.interpret(_raw(_frame(CameraCall.DELETE, present=b"1", rm=b"1")))
    assert facts.observations[0].data["present"] is True
    assert facts.error is not None and facts.effect is EffectState.UNKNOWN
    missing = _command(CameraCall.DELETE).interpreter.interpret(_raw(b"", code=1))
    assert missing.observations == ()


@pytest.mark.parametrize("kind", [CameraCall.DELETE, CameraCall.FILE_QUERY])
@pytest.mark.parametrize("change", ["locator", "binding", "target", "operation"])
def test_invalid_original_identity_is_rejected_before_building_command(kind, change):
    request = _request(kind)
    if change == "locator":
        request = replace(request, params={**request.params, "locator": {"path": "/DCIM/other"}})
    elif change == "binding":
        request = replace(request, binding=DeviceBinding("cam-other", BINDING.driver_id))
    elif change == "target":
        request = replace(request, params={**request.params, "cleanup_item_id": "8"})
    else:
        request = replace(request, ticket=replace(request.ticket, operation="stop"))
    with pytest.raises(ValueError):
        _command(kind, request)


def test_generated_script_keeps_special_path_in_one_shell_argument():
    command = _command(CameraCall.DELETE)
    remote = shlex.split(command.argv[5])
    assert remote[:2] == ["sh", "-c"] and len(remote) == 3
    assert shlex.quote(PATH) in remote[2]


@pytest.mark.parametrize("state,scan", [(b"unknown", b"1"), (b"type", b"0"), (b"duplicate", b"0")])
def test_completed_scan_without_applicable_file_fact_keeps_rm_and_scan_errors(state, scan):
    facts = _command(CameraCall.DELETE).interpreter.interpret(_raw(
        _frame(CameraCall.DELETE, present=state, rm=b"2", scan=scan), code=1))
    assert facts.observations == () and facts.effect is EffectState.UNKNOWN
    assert facts.error.details["rm_exit_code"] == 2
    assert facts.error.details["scan_exit_code"] == int(scan)


def test_failed_complete_scan_can_preserve_present_but_never_prove_absent():
    facts = _command().interpreter.interpret(_raw(_frame(scan=b"1"), code=1))
    assert facts.observations[0].data["present"] is True
    assert facts.error.details["scan_exit_code"] == 1
    absent = _command().interpreter.interpret(_raw(_frame(present=b"0", scan=b"1"), code=1))
    assert absent.observations == () and absent.error is not None


@pytest.mark.parametrize("target", ["0", "01", "-1", "9223372036854775808", 7, True, None])
def test_invalid_cleanup_identity_cannot_reach_the_command(target):
    request = _request(CameraCall.DELETE)
    request = replace(request, params={**request.params, "cleanup_item_id": target},
                      ticket=replace(request.ticket, target_id=target))
    with pytest.raises(ValueError):
        _command(CameraCall.DELETE, request)


@pytest.mark.parametrize("kind", [CameraCall.QUERY, "delete", None])
def test_file_factory_rejects_non_file_call_kinds(kind):
    from camctl.devices.drivers.adb_cameras.file_commands import file_command_factory, FILE_COMMAND_EVIDENCE
    with pytest.raises(ValueError):
        file_command_factory(kind, tools=ShellFileTools(), evidence=EvidenceRegistry(FILE_COMMAND_EVIDENCE))
