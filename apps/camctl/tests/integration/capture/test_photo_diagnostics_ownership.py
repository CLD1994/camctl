"""真实单张返回经 C1 保存或重送，诊断失败不能重放设备调用。"""
from dataclasses import replace
import json

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.devices.ports import DeviceCallResult
from camctl.logging_runtime.diagnostics import error_diagnostics_context
from camctl.operations import tool_diagnostics
from camctl.persistence.models import DbOutcomeKind

from ..support.photo_diagnostics_cases import (
    StartReceipts, actual_photo_world, advance_photo, assert_original_streams,
    start_outcome, start_row,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fault", ["index", "stream", "cap", "unregistered", "temporary"])
async def test_actual_return_validation_preserves_original_request(tmp_path, monkeypatch, fault):
    evidence = (EVIDENCE.with_contract(replace(
        EVIDENCE.contract("operation_returned", 1),
        type="operation_returned_without_diagnostics", max_diagnostic_calls=None))
        if fault == "unregistered" else EVIDENCE)
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(
        tmp_path, evidence=evidence)
    original_control = driver.control
    actual = None
    requests, checked, records = [], [], []
    checks = 0
    validate = tool_diagnostics.validate_diagnostics

    def failing(value):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise ValueError("一次临时诊断校验故障")
        return validate(value)

    async def returned(request):
        nonlocal actual
        requests.append(request)
        result = await original_control(request)
        diagnostics = result.outcome.diagnostics
        if fault == "cap":
            diagnostics = replace(diagnostics, call_limit=diagnostics.call_limit + 1)
        elif fault in ("index", "stream"):
            first = diagnostics.calls[0]
            first = (replace(first, step_index=True) if fault == "index" else
                     replace(first, stdout=replace(first.stdout, state="complete")))
            diagnostics = replace(diagnostics, calls=(first, *diagnostics.calls[1:]))
        actual = replace(result.outcome, diagnostics=diagnostics)
        if fault == "unregistered":
            actual = replace(actual, settlement=replace(actual.settlement, evidence=replace(
                actual.settlement.evidence, type="operation_returned_without_diagnostics")))
        if fault == "temporary":
            monkeypatch.setattr(tool_diagnostics, "validate_diagnostics", failing)

        def check(original):
            checked.append(original)
            result.diagnostics_check(original)

        return DeviceCallResult.from_outcome(actual, diagnostics_check=check)

    monkeypatch.setattr(driver, "control", returned)
    receipts = StartReceipts(monkeypatch, unknown="before" if fault == "temporary" else None)
    occurred_at = clock[0]
    try:
        with error_diagnostics_context(None, records.append):
            with pytest.raises(ConsistencyError):
                await advance_photo(runtime)
            first = start_row(owned)
            assert (first["result_json"], first["error_json"]) == (None, None)
            assert checked == [actual]
            assert_original_streams(actual, transport, owned)
            clock[0] += 99
            if fault == "temporary":
                assert checks >= 1
                with pytest.raises(ConsistencyError):
                    await advance_photo(runtime)
                assert len(receipts.calls) == 2
                assert all(call.receipt.kind is DbOutcomeKind.UNKNOWN for call in receipts.calls)
                assert start_row(owned)["result_json"] is None
                await advance_photo(runtime)
                assert checks >= 3
                receipts.assert_same_original(actual, occurred_at=occurred_at)
                assert receipts.calls[0].finish.ticket == requests[0].ticket
                assert start_outcome(owned) == actual
                assert start_row(owned)["occurred_at"] == occurred_at
            else:
                with pytest.raises(ConsistencyError):
                    await advance_photo(runtime)
                assert start_row(owned) == first
                assert not receipts.calls
            assert checked == [actual, actual]
        assert len(requests) == 1
        assert len(transport.calls) == 9
        document, = [json.loads(line) for line in records]
        assert document["level"] == "ERROR"
        diagnostic = document.get("diagnostic", document)
        assert diagnostic["stage"] == "capture.result_validation"
        assert diagnostic["context"]["ticket"]["run_id"] == requests[0].ticket.run_id
        assert "hb diagnostic" in json.dumps(document)
        assert b"hb diagnostic\xff\n".hex() in json.dumps(document)
    finally:
        owned.connection.close()


async def test_driver_prefix_failure_hands_original_bytes_to_owner(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    control = driver.control
    entered, records = [], []

    async def returned(request):
        response = await control(request)
        diagnostics = response.outcome.diagnostics
        actual = replace(response.outcome, diagnostics=replace(diagnostics, calls=tuple(
            replace(call, step_name="wrong-prefix") for call in diagnostics.calls)))

        def check(original):
            entered.append(original)
            assert start_row(owned)["result_json"] is None
            response.diagnostics_check(original)
        return DeviceCallResult.from_outcome(actual, diagnostics_check=check)

    monkeypatch.setattr(driver, "control", returned)
    try:
        with error_diagnostics_context(None, records.append):
            for _ in range(2):
                with pytest.raises(ConsistencyError, match="诊断校验失败"):
                    await advance_photo(runtime)
        actual = entered[0]
        assert entered == [actual, actual]
        assert actual.observations[0].type == "photo_taken"
        assert_original_streams(actual, transport, owned)
        assert start_row(owned)["result_json"] is None
        assert start_row(owned)["error_json"] is None
        document, = [json.loads(line) for line in records]
        assert document["level"] == "ERROR"
        assert "wrong-prefix" in json.dumps(document)
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_driver_diagnostic_validation_only_runs_after_owner_holds_result(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    checked, records = [], []
    control = driver.control
    actual = None

    async def returned(request):
        nonlocal actual
        result = await control(request)
        actual = result.outcome

        def failing(original):
            assert actual is original
            checked.append(original.diagnostics)
            raise ValueError("拥有者取得原结果后的一次校验故障")

        return DeviceCallResult.from_outcome(actual, diagnostics_check=failing)

    monkeypatch.setattr(driver, "control", returned)
    try:
        with error_diagnostics_context(None, records.append):
            with pytest.raises(ConsistencyError):
                await advance_photo(runtime)
            assert checked == [actual.diagnostics]
            with pytest.raises(ConsistencyError):
                await advance_photo(runtime)
        assert checked == [actual.diagnostics, actual.diagnostics]
        assert len(records) == 1
        assert json.loads(records[0])["level"] == "ERROR"
        assert_original_streams(actual, transport, owned)
        assert start_row(owned)["result_json"] is None
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_general_invalid_observation_keeps_existing_rejection(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    original_control = driver.control

    async def returned(request):
        result = await original_control(request)
        observation = result.outcome.observations[0]
        actual = replace(result.outcome, observations=(replace(observation, type="unregistered"),))
        return DeviceCallResult.from_outcome(actual, diagnostics_check=result.diagnostics_check)

    monkeypatch.setattr(driver, "control", returned)
    try:
        await advance_photo(runtime)
        saved = start_row(owned)
        assert json.loads(saved["result_json"])["observations"] == []
        assert json.loads(saved["error_json"])["code"] == "invalid_device_result"
        await advance_photo(runtime)
        assert start_row(owned) == saved
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_temporary_frozen_prefix_check_resumes_same_original_without_replanning(tmp_path, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as module
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    control = driver.control
    checked = []

    async def returned(request):
        response = await control(request)
        def check(original):
            checked.append(original)
            if len(checked) == 1:
                raise RuntimeError("本次冻结前缀检查的一次临时故障")
            response.diagnostics_check(original)
        return DeviceCallResult.from_outcome(response.outcome, diagnostics_check=check)

    monkeypatch.setattr(driver, "control", returned)
    receipts = StartReceipts(monkeypatch, unknown="after")
    occurred_at = clock[0]
    try:
        with pytest.raises(ConsistencyError):
            await advance_photo(runtime)
        actual, = checked

        def forbidden(_settings):
            raise AssertionError("原申请接手不得重新规划命令")

        monkeypatch.setattr(module, "photo_start_commands", forbidden)
        clock[0] += 99
        with pytest.raises(ConsistencyError):
            await advance_photo(runtime)
        committed = start_row(owned)
        assert start_outcome(owned) == actual
        assert len(receipts.calls) == 2
        assert all(call.receipt.kind is DbOutcomeKind.UNKNOWN for call in receipts.calls)
        await advance_photo(runtime)
        receipts.assert_same_original(actual, occurred_at=occurred_at)
        assert start_row(owned) == committed
        assert checked == [actual, actual]
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_actual_failed_capture_error_survives_bad_diagnostic_index(tmp_path, monkeypatch):
    from camctl.operations.models import AttemptStatus, EffectState
    from camctl.operations.process import LocalExit
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    run = transport.run

    async def failed_capture(spec, stop):
        raw = await run(spec, stop)
        if spec.argv[3] == "capture":
            raw = replace(raw, exit=LocalExit(exit_code=7))
            transport.outcomes[-1] = raw
        return raw

    monkeypatch.setattr(transport, "run", failed_capture)
    control = driver.control
    actual = None

    async def returned(request):
        nonlocal actual
        result = await control(request)
        diagnostic = result.outcome.diagnostics
        actual = replace(result.outcome, diagnostics=replace(diagnostic, calls=(
            replace(diagnostic.calls[0], step_index=True), *diagnostic.calls[1:])))
        return DeviceCallResult.from_outcome(actual, diagnostics_check=result.diagnostics_check)

    monkeypatch.setattr(driver, "control", returned)
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            with pytest.raises(ConsistencyError):
                await advance_photo(runtime)
        assert actual.status is AttemptStatus.FAILED
        assert actual.effect is EffectState.UNKNOWN
        assert actual.error.code == "device_error"
        assert actual.error.details["step"] == "capture"
        assert actual.call_info.local_exit_code == 7
        assert_original_streams(actual, transport, owned)
        assert (start_row(owned)["result_json"], start_row(owned)["error_json"]) == (None, None)
        document, = [json.loads(line) for line in records]
        assert document["level"] == "ERROR"
        assert "device_error" in json.dumps(document)
        assert "capture" in json.dumps(document)
        assert "hb diagnostic" in json.dumps(document)
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()
