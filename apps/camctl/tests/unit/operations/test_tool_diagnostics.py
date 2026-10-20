"""原双流诊断严格往返、有限约束和独立结果成员。"""
from dataclasses import replace

import pytest

from camctl.operations.result_format import result_document, read_result_document
from camctl.operations.validation import validate_outcome
from .test_results import _outcome, _TICKET, OPERATION_RETURNED, REGISTRY


def _diagnostics(stdout=b"\x00\xff\n", stderr=b"", state="complete", limit=2):
    from camctl.operations.tool_diagnostics import (
        ToolDiagnostics, ToolCallOutput, ToolStream, ToolStreamState,
    )
    stream = lambda data: ToolStream(ToolStreamState.UNAVAILABLE if data is None else ToolStreamState(state), data)
    return ToolDiagnostics(limit, (ToolCallOutput(1, "capture", stream(stdout), stream(stderr)),))


@pytest.mark.parametrize("stdout,stderr,state", [
    (b"\x00\xff\n", b"", "complete"), (b"", None, "unconfirmed"),
    (None, b"prefix", "unconfirmed"), (None, None, "unconfirmed"),
])
def test_diagnostics_round_trip_preserves_each_stream(stdout, stderr, state):
    original = replace(_outcome(), diagnostics=_diagnostics(stdout, stderr, state))
    document = result_document(original)
    assert document["diagnostics"]["version"] == 1
    assert document["diagnostics"]["calls"][0]["stdout"]["hex"] == (None if stdout is None else stdout.hex())
    assert "diagnostics" not in document["settlement"]
    decoded = read_result_document(2, 3, document, None)
    assert decoded == original


def test_diagnostics_not_applicable_is_omitted():
    assert "diagnostics" not in result_document(_outcome())


@pytest.mark.parametrize("hex_value", ["A0", "0", "00 01", "gg", 0])
def test_diagnostics_rejects_noncanonical_hex(hex_value):
    original = replace(_outcome(), diagnostics=_diagnostics())
    document = result_document(original)
    document["diagnostics"]["calls"][0]["stdout"]["hex"] = hex_value
    with pytest.raises(ValueError):
        read_result_document(2, 3, document, None)


@pytest.mark.parametrize("change", ["version", "unknown", "cap", "index", "state", "missing", "null"])
def test_diagnostics_rejects_invalid_frozen_structure(change):
    original = replace(_outcome(), diagnostics=_diagnostics())
    document = result_document(original)
    diag = document["diagnostics"]
    call = diag["calls"][0]
    if change == "version": diag["version"] = True
    elif change == "unknown": diag["error"] = "copied"
    elif change == "cap": diag["call_limit"] = 0
    elif change == "index": call["step_index"] = 2
    elif change == "state": call["stdout"]["state"] = "truncated"
    elif change == "missing": call["stdout"]["hex"] = None
    else: document["diagnostics"] = None
    with pytest.raises(ValueError):
        read_result_document(2, 3, document, None)


def test_diagnostics_new_result_checks_existing_registry_limit():
    outcome = replace(_outcome(), diagnostics=_diagnostics())
    with pytest.raises(ValueError):
        validate_outcome(_TICKET, outcome, REGISTRY)
    contract = replace(OPERATION_RETURNED, max_diagnostic_calls=2)
    registry = REGISTRY.with_contract(contract)
    assert validate_outcome(_TICKET, outcome, registry).outcome is outcome
    with pytest.raises(ValueError):
        validate_outcome(_TICKET, replace(outcome, diagnostics=_diagnostics(limit=3)), registry)


def test_diagnostics_limits_each_stream_before_hex_decode():
    from camctl.operations.process import OUTPUT_LIMIT_BYTES
    from camctl.operations.tool_diagnostics import decode_diagnostics
    original = replace(_outcome(), diagnostics=_diagnostics(stdout=b"x" * OUTPUT_LIMIT_BYTES))
    document = result_document(original)
    assert read_result_document(2, 3, document, None) == original
    document["diagnostics"]["calls"][0]["stdout"]["hex"] += "00"
    with pytest.raises(ValueError):
        decode_diagnostics(document["diagnostics"])


def test_diagnostics_typed_invalid_states_and_limits_are_rejected():
    from camctl.operations.tool_diagnostics import ToolDiagnostics, ToolCallOutput, ToolStream, ToolStreamState, validate_diagnostics
    for diagnostic in (
        ToolDiagnostics(True, ()), ToolDiagnostics(1, []),
        ToolDiagnostics(1, (ToolCallOutput(True, "capture", ToolStream(ToolStreamState.COMPLETE, b""), ToolStream(ToolStreamState.COMPLETE, b"")),)),
    ):
        with pytest.raises(ValueError):
            validate_diagnostics(diagnostic)


def test_diagnostics_rejects_half_confirmed_stream_pair():
    from camctl.operations.tool_diagnostics import validate_diagnostics
    with pytest.raises(ValueError):
        validate_diagnostics(_diagnostics(stdout=None, stderr=b"read", state="complete"))
