"""主机拍摄源的稳定责任与完整字节事务；不访问物理文件。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    PURPOSE_FIXED_EXTENSIONS,
    relative_file_path,
    validate_relative_file_path,
)
from camctl.history.host_photo_request import (
    HostPhotoComplete,
    PrepareHostPhoto,
    parse_host_photo_request,
)
from camctl.history.events import branch_of, reason_for_branch
from camctl.history.validators import EventValidationError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import require_host_photo_format
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan, TransactionError, TransactionScope, commit_operation,
    event_envelope, next_row_id, row_change, row_facts, saved_transaction_events, update_change,
)

_PURPOSE = enum_for("intermediate_files.purpose")
_RETENTION = enum_for("intermediate_files.retention_state")
_CLEANUP = enum_for("intermediate_files.cleanup_state")
_ACTION = enum_for("actions.type")
_CREATE_REASON = reason_for_branch("INTERMEDIATE_FILE_CHANGED", "CREATE")
_HOST_PHOTO_COMPLETE_REASON = reason_for_branch(
    "INTERMEDIATE_FILE_CHANGED", "HOST_PHOTO_COMPLETE"
)
_ROLES = tuple(
    (_PURPOSE[purpose.name], extension)
    for purpose, extension in PURPOSE_FIXED_EXTENSIONS.items()
)
_HOST_PURPOSES = frozenset(int(purpose) for purpose, _ in _ROLES)


@dataclass(frozen=True)
class PreparedHostFile:
    file_id: int
    purpose: FilePurpose
    relative_path: str


@dataclass(frozen=True)
class HostPhotoFiles:
    original: PreparedHostFile
    preview: PreparedHostFile


def _files(rows):
    ordered = []
    for purpose, extension in _ROLES:
        matched = [row for row in rows if row["purpose"] == int(purpose)]
        if len(matched) != 1:
            raise ConsistencyError("主机拍摄源必须恰有一份原图和一份预览责任")
        row = matched[0]
        model = FilePurpose[purpose.name]
        validate_relative_file_path(model, row["id"], row["relative_path"])
        if row["relative_path"] != relative_file_path(model, row["id"], extension):
            raise ConsistencyError("主机拍摄源扩展名与固定角色不一致")
        valid_lifecycle = (
            row["retention_state"] == int(_RETENTION.REQUIRED)
            and row["cleanup_state"] == int(_CLEANUP.NOT_NEEDED)
        ) or (
            row["retention_state"] == int(_RETENTION.PROMOTED)
            and row["cleanup_state"] in (int(_CLEANUP.NOT_NEEDED), int(_CLEANUP.COMPLETED))
        )
        if row["owner_delivery_id"] is not None or not valid_lifecycle:
            raise ConsistencyError("主机拍摄源的责任或保留状态矛盾")
        ordered.append(PreparedHostFile(row["id"], model, row["relative_path"]))
    return HostPhotoFiles(*ordered)


def _load(connection, action_id):
    action = row_facts(connection, "actions", action_id)
    if action is None or action["type"] != int(_ACTION.CAMERA_TAKE_PHOTO):
        raise ConsistencyError("主机拍摄源要求已保存的单张拍摄动作")
    with closing(connection.execute(
        "SELECT id FROM intermediate_files WHERE owner_action_id=? AND purpose IN (?,?) ORDER BY id LIMIT 3",
        (action_id, int(_PURPOSE.CAPTURE_ORIGINAL), int(_PURPOSE.CAPTURE_PREVIEW)),
    )) as cursor:
        identities = cursor.fetchall()
    rows = [row_facts(connection, "intermediate_files", identity) for identity, in identities]
    if rows:
        _files(rows)
    return action, rows


def validate_host_photo_event(event, context):
    """事件层复核完整申请与两份角色责任，回放沿用同一契约。"""
    branch_name, _ = branch_of(event.event_type, event.reason)
    has_host_bytes = any(
        row.table == "intermediate_files"
        and context.state_rows.get("intermediate_files", {}).get(row.row_id, {}).get("purpose") in _HOST_PURPOSES
        and ("size_bytes" in row.after.values or "sha256" in row.after.values)
        for row in event.rows)
    has_host_create = any(row.table == "intermediate_files" and not row.before.exists
        and row.after.values.get("purpose") in _HOST_PURPOSES for row in event.rows)
    raw = event.evidence.get("host_photo_request")
    if raw is None:
        if has_host_bytes or has_host_create or branch_name == "HOST_PHOTO_COMPLETE":
            raise EventValidationError("主机拍摄文件责任及完整字节必须保存原完整申请")
        return
    if branch_name == "CREATE":
        request_reason = 1
    elif branch_name == "HOST_PHOTO_COMPLETE":
        request_reason = 2
    else:
        raise EventValidationError("主机拍摄申请只能用于登记的 CREATE 或 HOST_PHOTO_COMPLETE 分支")
    try:
        request = parse_host_photo_request(raw, reason=request_reason)
        action = context.state_rows.get("actions", {}).get(request.action_id)
        if (action is None or action.get("type") != int(_ACTION.CAMERA_TAKE_PHOTO)
                or request.occurred_at != event.occurred_at or len(event.rows) != 2):
            raise ValueError("主机拍摄文件申请缺少原动作或两份共同责任")
        if isinstance(request, PrepareHostPhoto) and branch_name != "CREATE":
            raise ValueError("准备申请必须使用 CREATE 分支")
        if isinstance(request, HostPhotoComplete) and branch_name != "HOST_PHOTO_COMPLETE":
            raise ValueError("完整字节申请必须使用 HOST_PHOTO_COMPLETE 分支")
        rows = []
        for row in event.rows:
            if row.table != "intermediate_files":
                raise ValueError("主机拍摄申请只保存两份文件责任")
            if isinstance(request, PrepareHostPhoto):
                if row.before.exists:
                    raise ValueError("准备申请必须创建文件责任")
                facts = {"id": row.row_id, **row.after.values}
            else:
                facts = context.state_rows.get("intermediate_files", {}).get(row.row_id)
                if facts is None:
                    raise ValueError("完整字节申请没有原文件责任")
                is_original = row.row_id == request.original_file_id
                expected = {"size_bytes": request.original_size_bytes if is_original else request.preview_size_bytes,
                            "sha256": request.original_sha256 if is_original else request.preview_sha256}
                if not row.before.exists or not json_equal(row.after.values, expected):
                    raise ValueError("完整字节事件与申请不同")
            if facts.get("owner_action_id") != request.action_id:
                raise ValueError("主机拍摄文件责任属于其他动作")
            rows.append(facts)
        files = _files(rows)
        if isinstance(request, HostPhotoComplete) and (
            files.original.file_id != request.original_file_id or files.preview.file_id != request.preview_file_id):
            raise ValueError("完整字节申请交换了原图和预览身份")
    except (ValueError, TypeError, KeyError) as error:
        raise EventValidationError(f"主机拍摄文件事件违反契约: {error}") from error


class _HostPhotoCommand:
    def __init__(self, request, key, format_version):
        self.request, self.key = request, key
        self.format_version = format_version

    def plan(self, scope):
        request = self.request
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            return self.reuse(scope, saved)
        require_host_photo_format(self.format_version)
        action, rows = _load(scope.connection, request.action_id)
        state = {"actions": {action["id"]: action},
                 "intermediate_files": {row["id"]: row for row in rows}}
        if isinstance(request, PrepareHostPhoto):
            if rows:
                return CommandPlan((), {}, state, result=_files(rows), read_only=True)
            if action["status"] not in (1, 2):
                raise ConsistencyError("已结束动作不能新增拍摄文件责任")
            first_id = next_row_id(scope.connection, "intermediate_files")
            ObjectId(first_id + 1)
            changes = []
            for offset, (purpose, extension) in enumerate(_ROLES):
                identity = first_id + offset
                values = {
                    "owner_action_id": request.action_id, "owner_delivery_id": None,
                    "purpose": int(purpose),
                    "relative_path": relative_file_path(FilePurpose[purpose.name], identity, extension),
                    "retention_state": int(_RETENTION.REQUIRED), "cleanup_state": int(_CLEANUP.NOT_NEEDED),
                    "size_bytes": None, "sha256": None, "last_error_json": None,
                }
                changes.append(row_change("intermediate_files", identity, values))
            result = _files([{"id": change.row_id, **change.after.values} for change in changes])
            reason = _CREATE_REASON
        else:
            files = _files(rows)
            if (files.original.file_id, files.preview.file_id) != (request.original_file_id, request.preview_file_id):
                raise ConsistencyError("完整字节事实指向其他拍摄文件责任")
            changes = []
            for row, size, digest in (
                (state["intermediate_files"][request.original_file_id], request.original_size_bytes, request.original_sha256),
                (state["intermediate_files"][request.preview_file_id], request.preview_size_bytes, request.preview_sha256),
            ):
                if row["size_bytes"] is not None or row["sha256"] is not None:
                    if row["size_bytes"] != size or row["sha256"] != digest:
                        raise ConsistencyError("主机拍摄源已有完整字节事实与申请矛盾")
                    continue
                if row["retention_state"] != int(_RETENTION.REQUIRED):
                    raise ConsistencyError("已提升文件不能补造缺失的完整字节事实")
                changes.append(update_change("intermediate_files", row["id"],
                    {"size_bytes": None, "sha256": None}, {"size_bytes": size, "sha256": digest}))
            if not changes:
                return CommandPlan((), {}, state, result=files, read_only=True)
            if len(changes) != 2:
                raise ConsistencyError("两份拍摄源的完整字节必须共同保存")
            result, reason = files, _HOST_PHOTO_COMPLETE_REASON
        allocation = scope.allocate(1)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 26, reason,
            changes, request.occurred_at, evidence={"host_photo_request": asdict(request)})
        owners = {("intermediate_files", change.row_id): ("intermediate_file", change.row_id) for change in changes}
        return CommandPlan((event,), owners, state, result=result)

    def reuse(self, scope, saved):
        request = self.request
        reason = (
            _CREATE_REASON
            if isinstance(request, PrepareHostPhoto)
            else _HOST_PHOTO_COMPLETE_REASON
        )
        if (len(saved) != 1 or saved[0]["type"] != 26 or saved[0]["reason"] != reason
                or saved[0]["occurred_at"] != request.occurred_at
                or not json_equal(saved[0]["body"]["evidence"], {"host_photo_request": asdict(request)})):
            raise TransactionError("原主机拍摄文件申请与重送输入不一致")
        action, current_rows = _load(scope.connection, request.action_id)
        files = _files(current_rows)
        event = saved[0]
        changes = event["body"]["rows"]
        if len(changes) != 2 or {row["id"] for row in changes} != {files.original.file_id, files.preview.file_id}:
            raise ConsistencyError("原主机拍摄文件事务没有共同保存两份原责任")
        current = {row["id"]: row for row in current_rows}
        for row in changes:
            if row["table"] != "intermediate_files" or not row["after"]["exists"]:
                raise ConsistencyError("原主机拍摄文件事务含有其他记录")
            history = read_row_values_at_boundary(scope.connection,
                owner=("intermediate_file", row["id"]), table="intermediate_files", row_id=row["id"],
                columns=frozenset(row["after"]["values"]), current_values=current[row["id"]],
                boundary=HistoryBoundary(event["transaction"].txn_id, event["transaction"].last_event_id),
                current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
            if not json_equal(history, row["after"]["values"]):
                raise ConsistencyError("原主机拍摄文件事实与可靠历史不一致")
        return CommandPlan((), {}, {"actions": {action["id"]: action}}, result=files, read_only=True)


class HostCaptureRepository:
    def prepare_host_photo(self, action_id, occurred_at, key, owned):
        request = PrepareHostPhoto(action_id, occurred_at)
        return self._write(request, key, owned)

    def save_host_photo_complete(self, request, key, owned):
        if not isinstance(request, HostPhotoComplete):
            raise TypeError("完整文件申请必须使用 HostPhotoComplete")
        parsed = parse_host_photo_request(asdict(request), reason=2)
        return self._write(parsed, key, owned)

    @staticmethod
    def _write(request, key, owned):
        receipt = commit_operation(
            _HostPhotoCommand(request, key, owned.metadata.format_version), key, owned
        )
        return DbOutcome(DbOutcomeKind(receipt.kind), receipt.result, error=receipt.error, boundary=receipt.boundary)

    def verify_operation(self, request, key, owned):
        """新连接只核实原完整提交；原键不存在时可靠返回未提交。"""
        if not isinstance(request, (PrepareHostPhoto, HostPhotoComplete)):
            raise TypeError("核实要求原完整主机拍摄文件申请")
        connection = owned.connection
        reading = False
        try:
            if connection.in_transaction:
                raise TransactionError("原提交核实要求独立连接")
            connection.execute("BEGIN").close()
            reading = True
            saved = saved_transaction_events(connection, key)
            if saved is None:
                result = DbOutcome(DbOutcomeKind.ROLLED_BACK, error=TransactionError("原申请可靠未提交"), stage="verification")
            else:
                with closing(connection.execute("SELECT MAX(id), MAX(last_event_id) FROM history_transactions")) as cursor:
                    latest = cursor.fetchone()
                plan = _HostPhotoCommand(
                    request, key, owned.metadata.format_version
                ).reuse(TransactionScope(connection, *latest), saved)
                transaction = saved[0]["transaction"]
                result = DbOutcome(DbOutcomeKind.COMPLETED, plan.result,
                    boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id), stage="verification")
            connection.execute("COMMIT").close()
            reading = False
            return result
        except (sqlite3.Error, ValueError, TypeError) as error:
            if reading:
                try:
                    connection.execute("ROLLBACK").close()
                except sqlite3.Error as rollback_error:
                    error = rollback_error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=error, stage="verification")
