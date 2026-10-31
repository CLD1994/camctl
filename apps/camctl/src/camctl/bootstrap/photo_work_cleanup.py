"""按已保存角色来源和原工作路径有限清理照片临时副本。"""
import asyncio
import errno
import os
import re
import stat
from contextlib import closing
from pathlib import Path

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.host_files.capture_sources import (
    CaptureFileError, _open_directory, _open_directory_at, _sync_directory_fd,
)
from camctl.host_files.downloads import (
    CaptureCleanupWorkspace, ResultDownloadWorkspace, recover_result_download, result_role_owned, settled_file_io,
)
from camctl.host_files.models import BoundDirectories
from camctl.history.photo_cache_request import parse_photo_cache_request
from camctl.logging_runtime.diagnostics import record_error
from camctl.operations.error_diagnostics import attach_diagnostic_facts
from camctl.persistence.fact_reads import read_event
from camctl.persistence.repositories.capture_facts import PhotoResultReads
from camctl.persistence.repositories.result_pages import _at
from camctl.persistence.transaction import row_facts


_CURSOR = ".photo-work-cleanup-cursor"
_TEMP = ".photo-work-cleanup-cursor.tmp"
_HOST_CURSOR = ".photo-host-work-cleanup-cursor"
_HOST_TEMP = ".photo-host-work-cleanup-cursor.tmp"
_TURN = ".download-cleanup-turn"
_TURN_TEMP = ".download-cleanup-turn.tmp"


def _load_cursor(staging, name=_CURSOR):
    try:
        fd = os.open(staging / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return 0
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("照片工作目录维护游标不是普通文件")
        raw = stream.read(33)
    if re.fullmatch(rb"[0-9]{1,19}\n", raw) is None:
        raise ValueError("照片工作目录维护游标格式无效")
    return int(raw)


def _save_cursor(staging, identity, name=_CURSOR, temporary=_TEMP):
    fd = os.open(staging / temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(f"{identity}\n".encode("ascii"))
    os.replace(staging / temporary, staging / name)


def _attempt_candidate(connection, attempt_id, staging, horizon, boundary):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.contracts.json_values import parse_exact_json
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE
    from camctl.operations.models import AttemptTicket, SettlementBasis
    from camctl.operations.result_format import read_result_document
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.result_pages import read_pages
    from types import SimpleNamespace

    with closing(connection.execute(
        "SELECT r.id,a.attempt_no,r.action_id,r.activity_id,r.responsibility_key,"
        "a.status,a.effect_state,a.result_json,a.error_json,a.intent_event_id,a.result_event_id"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE a.id=?", (attempt_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("照片工作目录维护缺少原结果尝试")
    run_id, attempt_no, action_id, activity_id, responsibility, status, effect, result, error, intent, result_event = row
    action = row_facts(connection, "actions", action_id)
    if (action is None or action["execution_spec_json"].get("output_storage") != "device"
            or responsibility != f"results/{activity_id}" or activity_id is None):
        return None
    with closing(connection.execute(
        "SELECT a.status,a.effect_state,a.result_json FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 2", (f"start/{action_id}",))) as cursor:
        starts = cursor.fetchall()
    if len(starts) != 1 or starts[0][2] is None or starts[0][0] != int(enum_for("operation_attempts.status").SUCCEEDED) or starts[0][1] != int(enum_for("operation_attempts.effect_state").CONFIRMED):
        return None
    ticket = AttemptTicket(attempt_no, "result", str(activity_id), responsibility, run_id)
    reads = PhotoResultReads(connection)
    _, start, _ = reads.read_device_start(action, activity_id, before_event_id=intent,
                                           output_directory=str(staging / "capture-work" / str(activity_id)))
    if status == int(enum_for("operation_attempts.status").RUNNING) and result is None:
        if boundary is RecoveryBoundary.UNCONFIRMED or intent > horizon:
            return None
    elif status != int(enum_for("operation_attempts.status").RUNNING) and result is not None and result_event is not None:
        actual = read_result_document(status, effect, parse_exact_json(result),
                                      None if error is None else parse_exact_json(error))
        validate_outcome(ticket, actual, EVIDENCE)
        if actual.settlement.basis is SettlementBasis.ASSUMED and (
                boundary is RecoveryBoundary.UNCONFIRMED or intent > horizon):
            return None
    else:
        raise ConsistencyError("照片工作目录原尝试状态与结果不一致")
    page = read_pages(ticket, None, 3, SimpleNamespace(connection=connection))
    if page.next_cursor is not None or len(page.items) > 2:
        raise ConsistencyError("单张照片原尝试超出固定角色页范围")
    allowed = {"original": True, "thumbnail": True}
    sync_names = []
    for saved in page.items:
        observations = [item for item in saved.page.outcome.observations
                        if item.type == "result_files_listed" and item.version == 3]
        if not observations:
            continue
        if len(observations) != 1 or len(saved.file_ids) != 1:
            raise ConsistencyError("照片工作目录来源页不完整")
        role = observations[0].data.get("role")
        if role not in allowed:
            raise ConsistencyError("照片工作目录来源角色无效")
        reads.verify_device_page(action_id, activity_id, attempt_id, saved)
        purpose = enum_for("intermediate_files.purpose")
        expected = purpose.CAPTURE_ORIGINAL if role == "original" else purpose.CAPTURE_PREVIEW
        with closing(connection.execute(
            "SELECT id FROM intermediate_files WHERE owner_action_id=? AND purpose=? LIMIT 2",
            (action_id, int(expected)))) as cursor:
            files = cursor.fetchall()
        if not files:
            allowed[role] = False
            continue
        if len(files) != 1:
            raise ConsistencyError("照片工作目录存在重复角色缓存责任")
        with closing(connection.execute(
            "SELECT id FROM history_events WHERE event_type=26"
            " AND json_extract(body_json,'$.rows[0].table')='intermediate_files'"
            " AND json_extract(body_json,'$.rows[0].id')=?"
            " AND json_extract(body_json,'$.evidence.photo_cache_request') IS NOT NULL"
            " AND id<=? ORDER BY id LIMIT 2", (files[0][0], horizon))) as cursor:
            events = cursor.fetchall()
        if not events:
            allowed[role] = False
            continue
        if len(events) != 1:
            raise ConsistencyError("照片工作目录存在重复缓存决定")
        event = read_event(connection, events[0][0])
        request = parse_photo_cache_request(event.event.evidence.get("photo_cache_request"))
        if (request.device_file_id != saved.file_ids[0][1]
                or request.result_page_event_id != saved.ref.event_id
                or reads.verify_cache(request, before_event_id=event.event.event_id) != role):
            raise ConsistencyError("照片工作目录缓存决定与原角色页不符")
        candidate = start["original" if role == "original" else "preview"]
        if candidate is not None:
            sync_names.append((role, candidate["host_name"]))
    status_enum = enum_for("actions.status")
    terminal = action["status"] in (int(status_enum.SUCCEEDED), int(status_enum.FAILED),
                                    int(status_enum.CANCELED), int(status_enum.EXPIRED))
    failed_names = []
    if terminal:
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.action_id=? AND a.status=? LIMIT 1",
            (action_id, int(enum_for("operation_attempts.status").RUNNING)))) as cursor:
            stopped = cursor.fetchone() is None
        if stopped:
            for role, name in (("original", "original"), ("thumbnail", "preview")):
                candidate = start[name]
                if candidate is None:
                    continue
                with closing(connection.execute(
                    "SELECT 1 FROM result_page_files WHERE action_id=? AND run_id=?"
                    " AND CASE WHEN json_valid(identity) THEN json_extract(identity,'$[0]') END='hb-download-v3'"
                    " AND CASE WHEN json_valid(identity) THEN json_extract(identity,'$[5]') END=? LIMIT 1",
                    (action_id, run_id, role))) as cursor:
                    has_source = cursor.fetchone() is not None
                if not has_source:
                    failed_names.append((role, candidate["host_name"]))
    return ticket, allowed, tuple(sync_names), tuple(failed_names), terminal


def _remove_direct_file(directory_fd, directory, name):
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(char in name for char in ("/", "\\", "\x00", "\r", "\n"))):
        raise ValueError("照片工作文件不是已确认的直接文件名")
    try:
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(observed.st_mode):
        raise CaptureFileError("cleanup_type", directory / name, detail="原工作文件不是直接普通文件")
    os.unlink(name, dir_fd=directory_fd)
    _sync_directory_fd(directory_fd, directory, "cleanup_workspace_sync")


def _cleanup_directory(roots, directory, category, files, owner, *, remove_empty):
    try:
        with _open_directory(roots.staging, "cleanup_staging_open", owner=owner) as staging_fd:
            with _open_directory_at(staging_fd, category, directory.parent,
                                    "cleanup_parent_open", owner=owner) as parent_fd:
                with _open_directory_at(parent_fd, directory.name, directory,
                                        "cleanup_workspace_open", owner=owner) as workspace_fd:
                    opened = os.fstat(workspace_fd)
                    for name in files:
                        _remove_direct_file(workspace_fd, directory, name)
                if remove_empty:
                    named = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
                    if (not stat.S_ISDIR(named.st_mode)
                            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
                        raise CaptureFileError("cleanup_changed", directory, detail="原工作目录身份发生变化")
                    try:
                        os.rmdir(directory.name, dir_fd=parent_fd)
                    except OSError as error:
                        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                            raise
                    else:
                        _sync_directory_fd(parent_fd, directory.parent, "cleanup_parent_sync")
    except CaptureFileError as error:
        if isinstance(error.cause, FileNotFoundError):
            return
        raise
    except FileNotFoundError:
        return


def _cleanup_sync_files(staging, ticket, names, terminal, owner):
    roots = BoundDirectories(staging=staging)
    directory = staging / "capture-work" / ticket.target_id
    _cleanup_directory(roots, directory, "capture-work", names, owner, remove_empty=terminal)
    if not owner.files_closed:
        raise BaseExceptionGroup("照片采集工作目录关闭未确认", list(owner.close_errors))


class PhotoWorkCleanup:
    """按原尝试分页，维护两个固定角色的独占工作路径。"""

    def __init__(self, staging, *, boundary, pending_scans, pending_results):
        self.staging, self.boundary = Path(staging), boundary
        self.pending_scans, self.pending_results = pending_scans, pending_results
        self.horizon = None
        self.ceiling = 0
        self.after = None
        self.finished = False

    async def initialize(self, database, *, horizon):
        self.horizon = horizon
        def ceiling(owned):
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            with closing(owned.connection.execute(
                "SELECT COALESCE(MAX(t.intent_event_id),0) FROM operation_attempts t"
                " JOIN operation_runs r ON r.id=t.run_id JOIN actions a ON a.id=r.action_id"
                " WHERE r.kind=? AND a.driver_id=? AND t.intent_event_id<=?",
                (int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS), DRIVER_ID, horizon))) as cursor:
                return int(cursor.fetchone()[0])
        self.ceiling = await database.read(ceiling, description="照片工作目录维护上界读取")

    async def run(self, database, *, limit):
        if self.finished or limit <= 0 or self.horizon is None:
            return 0
        if self.after is None:
            self.after = await settled_file_io(_load_cursor, self.staging)
            if self.after >= self.ceiling:
                self.after = 0
        def candidates(owned):
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            with closing(owned.connection.execute(
                "SELECT t.id,t.intent_event_id,r.id,t.attempt_no FROM operation_attempts t"
                " JOIN operation_runs r ON r.id=t.run_id JOIN actions a ON a.id=r.action_id"
                " WHERE r.kind=? AND a.driver_id=? AND t.intent_event_id>? AND t.intent_event_id<=?"
                " ORDER BY t.intent_event_id LIMIT ?",
                (int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS), DRIVER_ID,
                 self.after, self.ceiling, limit))) as cursor:
                return cursor.fetchall()
        rows = await database.read(candidates, description="照片工作目录维护候选读取")
        for attempt_id, intent, run_id, attempt_no in rows:
            self.after = intent
            try:
                if (run_id, attempt_no) in self.pending_scans or (run_id, attempt_no) in self.pending_results:
                    continue
                candidate = await database.read(lambda owned: _attempt_candidate(
                    owned.connection, attempt_id, self.staging, self.horizon, self.boundary),
                    description="核对照片工作目录原尝试及选择")
                if candidate is None:
                    continue
                ticket, allowed, sync_names, failed_names, terminal = candidate
                if not any(key[0] == run_id for key in (*self.pending_scans, *self.pending_results)):
                    sync_names += tuple((role, name) for role, name in failed_names
                                        if not result_role_owned(ticket.target_id, role))
                for role in ("original", "thumbnail"):
                    if allowed[role]:
                        await recover_result_download(self.staging, ticket, role)
                for role, name in sync_names:
                    owner = ResultDownloadWorkspace(self.staging, ticket, role)
                    if owner.claim():
                        try:
                            await settled_file_io(_cleanup_sync_files, self.staging, ticket, (name,), False, owner)
                        except BaseException as error:
                            if not owner.files_closed:
                                attach_diagnostic_facts(error, {"photo_cleanup_close_errors": owner.close_errors})
                            raise
                        finally:
                            owner.release()
                if terminal:
                    owner = CaptureCleanupWorkspace(self.staging, ticket)
                    if owner.claim():
                        try:
                            await settled_file_io(_cleanup_sync_files, self.staging, ticket, (), True, owner)
                        except BaseException as error:
                            if not owner.files_closed:
                                attach_diagnostic_facts(error, {"photo_cleanup_close_errors": owner.close_errors})
                            raise
                        finally:
                            owner.release()
            except Exception as error:
                record_error(error, stage="capture.photo_work_cleanup", context={"result_attempt_id": attempt_id})
            finally:
                await settled_file_io(_save_cursor, self.staging, self.after)
        self.finished = True
        return len(rows)


def _host_workspace_candidate(connection, result_event_id):
    """只接受原 HOST 结果与两份正式归档共同可靠的已终态动作。"""
    from camctl.contracts.json_values import parse_exact_json
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE
    from camctl.host_files.capture_sources import CapturedArtifact
    from camctl.operations.models import AttemptTicket
    from camctl.operations.result_format import read_result_document
    from camctl.operations.validation import validate_outcome
    from camctl.history.host_photo_request import parse_host_photo_request
    from camctl.history.validators import validate_event_structure

    with closing(connection.execute(
        "SELECT r.id,a.attempt_no,r.action_id,r.activity_id,r.responsibility_key,"
        "a.status,a.effect_state,a.result_json,a.error_json,a.intent_event_id"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE a.result_event_id=?",
        (result_event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("HOST 采集目录缺少原 START 结果")
    run_id, attempt_no, action_id, activity_id, responsibility, status, effect, result, error, intent = row
    action = row_facts(connection, "actions", action_id)
    if action is None or action["execution_spec_json"].get("output_storage") != "host":
        return None
    if action["status"] != int(enum_for("actions.status").SUCCEEDED):
        return None
    if responsibility != f"start/{action_id}" or activity_id is None:
        raise ConsistencyError("HOST 采集目录改变原 START 归属")
    ticket = AttemptTicket(attempt_no, "control", str(activity_id), responsibility, run_id)
    outcome = read_result_document(status, effect, parse_exact_json(result),
                                   None if error is None else parse_exact_json(error))
    validate_outcome(ticket, outcome, EVIDENCE)
    observations = [item for item in outcome.observations if item.type == "photo_taken" and item.version == 1]
    if outcome.status.value != "succeeded" or outcome.effect.value != "confirmed" or len(observations) != 1:
        return None
    data = observations[0].data
    if data.get("activity_id") != str(activity_id):
        raise ConsistencyError("HOST 采集目录改变原活动")
    original, preview = CapturedArtifact.from_json(data["original"]), CapturedArtifact.from_json(data["preview"])
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=26"
        " AND json_extract(body_json,'$.evidence.host_photo_request.action_id')=?"
        " AND json_extract(body_json,'$.evidence.host_photo_request.original_file_id') IS NOT NULL"
        " ORDER BY id LIMIT 2", (action_id,))) as cursor:
        completions = cursor.fetchall()
    if len(completions) != 1:
        raise ConsistencyError("HOST 采集目录缺少唯一双文件归档事务")
    event = read_event(connection, completions[0][0])
    intent_event = read_event(connection, intent)
    result_event = read_event(connection, result_event_id)
    if (validate_event_structure(event.event)[:2] != ("INTERMEDIATE_FILE_CHANGED", "HOST_PHOTO_COMPLETE")
            or validate_event_structure(intent_event.event)[0] != "ATTEMPT_STARTED"
            or validate_event_structure(result_event.event)[:2] != ("ATTEMPT_RESULT", "SUCCEED")
            or intent_event.transaction.last_event_id >= result_event.transaction.first_event_id
            or result_event.transaction.last_event_id >= event.transaction.first_event_id):
        raise ConsistencyError("HOST 归档与原 START 事务时序不符")
    request = parse_host_photo_request(event.event.evidence["host_photo_request"], reason=2)
    if (request.action_id != action_id or request.original_size_bytes != original.size_bytes
            or request.original_sha256 != original.sha256 or request.preview_size_bytes != preview.size_bytes
            or request.preview_sha256 != preview.sha256):
        raise ConsistencyError("HOST 归档与原双文件内容不同")
    with closing(connection.execute(
        "SELECT kind,intermediate_file_id,device_file_id FROM outputs WHERE source_action_id=? ORDER BY kind LIMIT 3",
        (action_id,))) as cursor:
        outputs = cursor.fetchall()
    kinds = enum_for("outputs.kind")
    if len(outputs) != 2 or set(outputs) != {(int(kinds.ORIGINAL), request.original_file_id, None),
                        (int(kinds.PREVIEW), request.preview_file_id, None)}:
        raise ConsistencyError("HOST 正式产物不对应原双文件归档")
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.action_id=? AND a.status=1 LIMIT 1", (action_id,))) as cursor:
        if cursor.fetchone() is not None:
            return None
    return ticket


def _deferred_workspace_candidate(connection, result_event_id, staging):
    """终态按需照片仅清理原 START 可信记录的直接文件。"""
    from camctl.operations.models import AttemptTicket

    with closing(connection.execute(
        "SELECT r.id,t.attempt_no,r.action_id,r.activity_id,r.responsibility_key,t.status,t.effect_state"
        " FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE t.result_event_id=?",
        (result_event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("照片采集目录缺少原 START 结果")
    run_id, attempt_no, action_id, activity_id, responsibility, status, effect = row
    action = row_facts(connection, "actions", action_id)
    if action is None or action["execution_spec_json"].get("photo_source_policy") != "deferred":
        return None
    terminal = enum_for("actions.status")
    if action["status"] not in (int(terminal.SUCCEEDED), int(terminal.FAILED),
                                int(terminal.CANCELED), int(terminal.EXPIRED)):
        return None
    if (activity_id is None or responsibility != f"start/{action_id}"
            or status != int(enum_for("operation_attempts.status").SUCCEEDED)
            or effect != int(enum_for("operation_attempts.effect_state").CONFIRMED)):
        return None
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND t.status=? LIMIT 1",
        (action_id, int(enum_for("operation_attempts.status").RUNNING)))) as cursor:
        if cursor.fetchone() is not None:
            return None
    ticket = AttemptTicket(attempt_no, "control", str(activity_id), responsibility, run_id)
    start, data, _ = PhotoResultReads(connection).read_device_start(action, activity_id,
        before_event_id=2**63 - 1, output_directory=str(staging / "capture-work" / str(activity_id)))
    if start != result_event_id:
        raise ConsistencyError("照片采集目录改变原 START 结果")
    names = tuple(candidate["host_name"] for candidate in (data["original"], data["preview"])
                  if candidate is not None)
    return ticket, names


class HostCaptureWorkCleanup:
    def __init__(self, staging):
        self.staging = Path(staging)
        self.horizon = None
        self.ceiling = 0
        self.after = None
        self.finished = False

    async def initialize(self, database, *, horizon):
        self.horizon = horizon
        def ceiling(owned):
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            with closing(owned.connection.execute(
                "SELECT COALESCE(MAX(t.result_event_id),0) FROM operation_attempts t"
                " JOIN operation_runs r ON r.id=t.run_id JOIN actions a ON a.id=r.action_id"
                " WHERE r.kind=? AND a.driver_id=? AND t.result_event_id<=?",
                (int(enum_for("operation_runs.kind").START), DRIVER_ID, horizon))) as cursor:
                return int(cursor.fetchone()[0])
        self.ceiling = await database.read(ceiling, description="HOST 采集目录维护上界读取")

    async def run(self, database, *, limit):
        if self.finished or limit <= 0 or self.horizon is None:
            return 0
        if self.after is None:
            self.after = await settled_file_io(_load_cursor, self.staging, _HOST_CURSOR)
            if self.after >= self.ceiling:
                self.after = 0
        def candidates(owned):
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            with closing(owned.connection.execute(
                "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " JOIN actions a ON a.id=r.action_id WHERE r.kind=? AND a.driver_id=?"
                " AND t.result_event_id>? AND t.result_event_id<=? ORDER BY t.result_event_id LIMIT ?",
                (int(enum_for("operation_runs.kind").START), DRIVER_ID, self.after, self.ceiling, limit))) as cursor:
                return cursor.fetchall()
        rows = await database.read(candidates, description="HOST 采集目录维护候选读取")
        for event_id, in rows:
            self.after = event_id
            try:
                def candidate_for(owned):
                    ticket = _host_workspace_candidate(owned.connection, event_id)
                    return ((ticket, ()) if ticket is not None else
                            _deferred_workspace_candidate(owned.connection, event_id, self.staging))
                candidate = await database.read(candidate_for, description="核对照片采集目录原归档")
                if candidate is not None:
                    ticket, names = candidate
                    owner = CaptureCleanupWorkspace(self.staging, ticket)
                    if owner.claim():
                        try:
                            await settled_file_io(_cleanup_sync_files, self.staging, ticket, names, True, owner)
                        except BaseException as error:
                            if not owner.files_closed:
                                attach_diagnostic_facts(error, {"photo_cleanup_close_errors": owner.close_errors})
                            raise
                        finally:
                            owner.release()
            except Exception as error:
                record_error(error, stage="capture.host_work_cleanup", context={"start_result_event_id": event_id})
            finally:
                await settled_file_io(_save_cursor, self.staging, self.after, _HOST_CURSOR, _HOST_TEMP)
        self.finished = True
        return len(rows)


class DownloadCleanupChain:
    """共享本次维护额度及启动边界。"""

    def __init__(self, read_cleanup, photo_cleanup, host_cleanup):
        self.read_cleanup, self.photo_cleanup, self.host_cleanup = read_cleanup, photo_cleanup, host_cleanup
        self.finished = False

    async def initialize(self, database, *, horizon):
        await self.read_cleanup.initialize(database, horizon=horizon)
        await self.photo_cleanup.initialize(database, horizon=horizon)
        await self.host_cleanup.initialize(database, horizon=horizon)

    async def run(self, database):
        if self.finished:
            return
        # 共享总额度，但每次从下一类开始，避免永久历史候选饿死其他类。
        first = (await settled_file_io(_load_cursor, self.photo_cleanup.staging, _TURN)) % 3
        remaining = self.read_cleanup.limit
        try:
            for kind in ((first + offset) % 3 for offset in range(3)):
                if remaining <= 0:
                    break
                if kind == 0:
                    self.read_cleanup.remaining = remaining
                    await self.read_cleanup.run(database)
                    remaining = self.read_cleanup.remaining
                elif kind == 1:
                    remaining -= await self.photo_cleanup.run(database, limit=remaining)
                else:
                    remaining -= await self.host_cleanup.run(database, limit=remaining)
            self.finished = True
        finally:
            await settled_file_io(_save_cursor, self.photo_cleanup.staging,
                                  (first + 1) % 3, _TURN, _TURN_TEMP)
