"""普通录像的真实目录页、元数据和停止后完成等待。"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from pathlib import PurePosixPath

from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.devices.evidence import DeviceObservation, RESULT_PAGE_CONTRACT
from camctl.devices.ports import DeviceCallResult
from camctl.devices.recording_completion import FileCompletionMethod, is_completed_file_wait
from camctl.operations.models import (AttemptStatus, CallInfo, CallOutcome, EffectState,
    ErrorValue, EvidenceValue, Settlement, SettlementBasis)
from camctl.operations.owned_calls import current_call_scope
from camctl.operations.result_format import read_result_document


class _NeverStop:
    async def requested(self):
        await asyncio.Future()


def _completion_context(request):
    context = request.params.get("completion_context")
    if context is None:
        return None
    if (not isinstance(context, Mapping) or set(context) != {
            "activity_id", "stop_result_event_id", "stop_returned_at_us", "stop_response",
            "file_completion_wait_ms", "prior_completion"}
            or context["activity_id"] != request.ticket.target_id
            or type(context["stop_result_event_id"]) is not int or context["stop_result_event_id"] <= 0
            or type(context["stop_returned_at_us"]) is not int
            or type(context["file_completion_wait_ms"]) is not int or context["file_completion_wait_ms"] <= 0):
        raise ValueError("录像完成上下文缺少原活动、停止结果或固定等待规则")
    response = context["stop_response"]
    if not isinstance(response, Mapping) or set(response) != {"status", "effect_state", "error", "result"}:
        raise ValueError("录像完成上下文缺少完整原 STOP 结果")
    actual = read_result_document(response["status"], response["effect_state"], response["result"], response["error"])
    if (actual.effect is not EffectState.CONFIRMED or not any(
            observation.type == "stop_confirmed" and observation.data.get("activity_id") == request.ticket.target_id
            for observation in actual.observations)):
        raise ValueError("录像完成上下文没有原活动的可靠停止确认")
    prior = context["prior_completion"]
    if prior is not None and (not isinstance(prior, Mapping) or set(prior) != {"result_page_event_id", "evidence"}
            or type(prior["result_page_event_id"]) is not int or prior["result_page_event_id"] <= 0
            or not is_completed_file_wait(prior["evidence"], activity_id=context["activity_id"],
                stop_result_event_id=context["stop_result_event_id"],
                required_wait_ms=context["file_completion_wait_ms"])):
        raise ValueError("原完成等待与活动、停止结果或固定规则不符")
    return deepcopy(context)


def _call_data(raw, kind):
    result = {"kind": kind}
    if raw is not None:
        result.update(transport_error=raw.error, output_failure=raw.output_failure)
        if raw.exit is not None:
            result.update(local_exit_code=raw.exit.exit_code, local_signal=raw.exit.signal)
        if raw.stderr is not None:
            result["stderr_hex"] = raw.stderr.hex()
    return result


def _entry(identity, size, complete):
    return {"identity": identity.path, "locator": {"path": identity.path}, "complete": complete,
            "size_bytes": size, "kind": "video", "format_id": "mp4",
            "original_name": PurePosixPath(identity.path).name, "media_type": None,
            "paired_identity": None}


async def list_record_results(driver, request, batch):
    """单页共享有限期限；原停止事实与完整目录扫描分别判断。"""
    if request.params.get("activity_id") != request.ticket.target_id:
        raise ValueError("录像结果请求改变原活动")
    scope = request.params.get("output_scope")
    if not isinstance(scope, Mapping) or scope != {"directories": ["/mnt/media_rw/emulated/DCIM"]}:
        raise ValueError("当前录像结果要求固定的内部存储范围")
    context = _completion_context(request)
    cursor = (None if request.params.get("cursor") is None
              else DirectoryCursor.from_json(request.params["cursor"]))
    began = driver.monotonic_ns()
    calls, completion, completion_source = [], None, None
    raw = None

    def remaining():
        return request.timeout_s - Decimal(driver.monotonic_ns() - began) / Decimal(1_000_000_000)

    def reply(error=None, page=None):
        observations = () if page is None else (DeviceObservation(
            RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version, page),)
        call_info = None
        if raw is not None and raw.exit is not None:
            call_info = (CallInfo(local_exit_code=raw.exit.exit_code) if raw.exit.exit_code is not None
                         else CallInfo(local_signal=raw.exit.signal))
        outcome = CallOutcome(status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
            error=error, effect=EffectState.CONFIRMED if observations else EffectState.UNKNOWN,
            observations=observations, call_info=call_info,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("result_returned", 1,
                {"file_completion": completion,
                 "file_completion_source_page_event_id": completion_source, "calls": calls})))
        return DeviceCallResult.from_outcome(outcome)

    def waiting_error(reason):
        return ErrorValue("action6_file_completion_unconfirmed", "device_file_completion", {"reason": reason})

    if context is not None:
        if context["prior_completion"] is not None:
            completion = deepcopy(context["prior_completion"]["evidence"])
            completion_source = context["prior_completion"]["result_page_event_id"]
        else:
            wait_ms = context["file_completion_wait_ms"]
            completion = {"method": FileCompletionMethod.STOP_RETURN_AND_WAIT, "version": 1,
                "activity_id": context["activity_id"], "stop_result_event_id": context["stop_result_event_id"],
                "required_wait_ms": wait_ms, "observed_wait_ns": 0, "completed": False}
            if remaining() <= Decimal(wait_ms) / 1000:
                return reply(waiting_error("insufficient_deadline"))
            started = driver.monotonic_ns()
            try:
                await asyncio.sleep(wait_ms / 1000)
            except asyncio.CancelledError as error:
                completion["observed_wait_ns"] = max(0, driver.monotonic_ns() - started)
                owned = current_call_scope()
                if owned is not None:
                    owned.interrupted = error
                return reply(waiting_error("wait_interrupted"))
            completion["observed_wait_ns"] = driver.monotonic_ns() - started
            completion["completed"] = completion["observed_wait_ns"] >= wait_ms * 1_000_000
            if not completion["completed"] or remaining() <= 0:
                return reply(waiting_error("wait_not_completed" if not completion["completed"] else "deadline_elapsed"))

    filesystem = driver._filesystem(request.binding)
    directory = DirectoryRequest(request.binding, tuple(scope["directories"]), cursor, min(batch, 32), remaining())
    listing = await filesystem.read_directory(directory, stop=_NeverStop())
    raw = listing.outcome
    calls.append(_call_data(raw, "directory"))
    if listing.error is not None:
        return reply(listing.error)
    page = {"activity_id": request.ticket.target_id, "entries": [],
            "cursor": None if cursor is None else cursor.as_json(),
            "next_cursor": None if listing.page.next_cursor is None else listing.page.next_cursor.as_json(),
            "set_finalized": False, "completion_evidence": None}
    complete = completion is not None and completion["completed"] is True
    for identity in listing.page.items:
        if PurePosixPath(identity.path).suffix.lower() != ".mp4":
            continue
        owned = current_call_scope()
        if owned is not None and owned.interrupted is not None:
            return reply(waiting_error("call_interrupted"), page)
        if remaining() <= 0:
            return reply(ErrorValue("adb_metadata_failed", "device_file_access", {"reason": "deadline_elapsed"}), page)
        metadata = await filesystem.metadata(identity, timeout_s=remaining(), stop=_NeverStop())
        raw = metadata.outcome
        calls.append(_call_data(raw, "metadata"))
        if metadata.error is not None:
            page["entries"].append(_entry(identity, None, False))
            return reply(metadata.error, page)
        page["entries"].append(_entry(identity, metadata.value, complete))
    page["set_finalized"] = complete and listing.page.next_cursor is None
    return reply(page=page)
