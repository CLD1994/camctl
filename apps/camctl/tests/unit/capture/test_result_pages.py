"""同一实际结果供实时与历史解码；扫描结束不代表集合确定。"""
from dataclasses import replace

import pytest

from camctl.capture import result_inputs
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import AttemptTicket, CallOutcome, AttemptStatus, ErrorValue

_TICKET = AttemptTicket(1, "result", "71", "results/71", 9)
_BINDING = DeviceBinding("cam-1", "dji-action6")
_CURSOR = DirectoryCursor(_BINDING, ("/DCIM",), 0, "/DCIM/a.mp4")
_ENTRY = {"identity": "/DCIM/a.mp4", "locator": {"path": "/DCIM/a.mp4"},
          "complete": True, "size_bytes": 50, "kind": "video", "format_id": "mp4"}


def _cursor(cursor):
    if cursor is None:
        return None
    return {"device_id": cursor.binding.device_id, "driver_id": cursor.binding.driver_id,
            "directories": list(cursor.directories), "directory_index": cursor.directory_index,
            "after_path": cursor.after_path}


def _outcome(*, entries=(), cursor=None, next_cursor=None, finalized=False, completion=None):
    return CallOutcome(observations=(DeviceObservation("result_files_listed", 2, {
        "activity_id": "71", "entries": list(entries), "cursor": _cursor(cursor),
        "next_cursor": _cursor(next_cursor), "set_finalized": finalized,
        "completion_evidence": completion}),))


def _page(outcome, **kwargs):
    return result_inputs.page_from_outcome(_TICKET, outcome, **kwargs)


def test_legacy_v1_has_no_set_finality():
    outcome = CallOutcome(observations=(DeviceObservation("result_files_listed", 1, {"activity_id": "71", "entries": [_ENTRY]}),))
    page = _page(outcome)
    assert page.next_cursor is None and not page.set_finalized and page.completion_evidence is None
    assert page.entries[0].identity == "/DCIM/a.mp4"


def test_empty_middle_page_keeps_explicit_next_cursor():
    page = _page(_outcome(next_cursor=_CURSOR))
    assert page.entries == () and page.next_cursor == _CURSOR and not page.set_finalized


def test_last_nonempty_page_does_not_derive_finality():
    page = _page(_outcome(entries=[_ENTRY]))
    assert len(page.entries) == 1 and page.next_cursor is None and not page.set_finalized


def test_finalized_empty_page_is_a_real_empty_set():
    page = _page(_outcome(finalized=True))
    assert page.entries == () and page.set_finalized and page.next_cursor is None


@pytest.mark.parametrize("member,value", [
    ("activity_id", "72"), ("activity_id", "071"), ("extra", True),
    ("entries", None), ("set_finalized", 1), ("completion_evidence", {}),
])
def test_invalid_identity_or_registered_members_are_rejected(member, value):
    outcome = _outcome()
    outcome.observations[0].data[member] = value
    with pytest.raises((ValueError, result_inputs.ConsistencyError)):
        _page(outcome)


def test_unknown_version_is_rejected():
    outcome = _outcome()
    outcome = replace(outcome, observations=(replace(outcome.observations[0], version=3),))
    with pytest.raises((ValueError, result_inputs.ConsistencyError)):
        _page(outcome)


def test_finality_cannot_be_set_before_scan_end():
    with pytest.raises((ValueError, result_inputs.ConsistencyError)):
        _page(_outcome(next_cursor=_CURSOR, finalized=True))


def test_page_must_respond_to_original_cursor():
    with pytest.raises((ValueError, result_inputs.ConsistencyError)):
        _page(_outcome(), cursor=_CURSOR)


def test_returned_cursor_binding_cannot_change():
    outcome = _outcome(next_cursor=_CURSOR)
    outcome.observations[0].data["next_cursor"]["device_id"] = "other"
    with pytest.raises((ValueError, result_inputs.ConsistencyError)):
        _page(outcome, binding=_BINDING)


def test_error_without_observation_is_not_a_real_empty_scan():
    actual = CallOutcome(status=AttemptStatus.FAILED, error=ErrorValue("read_failed", "device", {"offset": 5}))
    page = _page(actual)
    assert page.outcome is actual and page.entries == () and not page.set_finalized
    assert page.outcome.error.details == {"offset": 5}


def test_completion_is_independent_from_finality():
    completed = {"type": "capture_completed", "version": 1, "data": {"activity_id": "71"}}
    page = _page(_outcome(completion=completed))
    assert page.completion_evidence == DeviceObservation("capture_completed", 1, {"activity_id": "71"})
    assert not page.set_finalized


def test_partial_error_keeps_files_and_full_actual_result():
    actual = replace(_outcome(entries=[_ENTRY], finalized=True), status=AttemptStatus.FAILED,
                     error=ErrorValue("read_failed", "device", {"received": 50}))
    page = _page(actual)
    assert page.outcome is actual and page.entries[0].identity == "/DCIM/a.mp4"
    assert page.set_finalized and not page.scan_complete
