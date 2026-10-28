"""取得主机双文件的单张拍摄编排，按保存定义登记正式源。"""

from __future__ import annotations

from camctl.persistence.models import db_failure

from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.host_files.capture_sources import (
    CapturedArtifact,
    archive_capture_file,
)
from camctl.host_files.models import FileRef, FilePurpose
from camctl.host_files.photo_formats import CAPTURE_FORMATS
from camctl.capture.photo_format import saved_photo_format
from camctl.operations.models import AttemptTicket, EffectState
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.result_format import read_result_document
from camctl.persistence.business import settle_required
from camctl.persistence.models import DbOutcomeKind, StateDatabaseError, db_outcome_for
from camctl.persistence.repositories.capture import FinishCapture
from camctl.persistence.repositories.host_capture import (
    HostPhotoComplete,
    PrepareHostPhoto,
)
from camctl.persistence.repositories.scheduling import ExpireActionRequest
from camctl.persistence.transaction import row_facts
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)

_ACTION = enum_for("actions.type")
_ATTEMPT = enum_for("operation_attempts.status")
_EFFECT = enum_for("operation_attempts.effect_state")
_RUN_KIND = enum_for("operation_runs.kind")
_ACTION_STATUS = enum_for("actions.status")


@dataclass(frozen=True)
class PendingHostPhotoRequest:
    """一项主机拍摄保存请求及其首次分配的键。"""

    key: str
    request: Any


@dataclass(frozen=True)
class SavedPhotoReturn:
    """原 START 事务保存的设备响应及其事实时刻。"""

    ticket: AttemptTicket
    status: int
    effect: int
    occurred_at: int
    result_json: Any
    error_json: Any


def is_saved_host_photo(action: Mapping[str, Any]) -> bool:
    """只按首次受理保存的执行规格识别主机单张分支。"""
    return (
        action.get("type") == int(_ACTION.CAMERA_TAKE_PHOTO)
        and isinstance(action.get("execution_spec_json"), Mapping)
        and action["execution_spec_json"].get("output_storage") == "host"
    )


def is_saved_capture_photo(action: Mapping[str, Any]) -> bool:
    """两种正式源共用捕获返回；旧空定义继续走原列举流程。"""
    return (
        action.get("type") == int(_ACTION.CAMERA_TAKE_PHOTO)
        and isinstance(action.get("execution_spec_json"), Mapping)
        and action["execution_spec_json"].get("output_storage") in ("host", "device")
    )


def _owned_repository(runtime, action_id: int):
    repository = getattr(runtime, "host_capture", None)
    roots = getattr(runtime, "roots", None)
    if repository is None or roots is None:
        raise ConsistencyError("已保存的主机单张动作缺少主机文件仓储或目录绑定")
    return repository, roots


async def _verify(runtime, repository, request, key):
    """UNKNOWN由下一worker连接按原键核实，不访问失效连接。"""
    return await runtime.database.read(lambda owned: repository.verify_operation(request, key, owned))


async def _request_result(runtime, repository, pending: PendingHostPhotoRequest, save):
    """按原完整请求与键核实 UNKNOWN；回滚重送也沿用原键。"""
    error = None
    for _ in range(2):
        receipt = await runtime.database.write(lambda owned: save(pending.request, pending.key, owned), key=pending.key)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            return receipt.value
        error = receipt.error
        if isinstance(error, StateDatabaseError):
            raise error
        if receipt.kind is DbOutcomeKind.UNKNOWN:
            verified = await _verify(runtime, repository, pending.request, pending.key)
            if verified.kind is DbOutcomeKind.COMPLETED:
                return verified.value
            if verified.kind is DbOutcomeKind.UNKNOWN:
                raise ConsistencyError(
                    f"原主机拍摄文件申请回执仍未知，原request/key继续保留: {verified.error}"
                )
            if verified.kind not in (DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED):
                raise db_failure(ConsistencyError, f"原主机拍摄文件申请核实未完成: {verified.error}", verified)
    raise ConsistencyError(f"原主机拍摄文件申请未可靠保存，原请求继续保留: {error}")


async def _prepare_files(runtime, action_id: int):
    async def save_and_apply():
        from camctl.persistence.repositories.host_capture import PrepareHostPhoto

        repository, _roots = _owned_repository(runtime, action_id)
        pending = runtime.pending_host_photo_prepares.get(action_id)
        if pending is None:
            pending = PendingHostPhotoRequest(
                new_operation_key(), PrepareHostPhoto(action_id, runtime.wall_us()))
            runtime.pending_host_photo_prepares[action_id] = pending
        if not isinstance(pending.request, PrepareHostPhoto) or pending.request.action_id != action_id:
            raise ConsistencyError("主机拍摄文件准备仍持有其他原申请")
        try:
            files = await _request_result(
                runtime, repository, pending,
                lambda request, key, owned: repository.prepare_host_photo(
                    request.action_id, request.occurred_at, key, owned),
            )
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is not None and receipt.kind is DbOutcomeKind.COMPLETED:
                runtime.pending_host_photo_prepares.pop(action_id, None)
            raise
        runtime.pending_host_photo_prepares.pop(action_id, None)
        return files
    return await settle_required(save_and_apply())


async def _save_complete(runtime, action_id: int, request: HostPhotoComplete) -> None:
    async def save_and_apply():
        repository, _roots = _owned_repository(runtime, action_id)
        pending = runtime.pending_host_photo_completions.get(action_id)
        if pending is None:
            pending = PendingHostPhotoRequest(new_operation_key(), request)
            runtime.pending_host_photo_completions[action_id] = pending
        elif pending.request != request:
            raise ConsistencyError("原主机拍摄完整字节申请不可替换")
        if not isinstance(pending.request, HostPhotoComplete):
            raise ConsistencyError("主机拍摄完整字节保存仍持有准备申请")
        try:
            await _request_result(runtime, repository, pending, repository.save_host_photo_complete)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is not None and receipt.kind is DbOutcomeKind.COMPLETED:
                runtime.pending_host_photo_completions.pop(action_id, None)
            raise
        runtime.pending_host_photo_completions.pop(action_id, None)
    return await settle_required(save_and_apply())


async def _save_photo_cache(runtime, action_id, role, request=None):
    """沿角色原申请与原键完成缓存字节保存。"""
    from camctl.history.photo_cache_request import PhotoCacheComplete
    repository, _ = _owned_repository(runtime, action_id)
    identity = (action_id, role)
    pending = runtime.pending_host_photo_completions.get(identity)
    if pending is None:
        if not isinstance(request, PhotoCacheComplete):
            raise ConsistencyError("照片缓存缺少原完整申请")
        pending = PendingHostPhotoRequest(new_operation_key(), request)
        runtime.pending_host_photo_completions[identity] = pending
    elif request is not None and pending.request != request:
        raise ConsistencyError("照片缓存原完整申请不可替换")
    try:
        result = await _request_result(runtime, repository, pending, repository.save_photo_cache_complete)
    except BaseException as error:
        receipt = db_outcome_for(error)
        if receipt is not None and receipt.kind is DbOutcomeKind.COMPLETED:
            runtime.pending_host_photo_completions.pop(identity, None)
        raise
    runtime.pending_host_photo_completions.pop(identity, None)
    return result


async def archive_device_photo_cache(runtime, action_id, saved, device_file_id):
    """归档已固定选择；缓存本地失败不改变可靠相机来源。"""
    from camctl.history.photo_cache_request import PhotoCacheComplete
    from camctl.host_files.capture_sources import CaptureFileError, archive_photo_cache
    from camctl.host_files.downloads import ResultDownloadWorkspace
    from camctl.host_files.tasks import AsyncFileTask, FileTaskError, FileTaskId
    from camctl.logging_runtime.diagnostics import record_error
    from camctl.operations.error_diagnostics import exception_diagnostic, attach_diagnostic_facts
    import sys

    attempts = [item for item in saved.page.outcome.observations
                if item.type == "photo_result_attempt" and item.version == 2]
    if len(attempts) != 1 or not isinstance(attempts[0].data.get("comparison"), Mapping):
        return
    detail = attempts[0].data
    role, decision = detail["role"], detail["comparison"]
    selected = decision["selected"]
    identity = (action_id, role)
    if identity in runtime.pending_host_photo_completions:
        await _save_photo_cache(runtime, action_id, role)
        return
    files = await _prepare_files(runtime, action_id)
    prepared = files.original if role == "original" else files.preview
    facts = await runtime.database.read(lambda owned: row_facts(owned.connection, "intermediate_files", prepared.file_id),
                                        description="读取照片缓存原责任")
    entry = saved.page.entries[0]
    raw_artifact = decision["sync_artifact"] if selected == "sync" else detail["steps"][-1]["artifact"]
    artifact = CapturedArtifact.from_json(raw_artifact)
    if facts["last_error_json"] is not None:
        return
    if facts["size_bytes"] is not None or facts["sha256"] is not None:
        if facts["size_bytes"] != entry.size_bytes or facts["sha256"] != artifact.sha256:
            raise ConsistencyError("照片缓存原完整大小与来源不同")
        return
    if runtime.file_executor is None:
        return
    _, roots = _owned_repository(runtime, action_id)
    action = await runtime.action(action_id)
    owner = ResultDownloadWorkspace(roots.staging, saved.ref.ticket, role)
    if not owner.claim():
        raise ConsistencyError("照片缓存原文件操作仍由其他拥有者持有")
    async def work(_control):
        try:
            await archive_photo_cache(roots, saved.ref.ticket.target_id, saved.ref.ticket, role,
                selected, artifact, _file_ref(prepared, roots.staging), purpose=prepared.purpose,
                capture_format=saved_photo_format(action), owner=owner)
            request = PhotoCacheComplete(action_id, prepared.file_id, device_file_id, saved.ref.event_id,
                selected, artifact.size_bytes, artifact.sha256, runtime.wall_us(), None)
            await _save_photo_cache(runtime, action_id, role, request)
        except CaptureFileError as error:
            if not owner.files_closed:
                raise
            record_error(error, stage="capture.photo_cache_archive",
                         context={"action_id": action_id, "role": role})
            request = PhotoCacheComplete(action_id, prepared.file_id, device_file_id, saved.ref.event_id,
                selected, artifact.size_bytes, artifact.sha256, runtime.wall_us(), exception_diagnostic(error))
            await _save_photo_cache(runtime, action_id, role, request)
        finally:
            owner.release()
            if not owner.files_closed:
                actual = sys.exception()
                if actual is None:
                    raise BaseExceptionGroup("照片缓存文件关闭未确认", list(owner.close_errors))
                attach_diagnostic_facts(actual, {"photo_cache_close_errors": owner.close_errors})
    try:
        await runtime.file_executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId(f"photo_cache/{new_operation_key()}"), (prepared.file_id,),
            "photo_cache", "照片来源缓存归档", work))
    except FileTaskError as error:
        record_error(error, stage="capture.photo_cache_archive",
                     context={"action_id": action_id, "role": role})
    finally:
        owner.release()


async def _saved_start(runtime, action_id: int) -> SavedPhotoReturn | None:
    responsibility = f"start/{action_id}"
    def read(owned, parameters=(responsibility,)):
        with closing(owned.connection.execute("SELECT r.id,r.action_id,r.activity_id,r.kind,a.attempt_no,a.status,a.effect_state,"
        " a.result_json,a.error_json,a.result_event_id,e.occurred_at"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " LEFT JOIN history_events e ON e.id=a.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 1", parameters)) as cursor:
            return cursor.fetchone()
    row = await runtime.database.read(read, description="读取拍摄直接事实")
    if row is None:
        return None
    activity_id = (await _activity_id(runtime, action_id))
    (run_id, saved_action_id, saved_activity_id, kind, attempt_no, status,
     effect, result_json, error_json, result_event_id, occurred_at) = row
    if (saved_action_id != action_id or saved_activity_id != activity_id
            or kind != int(_RUN_KIND.START)):
        raise ConsistencyError("主机单张 START 责任与保存活动身份不符")
    ticket = AttemptTicket(attempt_no, "control", str(activity_id), responsibility, run_id)
    if result_event_id is None:
        return SavedPhotoReturn(ticket, status, effect, 0, None, None)
    if occurred_at is None or result_json is None:
        raise ConsistencyError("主机单张 START 的完整返回缺少原结果或时刻")
    return SavedPhotoReturn(ticket, status, effect, occurred_at, result_json, error_json)


async def _activity_id(runtime, action_id: int) -> int:
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", parameters)) as cursor:
            return cursor.fetchone()
    row = await runtime.database.read(read, description="读取拍摄直接事实")
    if row is None:
        raise ConsistencyError("主机单张动作缺少正式设备活动身份")
    return int(row[0])


def _load_outcome(saved: SavedPhotoReturn):
    result = None if saved.result_json is None else parse_exact_json(saved.result_json)
    error = None if saved.error_json is None else parse_exact_json(saved.error_json)
    try:
        return read_result_document(saved.status, saved.effect, result, error)
    except (TypeError, ValueError, KeyError) as failure:
        raise ConsistencyError(f"原主机单张 START 结果格式无效: {failure}") from failure


async def _photo_artifacts(runtime, action_id: int, saved: SavedPhotoReturn):
    outcome = _load_outcome(saved)
    if outcome.status.value != "succeeded":
        return outcome, None
    if outcome.effect is not EffectState.CONFIRMED:
        raise ConsistencyError("已保存的成功主机单张结果没有确认拍摄效果")
    photos = tuple(item for item in outcome.observations if item.type == "photo_taken")
    if len(photos) != 1:
        raise ConsistencyError("已确认成功的主机单张结果必须恰有一份照片观察")
    observation = photos[0]
    activity_id = str((await _activity_id(runtime, action_id)))
    if observation.type != "photo_taken" or observation.version != 1:
        raise ConsistencyError("已确认成功的主机单张结果包含无效照片观察类型或版本")
    data = observation.data
    if (not isinstance(data, Mapping)
            or set(data) != {"activity_id", "original", "preview", "steps"}):
        raise ConsistencyError("已确认成功的主机单张照片观察成员不完整")
    if data.get("activity_id") != activity_id or activity_id != saved.ticket.target_id:
        raise ConsistencyError("已确认成功的主机单张照片观察与原活动身份不符")
    steps = data.get("steps")
    if (not isinstance(steps, list) or not steps
            or any(not isinstance(step, Mapping)
                   or not all(isinstance(key, str) for key in step)
                   or not isinstance(step.get("name"), str)
                   or not step["name"].strip()
                   for step in steps)):
        raise ConsistencyError("已确认成功的主机单张照片观察 steps 结构无效")
    try:
        original = CapturedArtifact.from_json(data["original"])
        preview = CapturedArtifact.from_json(data["preview"])
    except (KeyError, ValueError, TypeError) as failure:
        raise ConsistencyError(
            f"已确认成功的主机单张原图或预览事实无效: {failure}") from failure
    description = CAPTURE_FORMATS[saved_photo_format((await runtime.action(action_id)))]
    if ((original.media_type, preview.media_type) !=
            (description.original_media_type, description.preview_media_type)
            or original.original_name == preview.original_name
            or not original.original_name.casefold().endswith(description.original_extension)
            or preview.original_name != original.original_name + ".thumbnail" + description.preview_extension):
        raise ConsistencyError("已确认成功的单张原图与预览不符合保存格式或配对身份")
    _capture_locator(outcome)
    return outcome, (original, preview)


def _file_ref(prepared, root) -> FileRef:
    return FileRef(prepared.file_id, prepared.purpose, prepared.relative_path, root)


async def _failure(runtime, action_id: int, *, unconfirmed: bool, occurred_at: int,
             reason: str = "device_failed") -> None:
    from camctl.capture.handlers import _settle_open_start, _unconfirmed_failure
    from camctl.capture.media import RecordingFailure
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue

    activity_id = (await _activity_id(runtime, action_id))
    if unconfirmed:
        failure = (await _unconfirmed_failure(runtime, action_id))
        (await _settle_open_start(runtime, action_id, RunOutcome.UNCONFIRMED,
            error=ErrorValue("result_unconfirmed", "device")))
    else:
        failure = RecordingFailure("capture_failed", {
            "activity_id": str(activity_id), "reason": reason})
    (await runtime.save_capture_completion(FinishCapture(
        action_id, (), OutputCatalogFacts(action_id, True), occurred_at,
        failure=failure)))


async def _finish_not_started(runtime, action_id: int, action) -> bool:
    from camctl.capture.handlers import _finish_canceled_capture
    from camctl.scheduling.rules import WindowPhase, window_phase

    current = (await runtime.action(action_id))
    if current["status"] in (int(enum_for("actions.status").SUCCEEDED),
                              int(enum_for("actions.status").FAILED),
                              int(enum_for("actions.status").CANCELED),
                              int(enum_for("actions.status").EXPIRED)):
        return True
    if current["cancel_requested"]:
        (await _finish_canceled_capture(runtime, action_id, unstarted=True))
        return True
    now = runtime.wall_us()
    if window_phase(runtime.window_of(current), now) is WindowPhase.AFTER_WINDOW:
        _save_inputs = (ExpireActionRequest(action_id, now, now), new_operation_key())
        _save_options = dict()
        receipt = (await runtime.database.write(lambda owned: runtime.scheduling.expire_action(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"主机单张启动窗口过期事务未完成: {receipt.error}", receipt)
        return True
    return False


async def _failure_after_return(runtime, action_id: int, saved: SavedPhotoReturn,
                          outcome) -> bool:
    from camctl.capture.handlers import _settle_unstarted_attempt
    action = (await runtime.action(action_id))
    attempt = (await runtime.last_attempt(f"start/{action_id}"))
    if (outcome.effect is EffectState.NO_EFFECT
            and (await _settle_unstarted_attempt(runtime, action, attempt))):
        return True
    if outcome.effect is EffectState.NO_EFFECT and outcome.status.value == "failed":
        (await _failure(runtime, action_id, unconfirmed=False, occurred_at=saved.occurred_at))
        from camctl.capture.handlers import _release_occupancy
        (await _release_occupancy(runtime, action_id))
        return True
    (await _failure(runtime, action_id, unconfirmed=True, occurred_at=saved.occurred_at))
    return True


def _drafts(files, artifacts: tuple[CapturedArtifact, CapturedArtifact]):
    original, preview = artifacts
    return (
        OutputDraft(
            OutputKind.ORIGINAL,
            FileReference(intermediate_file_id=files.original.file_id),
            True,
            original.sha256,
            original_name=original.original_name,
            media_type=original.media_type,
        ),
        OutputDraft(
            OutputKind.PREVIEW,
            FileReference(intermediate_file_id=files.preview.file_id),
            True,
            preview.sha256,
            original_batch_intermediate_file_id=files.original.file_id,
            original_name=preview.original_name,
            media_type=preview.media_type,
        ),
    )


def _capture_locator(outcome):
    from camctl.devices.drivers.hasselblad.files import CameraFileLocator

    observations = tuple(item for item in outcome.observations if item.type == "photo_taken")
    if len(observations) != 1 or not isinstance(observations[0].data, Mapping):
        raise ConsistencyError("相机正式源缺少唯一拍摄观察")
    steps = observations[0].data.get("steps")
    if not isinstance(steps, list):
        raise ConsistencyError("相机正式源缺少拍摄步骤事实")
    capture_steps = tuple(step for step in steps if isinstance(step, Mapping)
                          and step.get("name") == "capture")
    if len(capture_steps) != 1:
        raise ConsistencyError("相机正式源缺少唯一 capture 步骤")
    try:
        return CameraFileLocator.from_json(capture_steps[0].get("camera_locator"))
    except (TypeError, ValueError) as failure:
        raise ConsistencyError(f"相机正式源的机内定位事实无效: {failure}") from failure


def _capture_camera_identity(outcome):
    observation, = (item for item in outcome.observations if item.type == "photo_taken")
    steps = [step for step in observation.data["steps"] if step.get("name") == "capture"]
    identity = steps[0].get("camera_identity") if len(steps) == 1 else None
    if not isinstance(identity, Mapping) or set(identity) != {"serial", "product_id"}:
        raise ConsistencyError("相机正式源缺少原拍摄结果中的机身身份")
    return identity


async def _device_drafts(runtime, action_id, artifacts, occurred_at, outcome):
    from camctl.capture.handlers import FileRegistration, ObservedFile, _binding, _register_observed
    from camctl.capture.results import FileKind
    from camctl.devices.drivers.hasselblad.files import (
        CameraFileIdentity,
        CameraLocatorStatus,
        DownloadVariant,
    )

    binding = _binding((await runtime.action(action_id)))
    original, preview = artifacts
    locator = _capture_locator(outcome)
    camera = _capture_camera_identity(outcome)
    if locator.status is not CameraLocatorStatus.PATH:
        raise ConsistencyError("相机正式源缺少可下载的机内文件名和完整路径")
    try:
        identities = tuple(CameraFileIdentity(
            binding, locator.camera_path, locator.camera_name, variant, artifact.sha256,
            camera["serial"], camera["product_id"], artifact.size_bytes,
        ) for variant, artifact in zip(
            (DownloadVariant.ORIGINAL, DownloadVariant.THUMBNAIL), artifacts))
    except (TypeError, ValueError) as error:
        raise ConsistencyError("相机正式源的原拍摄机身或文件事实无效") from error
    activity_id = str(await _activity_id(runtime, action_id))
    entries = tuple(ObservedFile(
        identity=identity.file_identity, locator=identity.locator(),
        evidence={"activity_id": activity_id,
                  "artifact": asdict(artifact)},
        complete=True, size_bytes=artifact.size_bytes, kind=FileKind.PHOTO,
        original_name=artifact.original_name, media_type=artifact.media_type,
        paired_identity=None if index == 0 else identities[0].file_identity,
    ) for index, (identity, artifact) in enumerate(zip(identities, artifacts)))
    registered = (await _register_observed(runtime, action_id, entries, occurred_at=occurred_at,
        registration=FileRegistration(
            ownership_method=int(enum_for("device_files.ownership_evidence_json.method").DRIVER_TASK_ASSOCIATION),
            captured_checksums={identity.file_identity: artifact.sha256
                                for identity, artifact in zip(identities, artifacts)})))
    original_id, preview_id = (file_id for _, file_id in registered)
    return (
        OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=original_id), True, original.sha256),
        OutputDraft(OutputKind.PREVIEW, FileReference(device_file_id=preview_id), True, preview.sha256,
                    original_batch_file_id=original_id),
    )


async def _persist_outputs(runtime, action_id: int, files, artifacts, occurred_at: int, outcome) -> None:
    from camctl.persistence.repositories.capture import FinishCanceledCapture

    action = (await runtime.action(action_id))
    drafts = (_drafts(files, artifacts) if is_saved_host_photo(action)
              else (await _device_drafts(runtime, action_id, artifacts, occurred_at, outcome)))
    action = await runtime.action(action_id)
    request_type = FinishCanceledCapture if action["cancel_requested"] else FinishCapture
    (await runtime.save_capture_completion(request_type(
        action_id=action_id, drafts=drafts, catalog_facts=OutputCatalogFacts(action_id, True),
        occurred_at=occurred_at)))


async def _device_source_drafts(runtime, action_id: int, ticket):
    """按原结果页和完成事实取得两个固定相机来源。"""
    from camctl.capture.files import file_identity_key
    from camctl.capture.result_scans import SourceResultEntries
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, DownloadVariant

    action = await runtime.action(action_id)
    sources = {}
    async for entry, file, file_id in SourceResultEntries(runtime.capture, runtime.database, ticket).records():
        if not file.complete:
            continue
        identity = CameraFileIdentity.from_source(
            file_identity_key(action["device_id"], action["driver_id"], entry.identity), entry.locator)
        if identity.variant in sources:
            raise ConsistencyError("照片同一来源角色出现多个完整文件")
        sources[identity.variant] = (entry, file_id, identity)
    original = sources.get(DownloadVariant.ORIGINAL)
    preview = sources.get(DownloadVariant.THUMBNAIL)
    if original is None:
        return ()
    drafts = [OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=original[1]), True, original[2].sha256)]
    if preview is not None:
        if preview[0].paired_identity != original[0].identity:
            raise ConsistencyError("照片预览没有指向原可靠原图")
        drafts.append(OutputDraft(OutputKind.PREVIEW, FileReference(device_file_id=preview[1]), True,
                                  preview[2].sha256, original_batch_file_id=original[1]))
    return tuple(drafts)


async def _device_photo_results(runtime, action_id: int):
    """推进原 RESULTS，可靠关闭后交付已固定来源草稿。"""
    from camctl.capture.handlers import (
        ListingPhase, _close_check_unconfirmed, _conclude_activity, _finish_listing_result,
        _listing_round_input, _original_ticket,
    )
    from camctl.capture.media import RecordingFailure
    from camctl.operations.attempts import RunOutcome

    await runtime.resume_capture_completions(action_id)
    action = await runtime.action(action_id)
    if action["status"] in (int(_ACTION_STATUS.SUCCEEDED), int(_ACTION_STATUS.FAILED),
                            int(_ACTION_STATUS.CANCELED), int(_ACTION_STATUS.EXPIRED)):
        return None
    listing = await _listing_round_input(runtime, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        return None
    invalid = False
    unconfirmed = False
    binding_error = None
    if listing.phase is ListingPhase.LISTED:
        if listing.outcome.error is not None:
            invalid = listing.outcome.error.details.get("classification") == "invalid"
            if listing.outcome.error.code == "device_binding_unavailable":
                binding_error = listing.outcome.error
            if invalid or binding_error is not None:
                await _finish_listing_result(runtime, listing, end_run=RunOutcome.FAILED,
                                             run_error=listing.outcome.error)
            else:
                await _finish_listing_result(runtime, listing, retry_wait=True)
                return None
        else:
            await _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED)
    elif listing.phase is ListingPhase.EXHAUSTED:
        await _close_check_unconfirmed(runtime, action_id)
        unconfirmed = True
    elif listing.phase is ListingPhase.CLOSED and listing.outcome is not None and listing.outcome.error is not None:
        invalid = listing.outcome.error.details.get("classification") == "invalid"
        if listing.outcome.error.code == "device_binding_unavailable":
            binding_error = listing.outcome.error
        unconfirmed = not invalid and binding_error is None
    ticket = listing.ticket
    if ticket is None:
        ticket = await _original_ticket(runtime, f"results/{await _activity_id(runtime, action_id)}", "result")
    drafts = await _device_source_drafts(runtime, action_id, ticket)
    if binding_error is not None:
        failure = RecordingFailure("device_binding_unavailable", dict(binding_error.details))
    elif unconfirmed:
        failure = RecordingFailure("capture_result_unconfirmed", {
            "activity_id": str(await _activity_id(runtime, action_id)), "reason": "outputs_unknown"})
    elif invalid:
        failure = RecordingFailure("capture_failed", {
            "activity_id": str(await _activity_id(runtime, action_id)), "reason": "invalid_outputs"})
    else:
        failure = None
        if len(drafts) != 2:
            raise ConsistencyError("照片 RESULTS 成功但两个原角色尚未可靠完成")
    await _conclude_activity(runtime, action_id)
    return drafts, failure


@owned_tool_call
async def advance_host_photo(runtime, action_id: int) -> None:
    """消费原 START 结果、归档双文件并按固定模式登记正式产物。"""
    from camctl.capture.handlers import (
        _baseline_before_control,
        _control_call,
        _conclude_activity,
        _finish_canceled_capture,
        _handle_binding_failure,
        _expire_unentered_photo,
    )
    from camctl.scheduling.rules import WindowPhase, window_phase

    action = (await runtime.action(action_id))
    if is_saved_capture_photo(action) and not is_saved_host_photo(action):
        await runtime.resume_capture_completions(action_id)
        action = await runtime.action(action_id)
    if action["status"] in (
            int(_ACTION_STATUS.SUCCEEDED), int(_ACTION_STATUS.FAILED),
            int(_ACTION_STATUS.CANCELED), int(_ACTION_STATUS.EXPIRED)):
        return
    _repository, roots = _owned_repository(runtime, action_id)
    saved = (await _saved_start(runtime, action_id))

    # 只有尚无 START 责任时才核当前设备绑定与新派发窗口。成功返回的
    # 原 START 后续只依赖保存结果、根目录及文件责任完成本地归档。
    if saved is None:
        if action["cancel_requested"]:
            (await _finish_canceled_capture(runtime, action_id, unstarted=True))
            return
        if (await _handle_binding_failure(runtime, action)):
            return
        now = runtime.wall_us()
        phase = window_phase(runtime.window_of(action), now)
        if phase is WindowPhase.BEFORE_START:
            return
        if phase is WindowPhase.AFTER_WINDOW:
            _save_inputs = (ExpireActionRequest(action_id, now, now), new_operation_key())
            _save_options = dict()
            receipt = (await runtime.database.write(lambda owned: runtime.scheduling.expire_action(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"主机单张过期事务未完成: {receipt.error}", receipt)
            return
        if getattr(runtime, "capture_preparation_supported", False) is True:
            prepared = await _baseline_before_control(runtime, action)
            if prepared is not None:
                return
        files = (await _prepare_files(runtime, action_id))
        step = await _control_call(
            runtime,
            action,
            "take_photo",
            "photo_taken",
            activity_facts=lambda confirmed: (
                {"sent_at": runtime.wall_us()}
                | ({"started_at": runtime.wall_us(), "activity_state": 2}
                   if confirmed else {})
            ),
        )
        if step.phase == "not_granted":
            if (await _finish_not_started(runtime, action_id, action)):
                return
            return
        saved = (await _saved_start(runtime, action_id))
        if saved is None:
            raise ConsistencyError("主机单张已派发但没有原 START 责任")
    else:
        files = (await _prepare_files(runtime, action_id))

    # 旧前台或中断会话保留 RUNNING 时，沿原票据恢复；恢复只能结束这
    # 次尝试，不能生成另一个 START。
    if saved.result_json is None:
        if saved.status == int(_ATTEMPT.RUNNING):
            from camctl.devices.start_entry import EntryPhase
            entry = runtime.start_entries.get(runtime.database.metadata.instance_id, action_id)
            local_ready = (entry is not None and entry.ticket == saved.ticket and entry.phase is EntryPhase.READY
                           and not entry.active and not action["cancel_requested"])
            phase = window_phase(runtime.window_of(action), runtime.wall_us()) if local_ready else None
            if local_ready and phase is WindowPhase.AFTER_WINDOW:
                await _expire_unentered_photo(runtime, action, saved.ticket, entry)
                return
            if local_ready and phase is WindowPhase.IN_WINDOW:
                step = await _control_call(runtime, action, "take_photo", "photo_taken",
                    activity_facts=lambda confirmed: (
                        {"sent_at": runtime.wall_us()}
                        | ({"started_at": runtime.wall_us(), "activity_state": 2} if confirmed else {})))
                if step.phase == "not_granted":
                    return
                saved = (await _saved_start(runtime, action_id))
                if saved is None:
                    raise ConsistencyError("原单张 START 结果保存后责任缺失")
            if saved.result_json is None:
                (await runtime.recover_attempt(saved.ticket))
                saved = (await _saved_start(runtime, action_id))
                if saved is None or saved.result_json is None:
                    return
        else:
            raise ConsistencyError("主机单张 START 的结果事件缺失但尝试已终态")

    if not is_saved_host_photo(action):
        outcome = _load_outcome(saved)
        if outcome.status.value != "succeeded" or outcome.effect is not EffectState.CONFIRMED:
            await _failure_after_return(runtime, action_id, saved, outcome)
            return
        photos = [item for item in outcome.observations if item.type == "photo_taken" and item.version == 2]
        if photos:
            if len(photos) != 1 or photos[0].data.get("activity_id") != saved.ticket.target_id:
                raise ConsistencyError("DEVICE 单张缺少原可靠相机控制观察")
            result = await _device_photo_results(runtime, action_id)
            if result is None:
                return
            drafts, failure = result
            current = await runtime.action(action_id)
            if current["cancel_requested"]:
                from camctl.persistence.repositories.capture import FinishCanceledCapture
                request = FinishCanceledCapture(action_id, runtime.wall_us(), drafts,
                                                OutputCatalogFacts(action_id, True))
            else:
                request = FinishCapture(action_id, drafts, OutputCatalogFacts(action_id, True),
                                        runtime.wall_us(), failure=failure)
            await runtime.save_capture_completion(request)
            return

    outcome, artifacts = (await _photo_artifacts(runtime, action_id, saved))
    if artifacts is None:
        (await _failure_after_return(runtime, action_id, saved, outcome))
        return

    original, preview = artifacts
    from camctl.devices.drivers.hasselblad.files import CameraLocatorStatus
    if (not is_saved_host_photo(action)
            and _capture_locator(outcome).status is CameraLocatorStatus.EXPLICIT_MISSING):
        # 合法的缺名响应不是状态库损坏；保留原观察和工作文件，按业务
        # 结果不满足登记契约结束，不补造设备源或释放文件责任。
        (await _conclude_activity(runtime, action_id))
        (await _failure(runtime, action_id, unconfirmed=False, occurred_at=saved.occurred_at,
                 reason="invalid_outputs"))
        return
    capture_format = saved_photo_format(action)
    # 归档按固定顺序逐件幂等提交。原图成功而预览中断时，恢复会复用
    # 已匹配的原图目标，再完成预览，不重新访问相机。
    files = (await _prepare_files(runtime, action_id))
    await archive_capture_file(
        roots, saved.ticket.target_id, original,
        _file_ref(files.original, roots.staging),
        purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format=capture_format,
    )
    await archive_capture_file(
        roots, saved.ticket.target_id, preview,
        _file_ref(files.preview, roots.staging),
        purpose=FilePurpose.CAPTURE_PREVIEW, capture_format=capture_format,
    )
    occurred_at = saved.occurred_at
    (await _save_complete(runtime, action_id, HostPhotoComplete(
        action_id=action_id,
        original_file_id=files.original.file_id,
        original_size_bytes=original.size_bytes,
        original_sha256=original.sha256,
        preview_file_id=files.preview.file_id,
        preview_size_bytes=preview.size_bytes,
        preview_sha256=preview.sha256,
        occurred_at=occurred_at,
    )))
    # Hasselblad 的成功单张返回与已核实完整原图/预览共同证明本次活动结束；
    # 先用正式活动事务记录 END/RELEASE，再把动作和产物登记为终态。
    (await _conclude_activity(runtime, action_id))
    (await _persist_outputs(runtime, action_id, files, artifacts, occurred_at, outcome))
