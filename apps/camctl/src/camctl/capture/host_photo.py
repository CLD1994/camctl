"""取得主机双文件的单张拍摄编排，按保存定义登记正式源。"""

from __future__ import annotations

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
from camctl.host_files.models import FileRef
from camctl.operations.models import AttemptTicket, EffectState
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcomeKind, StateDatabaseError
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


def _verify(runtime, repository, request, key):
    connection = runtime.owned.connection
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK").close()
        except Exception as error:
            raise ConsistencyError("原主机拍摄事务无法结束，保留原申请") from error
    if connection.in_transaction:
        raise ConsistencyError("原主机拍摄事务仍未结束，保留原申请")
    verifier = getattr(runtime, "host_photo_operation_verifier", None)
    if verifier is None:
        raise ConsistencyError("主机拍摄回执核实端口未装配，保留原申请")
    return verifier(request, key, repository)


def _request_result(runtime, repository, pending: PendingHostPhotoRequest, save):
    """按原完整请求与键核实 UNKNOWN；回滚重送也沿用原键。"""
    error = None
    for _ in range(2):
        receipt = save(pending.request, pending.key, runtime.owned)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            return receipt.value
        error = receipt.error
        if isinstance(error, StateDatabaseError):
            raise error
        if receipt.kind is DbOutcomeKind.UNKNOWN:
            verified = _verify(runtime, repository, pending.request, pending.key)
            if verified.kind is DbOutcomeKind.COMPLETED:
                return verified.value
            if verified.kind is DbOutcomeKind.UNKNOWN:
                raise ConsistencyError(
                    f"原主机拍摄文件申请回执仍未知，原request/key继续保留: {verified.error}"
                )
            if verified.kind not in (DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED):
                raise ConsistencyError(f"原主机拍摄文件申请核实未完成: {verified.error}")
        if runtime.owned.connection.in_transaction:
            try:
                runtime.owned.connection.execute("ROLLBACK")
            except Exception as failure:
                raise ConsistencyError("原主机拍摄文件申请事务无法回滚，原请求继续保留") from failure
    raise ConsistencyError(f"原主机拍摄文件申请未可靠保存，原请求继续保留: {error}")


def _prepare_files(runtime, action_id: int):
    from camctl.persistence.repositories.host_capture import PrepareHostPhoto

    repository, _roots = _owned_repository(runtime, action_id)
    pending = runtime.pending_host_photo_prepares.get(action_id)
    if pending is None:
        pending = PendingHostPhotoRequest(
            new_operation_key(), PrepareHostPhoto(action_id, runtime.wall_us()))
        runtime.pending_host_photo_prepares[action_id] = pending
    if not isinstance(pending.request, PrepareHostPhoto) or pending.request.action_id != action_id:
        raise ConsistencyError("主机拍摄文件准备仍持有其他原申请")
    files = _request_result(
        runtime, repository, pending,
        lambda request, key, owned: repository.prepare_host_photo(
            request.action_id, request.occurred_at, key, owned),
    )
    runtime.pending_host_photo_prepares.pop(action_id, None)
    return files


def _save_complete(runtime, action_id: int, request: HostPhotoComplete) -> None:
    repository, _roots = _owned_repository(runtime, action_id)
    pending = runtime.pending_host_photo_completions.get(action_id)
    if pending is None:
        pending = PendingHostPhotoRequest(new_operation_key(), request)
        runtime.pending_host_photo_completions[action_id] = pending
    elif pending.request != request:
        raise ConsistencyError("原主机拍摄完整字节申请不可替换")
    if not isinstance(pending.request, HostPhotoComplete):
        raise ConsistencyError("主机拍摄完整字节保存仍持有准备申请")
    _request_result(runtime, repository, pending, repository.save_host_photo_complete)
    runtime.pending_host_photo_completions.pop(action_id, None)


def _saved_start(runtime, action_id: int) -> SavedPhotoReturn | None:
    responsibility = f"start/{action_id}"
    with closing(runtime.owned.connection.execute(
        "SELECT r.id,r.action_id,r.activity_id,r.kind,a.attempt_no,a.status,a.effect_state,"
        " a.result_json,a.error_json,a.result_event_id,e.occurred_at"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " LEFT JOIN history_events e ON e.id=a.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 1",
        (responsibility,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    activity_id = _activity_id(runtime, action_id)
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


def _activity_id(runtime, action_id: int) -> int:
    with closing(runtime.owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,),
    )) as cursor:
        row = cursor.fetchone()
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


def _photo_artifacts(runtime, action_id: int, saved: SavedPhotoReturn):
    outcome = _load_outcome(saved)
    if outcome.status.value != "succeeded":
        return outcome, None
    if outcome.effect is not EffectState.CONFIRMED:
        raise ConsistencyError("已保存的成功主机单张结果没有确认拍摄效果")
    if len(outcome.observations) != 1:
        raise ConsistencyError("已确认成功的主机单张结果必须恰有一份照片观察")
    observation = outcome.observations[0]
    activity_id = str(_activity_id(runtime, action_id))
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
    if (original.media_type != "image/x-hasselblad-3fr"
            or preview.media_type != "image/jpeg"):
        raise ConsistencyError("已确认成功的主机单张原图与预览媒体类型不符")
    return outcome, (original, preview)


def _file_ref(prepared, root) -> FileRef:
    return FileRef(prepared.file_id, prepared.purpose, prepared.relative_path, root)


def _failure(runtime, action_id: int, *, unconfirmed: bool, occurred_at: int) -> None:
    from camctl.capture.handlers import _settle_open_start, _unconfirmed_failure
    from camctl.capture.media import RecordingFailure
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue

    activity_id = _activity_id(runtime, action_id)
    if unconfirmed:
        failure = _unconfirmed_failure(runtime, action_id)
        _settle_open_start(runtime, action_id, RunOutcome.UNCONFIRMED,
            error=ErrorValue("result_unconfirmed", "device"))
    else:
        failure = RecordingFailure("capture_failed", {
            "activity_id": str(activity_id), "reason": "device_failed"})
    runtime.save_capture_completion(FinishCapture(
        action_id, (), OutputCatalogFacts(action_id, True), occurred_at,
        failure=failure))


def _finish_not_started(runtime, action_id: int, action) -> bool:
    from camctl.capture.handlers import _finish_canceled_capture
    from camctl.scheduling.rules import WindowPhase, window_phase

    current = runtime.action(action_id)
    if current["status"] in (int(enum_for("actions.status").SUCCEEDED),
                              int(enum_for("actions.status").FAILED),
                              int(enum_for("actions.status").CANCELED),
                              int(enum_for("actions.status").EXPIRED)):
        return True
    if current["cancel_requested"]:
        _finish_canceled_capture(runtime, action_id, unstarted=True)
        return True
    now = runtime.wall_us()
    if window_phase(runtime.window_of(current), now) is WindowPhase.AFTER_WINDOW:
        receipt = runtime.scheduling.expire_action(
            ExpireActionRequest(action_id, now, now), new_operation_key(), runtime.owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"主机单张启动窗口过期事务未完成: {receipt.error}")
        return True
    return False


def _failure_after_return(runtime, action_id: int, saved: SavedPhotoReturn,
                          outcome) -> bool:
    from camctl.capture.handlers import _settle_unstarted_attempt
    action = runtime.action(action_id)
    attempt = runtime.last_attempt(f"start/{action_id}")
    if (outcome.effect is EffectState.NO_EFFECT
            and _settle_unstarted_attempt(runtime, action, attempt)):
        return True
    if outcome.effect is EffectState.NO_EFFECT and outcome.status.value == "failed":
        _failure(runtime, action_id, unconfirmed=False, occurred_at=saved.occurred_at)
        return True
    _failure(runtime, action_id, unconfirmed=True, occurred_at=saved.occurred_at)
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


def _device_drafts(runtime, action_id, artifacts, occurred_at):
    from camctl.capture.handlers import FileRegistration, ObservedFile, _binding, _register_observed
    from camctl.capture.results import FileKind
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, DownloadVariant

    binding = _binding(runtime.action(action_id))
    original, preview = artifacts
    identities = tuple(CameraFileIdentity(binding, original.original_name, variant, artifact.sha256)
                       for variant, artifact in zip(
                           (DownloadVariant.ORIGINAL, DownloadVariant.THUMBNAIL), artifacts))
    entries = tuple(ObservedFile(
        identity=identity.file_identity, locator=identity.locator(),
        evidence={"activity_id": str(_activity_id(runtime, action_id)),
                  "artifact": asdict(artifact)},
        complete=True, size_bytes=artifact.size_bytes, kind=FileKind.PHOTO,
        original_name=artifact.original_name, media_type=artifact.media_type,
        paired_identity=None if index == 0 else identities[0].file_identity,
    ) for index, (identity, artifact) in enumerate(zip(identities, artifacts)))
    registered = _register_observed(runtime, action_id, entries, occurred_at=occurred_at,
        registration=FileRegistration(
            ownership_method=int(enum_for("device_files.ownership_evidence_json.method").DRIVER_TASK_ASSOCIATION),
            captured_checksums={identity.file_identity: artifact.sha256
                                for identity, artifact in zip(identities, artifacts)}))
    original_id, preview_id = (file_id for _, file_id in registered)
    return (
        OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=original_id), True, original.sha256),
        OutputDraft(OutputKind.PREVIEW, FileReference(device_file_id=preview_id), True, preview.sha256,
                    original_batch_file_id=original_id),
    )


def _persist_outputs(runtime, action_id: int, files, artifacts, occurred_at: int) -> None:
    from camctl.persistence.repositories.capture import FinishCanceledCapture

    action = runtime.action(action_id)
    drafts = (_drafts(files, artifacts) if is_saved_host_photo(action)
              else _device_drafts(runtime, action_id, artifacts, occurred_at))
    request_type = FinishCanceledCapture if action["cancel_requested"] else FinishCapture
    runtime.save_capture_completion(request_type(
        action_id=action_id, drafts=drafts, catalog_facts=OutputCatalogFacts(action_id, True),
        occurred_at=occurred_at))


@owned_tool_call
async def advance_host_photo(runtime, action_id: int) -> None:
    """消费原 START 结果、归档双文件并按固定模式登记正式产物。"""
    from camctl.capture.handlers import (
        _control_call,
        _conclude_activity,
        _finish_canceled_capture,
        _handle_binding_failure,
    )
    from camctl.scheduling.rules import WindowPhase, window_phase

    action = runtime.action(action_id)
    if action["status"] in (
            int(_ACTION_STATUS.SUCCEEDED), int(_ACTION_STATUS.FAILED),
            int(_ACTION_STATUS.CANCELED), int(_ACTION_STATUS.EXPIRED)):
        return
    _repository, roots = _owned_repository(runtime, action_id)
    saved = _saved_start(runtime, action_id)

    # 只有尚无 START 责任时才核当前设备绑定与新派发窗口。成功返回的
    # 原 START 后续只依赖保存结果、根目录及文件责任完成本地归档。
    if saved is None:
        if action["cancel_requested"]:
            _finish_canceled_capture(runtime, action_id, unstarted=True)
            return
        if _handle_binding_failure(runtime, action):
            return
        now = runtime.wall_us()
        phase = window_phase(runtime.window_of(action), now)
        if phase is WindowPhase.BEFORE_START:
            return
        if phase is WindowPhase.AFTER_WINDOW:
            receipt = runtime.scheduling.expire_action(
                ExpireActionRequest(action_id, now, now), new_operation_key(), runtime.owned)
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(f"主机单张过期事务未完成: {receipt.error}")
            return
        files = _prepare_files(runtime, action_id)
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
            if _finish_not_started(runtime, action_id, action):
                return
            return
        saved = _saved_start(runtime, action_id)
        if saved is None:
            raise ConsistencyError("主机单张已派发但没有原 START 责任")
    else:
        files = _prepare_files(runtime, action_id)

    # 旧前台或中断会话保留 RUNNING 时，沿原票据恢复；恢复只能结束这
    # 次尝试，不能生成另一个 START。
    if saved.result_json is None:
        if saved.status == int(_ATTEMPT.RUNNING):
            runtime.recover_attempt(saved.ticket)
            saved = _saved_start(runtime, action_id)
            if saved is None or saved.result_json is None:
                return
        else:
            raise ConsistencyError("主机单张 START 的结果事件缺失但尝试已终态")

    outcome, artifacts = _photo_artifacts(runtime, action_id, saved)
    if artifacts is None:
        _failure_after_return(runtime, action_id, saved, outcome)
        return

    original, preview = artifacts
    # 归档按固定顺序逐件幂等提交。原图成功而预览中断时，恢复会复用
    # 已匹配的原图目标，再完成预览，不重新访问相机。
    files = _prepare_files(runtime, action_id)
    await archive_capture_file(
        roots, saved.ticket.target_id, original,
        _file_ref(files.original, roots.staging),
    )
    await archive_capture_file(
        roots, saved.ticket.target_id, preview,
        _file_ref(files.preview, roots.staging),
    )
    occurred_at = saved.occurred_at
    _save_complete(runtime, action_id, HostPhotoComplete(
        action_id=action_id,
        original_file_id=files.original.file_id,
        original_size_bytes=original.size_bytes,
        original_sha256=original.sha256,
        preview_file_id=files.preview.file_id,
        preview_size_bytes=preview.size_bytes,
        preview_sha256=preview.sha256,
        occurred_at=occurred_at,
    ))
    # Hasselblad 的成功单张返回与已核实完整原图/预览共同证明本次活动结束；
    # 先用正式活动事务记录 END/RELEASE，再把动作和产物登记为终态。
    _conclude_activity(runtime, action_id)
    _persist_outputs(runtime, action_id, files, artifacts, occurred_at)
