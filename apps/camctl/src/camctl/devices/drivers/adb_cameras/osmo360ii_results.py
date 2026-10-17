"""OSMO 原片分页与有界第二阶段预览关联，不推定设备完成。"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from pathlib import PurePosixPath

from camctl.devices.capture_completion import validate_completion_context, is_stop_context
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.devices.evidence import DeviceObservation, RESULT_PAGE_CONTRACT
from camctl.devices.ports import DeviceCallResult
from camctl.devices.recording_completion import FileCompletionMethod
from camctl.operations.models import (AttemptStatus, CallInfo, CallOutcome, EffectState,
    ErrorValue, EvidenceValue, Settlement, SettlementBasis)
from camctl.operations.owned_calls import current_call_scope
from .record_results import _NeverStop, _call_data


def _entry(identity, size, complete, *, paired=None):
    path = PurePosixPath(identity.path)
    preview = path.suffix.lower() == ".lrf"
    return {"identity": identity.path, "locator": {"path": identity.path}, "complete": complete,
        "size_bytes": size, "kind": "video",
        "format_id": "lrf" if preview else "osv", "original_name": path.name,
        "media_type": None, "paired_identity": paired}


def _stem(path):
    # UTF-8 原字节比较，不对名称做大小写或 Unicode 归一化。
    return str(PurePosixPath(path).with_suffix("")).encode("utf-8")


class _ResultRead:
    def __init__(self, driver, request, batch):
        self.driver, self.request = driver, request
        self.batch = min(batch, 32)
        self.began = driver.monotonic_ns()
        self.calls, self.call_count, self.previews = [], 0, []
        self.raw, self.completion, self.completion_source = None, None, None
        self.conflict = None

    def remaining(self):
        return self.request.timeout_s - Decimal(self.driver.monotonic_ns() - self.began) / Decimal(1_000_000_000)

    def available(self):
        owned = current_call_scope()
        return self.remaining() > 0 and (owned is None or owned.interrupted is None)

    def remember(self, raw, kind):
        self.raw = raw
        self.call_count += 1
        if len(self.calls) < 128:
            self.calls.append(_call_data(raw, kind))

    def reply(self, error=None, page=None):
        observations = () if page is None else (DeviceObservation(
            RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version, page),)
        call_info = None
        if self.raw is not None and self.raw.exit is not None:
            call_info = (CallInfo(local_exit_code=self.raw.exit.exit_code) if self.raw.exit.exit_code is not None
                         else CallInfo(local_signal=self.raw.exit.signal))
        outcome = CallOutcome(status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
            error=error, effect=EffectState.CONFIRMED if observations else EffectState.UNKNOWN,
            observations=observations, call_info=call_info,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("result_returned", 1,
                {"file_completion": self.completion,
                 "file_completion_source_page_event_id": self.completion_source,
                 "calls": self.calls, "call_count": self.call_count,
                 "optional_preview_observations": self.previews})))
        return DeviceCallResult.from_outcome(outcome)

    @staticmethod
    def wait_error(reason):
        return ErrorValue("osmo360ii_file_completion_unconfirmed", "device_file_completion", {"reason": reason})

    async def wait(self, context):
        if context["prior_completion"] is not None:
            self.completion = deepcopy(context["prior_completion"]["evidence"])
            self.completion_source = context["prior_completion"]["result_page_event_id"]
            return None
        wait_ms = context["file_completion_wait_ms"]
        self.completion = {"method": FileCompletionMethod.STOP_RETURN_AND_WAIT, "version": 1,
            "activity_id": context["activity_id"], "stop_result_event_id": context["stop_result_event_id"],
            "required_wait_ms": wait_ms, "observed_wait_ns": 0, "completed": False}
        if self.remaining() <= Decimal(wait_ms) / 1000:
            return self.wait_error("insufficient_deadline")
        began = self.driver.monotonic_ns()
        try:
            await asyncio.sleep(wait_ms / 1000)
        except asyncio.CancelledError as error:
            self.completion["observed_wait_ns"] = max(0, self.driver.monotonic_ns() - began)
            owned = current_call_scope()
            if owned is not None:
                owned.interrupted = error
            return self.wait_error("wait_interrupted")
        self.completion["observed_wait_ns"] = self.driver.monotonic_ns() - began
        self.completion["completed"] = self.completion["observed_wait_ns"] >= wait_ms * 1_000_000
        if not self.completion["completed"] or not self.available():
            return self.wait_error("wait_not_completed" if not self.completion["completed"] else "deadline_elapsed")
        return None

    async def preview(self, filesystem, scope, preview, complete, observation, observed_originals):
        observation.update({"original_identity": None, "state": "completion_unknown",
            "osv_count": None, "lrf_count": None, "preview_identity": None, "size_bytes": None,
            "scan_complete": False, "observed_original_identities": [], "observed_preview_identities": []})
        observation["preview_identity"] = preview.path
        if not complete:
            return None
        cursor, original_candidate, preview_candidate, originals, previews = None, None, None, 0, 0
        while True:
            if not self.available():
                observation["state"] = "scan_unavailable"
                return None
            listing = await filesystem.read_directory(DirectoryRequest(self.request.binding,
                tuple(scope["directories"]), cursor, self.batch, self.remaining()), stop=_NeverStop())
            self.remember(listing.outcome, "preview_directory")
            if listing.error is not None:
                observation.update(state="scan_failed", error_code=listing.error.code)
                return None
            for identity in listing.page.items:
                suffix = PurePosixPath(identity.path).suffix.lower()
                if suffix not in (".osv", ".lrf") or _stem(identity.path) != _stem(preview.path):
                    continue
                if suffix == ".osv":
                    originals += 1
                    original_candidate = identity if originals == 1 else None
                    if len(observation["observed_original_identities"]) < 2:
                        observation["observed_original_identities"].append(identity.path)
                else:
                    previews += 1
                    if len(observation["observed_preview_identities"]) < 2:
                        observation["observed_preview_identities"].append(identity.path)
                    preview_candidate = identity if previews == 1 else None
            cursor = listing.page.next_cursor
            # 只保留计数及至多一个候选，不保留已扫描的目录条目。
            del listing
            if cursor is None:
                break
        observation.update(osv_count=originals, lrf_count=previews, scan_complete=True)
        if originals == 0:
            original = next((item for item in observed_originals if _stem(item.path) == _stem(preview.path)), None)
            if original is None:
                observation["state"] = "missing_pair"
            else:
                observation["state"] = "original_missing"
                self.conflict = ErrorValue("osmo360ii_result_conflict", "device_file_completion",
                    {"reason": "original_missing", "identity": original.path})
            return None
        if originals > 1 or previews > 1:
            observation["state"] = "ambiguous"
            return None
        if previews == 0 or preview_candidate.path != preview.path:
            observation["state"] = "preview_missing"
            return None
        observation["original_identity"] = original_candidate.path
        if not self.available():
            observation["state"] = "metadata_unavailable"
            return None
        metadata = await filesystem.metadata(preview, timeout_s=self.remaining(), stop=_NeverStop())
        self.remember(metadata.outcome, "preview_metadata")
        if metadata.error is not None:
            observation.update(state="metadata_failed", error_code=metadata.error.code)
            return None
        observation["size_bytes"] = metadata.value
        if metadata.value <= 0:
            observation["state"] = "zero_size"
            return None
        observation["state"] = "paired"
        return _entry(preview, metadata.value, True, paired=original_candidate.path)


async def list_osmo360ii_results(driver, request, batch):
    """主扫描保存原片事实，预览关联失败独立保留为可选事实。"""
    if request.params.get("activity_id") != request.ticket.target_id:
        raise ValueError("OSMO 结果请求改变原活动")
    scope = request.params.get("output_scope")
    if not isinstance(scope, Mapping) or scope != {"directories": ["/mnt/media_rw/emulated/DCIM"]}:
        raise ValueError("OSMO 结果要求固定的内部存储范围")
    context = validate_completion_context(request.params.get("completion_context"), activity_id=request.ticket.target_id)
    cursor = None if request.params.get("cursor") is None else DirectoryCursor.from_json(request.params["cursor"])
    if cursor is not None and (cursor.binding != request.binding or cursor.directories != tuple(scope["directories"])):
        raise ValueError("OSMO 结果游标改变原绑定或完整范围")
    read = _ResultRead(driver, request, batch)
    if context is not None and is_stop_context(context):
        error = await read.wait(context)
        if error is not None:
            return read.reply(error)
    complete = context is not None
    filesystem = driver._filesystem(request.binding)
    if not read.available():
        return read.reply(read.wait_error("call_unavailable"))
    listing = await filesystem.read_directory(DirectoryRequest(request.binding,
        tuple(scope["directories"]), cursor, read.batch, read.remaining()), stop=_NeverStop())
    read.remember(listing.outcome, "directory")
    if listing.error is not None:
        return read.reply(listing.error)
    page = {"activity_id": request.ticket.target_id, "entries": [],
        "cursor": None if cursor is None else cursor.as_json(),
        "next_cursor": None if listing.page.next_cursor is None else listing.page.next_cursor.as_json(),
        "set_finalized": False, "completion_evidence": None}
    originals, preview_inputs, original_observations = [], [], {}
    for identity in listing.page.items:
        suffix = PurePosixPath(identity.path).suffix.lower()
        if suffix == ".lrf":
            observation = {"preview_identity": identity.path, "state": "not_checked"}
            read.previews.append(observation)
            preview_inputs.append((identity, observation))
        elif suffix == ".osv":
            observation = {"original_identity": identity.path,
                "state": "completion_unknown" if not complete else "not_observed_on_page"}
            read.previews.append(observation)
            original_observations[identity.path] = observation
    for identity in listing.page.items:
        suffix = PurePosixPath(identity.path).suffix.lower()
        if suffix != ".osv":
            continue
        if not read.available():
            return read.reply(ErrorValue("adb_metadata_failed", "device_file_access", {"reason": "call_unavailable"}), page)
        metadata = await filesystem.metadata(identity, timeout_s=read.remaining(), stop=_NeverStop())
        read.remember(metadata.outcome, "metadata")
        page["entries"].append(_entry(identity, metadata.value if metadata.error is None else None,
                                      complete if metadata.error is None else False))
        if metadata.error is not None:
            return read.reply(metadata.error, page)
        originals.append(identity)
    # 必要 OSV 元数据先全部取得。预览在它自己的原目录页返回；
    # 第二阶段扫描只提供配对身份。
    entries = {entry["identity"]: entry for entry in page["entries"]}
    for identity, observation in preview_inputs:
        preview = await read.preview(filesystem, scope, identity, complete, observation, originals)
        for original in originals:
            if _stem(original.path) == _stem(identity.path):
                original_observations[original.path].update(observation, original_identity=original.path)
        if preview is not None:
            entries[identity.path] = preview
        if read.conflict is not None:
            break
    if complete and cursor is None and listing.page.next_cursor is None:
        # 只有单页覆盖整个范围时，本页未出现预览才能直接证明缺少预览。
        for observation in original_observations.values():
            if observation["state"] == "not_observed_on_page":
                observation.update(state="missing", scan_complete=True)
    page["entries"] = [entries[identity.path] for identity in listing.page.items if identity.path in entries]
    page["set_finalized"] = read.conflict is None and complete and listing.page.next_cursor is None
    return read.reply(read.conflict, page)
