"""按声明格式读取有限结果页，并把可选配对扫描与主游标分开。"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath

from camctl.devices.capture_completion import validate_completion_context, is_stop_context
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.devices.evidence import DeviceObservation, RESULT_PAGE_CONTRACT
from camctl.devices.ports import DeviceCallResult
from camctl.devices.recording_completion import FileCompletionMethod
from camctl.operations.models import (AttemptStatus, CallInfo, CallOutcome, EffectState,
    ErrorValue, EvidenceValue, Settlement, SettlementBasis)
from camctl.operations.owned_calls import current_call_scope
from camctl.logging_runtime.performance import record_performance
from .record_results import _NeverStop, _call_data


INTERNAL_DCIM = "/mnt/media_rw/emulated/DCIM"


@dataclass(frozen=True)
class ResultFormat:
    """一个结果页可识别的文件后缀及其登记分类。"""

    suffix: str
    kind: str
    format_id: str


class PreviewReadMode(Enum):
    """结果读取器如何处理页内预览文件。"""

    PAIR_FROM_SCOPE = "pair_from_scope"
    RAW_PAGE_FILES = "raw_page_files"


@dataclass(frozen=True)
class ResultProfile:
    """由驱动格式声明构成的有限读取行为。"""

    evidence_version: int
    formats: tuple[ResultFormat, ...]
    preview_format: ResultFormat
    paired_original_format_id: str
    original_count_field: str
    preview_count_field: str
    completion_error_code: str
    conflict_error_code: str
    preview_read_mode: PreviewReadMode = PreviewReadMode.PAIR_FROM_SCOPE


def _stem(path):
    """保留目录与 stem 的 UTF-8 原字节；不折叠大小写或 Unicode。"""
    return str(PurePosixPath(path).with_suffix("")).encode("utf-8")


def _entry(identity, size, complete, file_format, *, paired=None):
    return {"identity": identity.path, "locator": {"path": identity.path}, "complete": complete,
        "size_bytes": size, "kind": file_format.kind, "format_id": file_format.format_id,
        "original_name": PurePosixPath(identity.path).name, "media_type": None,
        "paired_identity": paired}


class _BoundedRead:
    def __init__(self, driver, request, batch, profile):
        self.driver, self.request, self.profile = driver, request, profile
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
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("result_returned",
                self.profile.evidence_version, {"file_completion": self.completion,
                    "file_completion_source_page_event_id": self.completion_source,
                    "calls": self.calls, "call_count": self.call_count,
                    "optional_preview_observations": self.previews})))
        return DeviceCallResult.from_outcome(outcome)

    def wait_error(self, reason):
        return ErrorValue(self.profile.completion_error_code, "device_file_completion", {"reason": reason})

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
            record_performance("file_completion_wait", self.completion["observed_wait_ns"],
                device_id=self.request.binding.device_id, activity_id=context["activity_id"], completed=False)
            owned = current_call_scope()
            if owned is not None:
                owned.interrupted = error
            return self.wait_error("wait_interrupted")
        self.completion["observed_wait_ns"] = self.driver.monotonic_ns() - began
        self.completion["completed"] = self.completion["observed_wait_ns"] >= wait_ms * 1_000_000
        record_performance("file_completion_wait", self.completion["observed_wait_ns"],
            device_id=self.request.binding.device_id, activity_id=context["activity_id"],
            completed=self.completion["completed"])
        if not self.completion["completed"] or not self.available():
            reason = "wait_not_completed" if not self.completion["completed"] else "deadline_elapsed"
            return self.wait_error(reason)
        return None

    async def scan_preview(self, filesystem, scope, preview, complete, observation, observed_originals):
        profile = self.profile
        preview_suffix = profile.preview_format.suffix.casefold()
        original_suffixes = frozenset(item.suffix.casefold() for item in profile.formats
            if item.format_id == profile.paired_original_format_id)
        observation.update({"original_identity": None, "state": "completion_unknown",
            profile.original_count_field: None, profile.preview_count_field: None,
            "preview_identity": preview.path, "size_bytes": None, "scan_complete": False,
            "observed_original_identities": [], "observed_preview_identities": []})
        if not complete:
            return None
        original_count = preview_count = 0
        original_candidate = preview_candidate = None
        scan_cursor = None
        while True:
            if not self.available():
                observation["state"] = "scan_unavailable"
                return None
            listing = await filesystem.read_directory(DirectoryRequest(self.request.binding,
                tuple(scope["directories"]), scan_cursor, self.batch, self.remaining()), stop=_NeverStop())
            self.remember(listing.outcome, "preview_directory")
            if listing.error is not None:
                observation.update(state="scan_failed", error_code=listing.error.code)
                return None
            for identity in listing.page.items:
                suffix = PurePosixPath(identity.path).suffix.casefold()
                if (suffix != preview_suffix and suffix not in original_suffixes
                        or _stem(identity.path) != _stem(preview.path)):
                    continue
                if suffix == preview_suffix:
                    preview_count += 1
                    if preview_count == 1:
                        preview_candidate = identity
                    if len(observation["observed_preview_identities"]) < 2:
                        observation["observed_preview_identities"].append(identity.path)
                else:
                    original_count += 1
                    if original_count == 1:
                        original_candidate = identity
                    if len(observation["observed_original_identities"]) < 2:
                        observation["observed_original_identities"].append(identity.path)
            scan_cursor = listing.page.next_cursor
            del listing
            if scan_cursor is None:
                break
        observation[profile.original_count_field] = original_count
        observation[profile.preview_count_field] = preview_count
        observation["scan_complete"] = True
        if original_count == 0:
            original = next((item for item in observed_originals if _stem(item.path) == _stem(preview.path)), None)
            if original is not None:
                observation.update(state="original_missing", original_identity=original.path)
                self.conflict = ErrorValue(profile.conflict_error_code, "device_file_completion",
                    {"reason": "original_missing", "identity": original.path})
                return None
            if self.request.params.get("cursor") is not None:
                history = self.request.prior_result_entries
                if history is None:
                    observation["state"] = "history_unavailable"
                    self.conflict = ErrorValue(profile.completion_error_code, "device_file_completion",
                        {"reason": "history_unavailable"})
                    return None
                matched_identity = None
                async for entry in history():
                    if not isinstance(entry, Mapping):
                        raise ValueError("前页结果历史必须是文件条目表")
                    identity = entry.get("identity")
                    format_id = entry.get("format_id")
                    if (not isinstance(identity, str) or not identity
                            or format_id is not None
                            and (not isinstance(format_id, str) or not format_id)):
                        raise ValueError("前页结果历史缺少稳定身份或格式分类")
                    if (matched_identity is None
                            and format_id == profile.paired_original_format_id
                            and _stem(identity) == _stem(preview.path)):
                        matched_identity = identity
                if matched_identity is not None:
                    observation.update(state="original_missing", original_identity=matched_identity)
                    self.conflict = ErrorValue(profile.conflict_error_code, "device_file_completion",
                        {"reason": "original_missing", "identity": matched_identity})
                    return None
            observation["state"] = "missing_pair"
            return None
        if original_count > 1 or preview_count > 1:
            observation["state"] = "ambiguous"
            return None
        if preview_count == 0 or preview_candidate.path != preview.path:
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
        return _entry(preview, metadata.value, True, profile.preview_format, paired=original_candidate.path)


async def list_declared_results(driver, request, batch, profile):
    """按结果声明读取一页；驱动选择页内原始观察或原有配对读取策略。"""
    if request.params.get("activity_id") != request.ticket.target_id:
        raise ValueError("结果请求改变原活动")
    scope = request.params.get("output_scope")
    if not isinstance(scope, Mapping) or scope != {"directories": [INTERNAL_DCIM]}:
        raise ValueError("结果请求要求固定的内部存储范围")
    context = validate_completion_context(request.params.get("completion_context"), activity_id=request.ticket.target_id)
    cursor = None if request.params.get("cursor") is None else DirectoryCursor.from_json(request.params["cursor"])
    if cursor is not None and (cursor.binding != request.binding or cursor.directories != tuple(scope["directories"])):
        raise ValueError("结果游标改变原绑定或完整范围")

    read = _BoundedRead(driver, request, batch, profile)
    if context is not None and is_stop_context(context):
        error = await read.wait(context)
        if error is not None:
            return read.reply(error)
    complete = context is not None
    filesystem = driver._filesystem(request.binding)
    if not read.available():
        return read.reply(read.wait_error("call_unavailable"))
    directory_request = DirectoryRequest(request.binding,
        tuple(scope["directories"]), cursor, read.batch, read.remaining())
    if profile.preview_read_mode is PreviewReadMode.RAW_PAGE_FILES and request.result_directory_reader is not None:
        listing = await request.result_directory_reader(filesystem, directory_request, stop=_NeverStop())
    else:
        listing = await filesystem.read_directory(directory_request, stop=_NeverStop())
    if listing.call_performed:
        read.remember(listing.outcome, "directory")
    if listing.error is not None:
        return read.reply(listing.error)
    page = {"activity_id": request.ticket.target_id, "entries": [],
        "cursor": None if cursor is None else cursor.as_json(),
        "next_cursor": None if listing.page.next_cursor is None else listing.page.next_cursor.as_json(),
        "set_finalized": False, "completion_evidence": None}

    by_suffix = {item.suffix.casefold(): item for item in profile.formats}
    if profile.preview_read_mode is PreviewReadMode.RAW_PAGE_FILES:
        page_formats = {**by_suffix, profile.preview_format.suffix.casefold(): profile.preview_format}
        raw_files = tuple(
            (identity, file_format)
            for identity in listing.page.items
            if (file_format := page_formats.get(PurePosixPath(identity.path).suffix.casefold())) is not None
        )
        page_entries = [
            _entry(identity, None, False, file_format)
            for identity, file_format in raw_files
        ]
        page["entries"] = page_entries
        candidates = (tuple(identity for identity, _ in raw_files)
            if request.result_candidates is None else
            await request.result_candidates(tuple(identity for identity, _ in raw_files)))
        candidate_set = set(candidates)
        if (len(candidate_set) != len(candidates)
                or candidates != tuple(identity for identity, _ in raw_files if identity in candidate_set)):
            raise ValueError("结果候选筛选必须返回原页中保持次序的文件身份子集")
        metadata_files = tuple((index, identity, file_format)
            for index, (identity, file_format) in enumerate(raw_files) if identity in candidate_set)
        if metadata_files:
            if not read.available():
                error = ErrorValue("adb_metadata_failed", "device_file_access", {"reason": "call_unavailable"})
                return read.reply(error, page)
            metadata = await filesystem.metadata_many(tuple(identity for _, identity, _ in metadata_files),
                timeout_s=read.remaining(), stop=_NeverStop())
            read.remember(metadata.outcome, "metadata")
            for metadata_index, item in enumerate(metadata.items):
                index, identity, file_format = metadata_files[metadata_index]
                if item.identity != identity:
                    raise ValueError("结果元数据改变本次候选文件身份")
                if item.error is None:
                    page_entries[index] = _entry(identity, item.value, complete, file_format)
            if metadata.error is not None:
                return read.reply(metadata.error, page)
        page["set_finalized"] = complete and listing.page.next_cursor is None
        return read.reply(page=page)

    preview_inputs, originals, original_observations = [], [], {}
    for identity in listing.page.items:
        suffix = PurePosixPath(identity.path).suffix.casefold()
        if suffix == profile.preview_format.suffix.casefold():
            observation = {"preview_identity": identity.path, "state": "not_checked"}
            read.previews.append(observation)
            preview_inputs.append((identity, observation))
        elif suffix in by_suffix and by_suffix[suffix].format_id == profile.paired_original_format_id:
            observation = {"original_identity": identity.path,
                "state": "completion_unknown" if not complete else "not_observed_on_page"}
            read.previews.append(observation)
            original_observations[identity.path] = observation

    entries = {}
    for identity in listing.page.items:
        file_format = by_suffix.get(PurePosixPath(identity.path).suffix.casefold())
        if file_format is None:
            continue
        if not read.available():
            error = ErrorValue("adb_metadata_failed", "device_file_access", {"reason": "call_unavailable"})
            page["entries"] = [entries[item.path] for item in listing.page.items if item.path in entries]
            return read.reply(error, page)
        metadata = await filesystem.metadata(identity, timeout_s=read.remaining(), stop=_NeverStop())
        read.remember(metadata.outcome, "metadata")
        entry = _entry(identity, metadata.value if metadata.error is None else None,
            complete if metadata.error is None else False, file_format)
        entries[identity.path] = entry
        if metadata.error is not None:
            page["entries"] = [entries[item.path] for item in listing.page.items if item.path in entries]
            return read.reply(metadata.error, page)
        if file_format.format_id == profile.paired_original_format_id:
            originals.append(identity)

    for preview, observation in preview_inputs:
        paired = await read.scan_preview(filesystem, scope, preview, complete, observation, originals)
        for original in originals:
            if _stem(original.path) == _stem(preview.path):
                original_observations[original.path].update(observation, original_identity=original.path)
        if paired is not None:
            entries[preview.path] = paired
        if read.conflict is not None:
            break

    if complete and cursor is None and listing.page.next_cursor is None:
        for observation in original_observations.values():
            if observation["state"] == "not_observed_on_page":
                observation.update(state="missing", scan_complete=True)
    page["entries"] = [entries[identity.path] for identity in listing.page.items if identity.path in entries]
    page["set_finalized"] = read.conflict is None and complete and listing.page.next_cursor is None
    return read.reply(read.conflict, page)
