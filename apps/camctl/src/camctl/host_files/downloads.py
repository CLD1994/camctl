"""原 READ 票据拥有的临时下载；不持有业务数据库。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import stat
import threading
import re
from itertools import islice

from camctl.contracts.values import ObjectId, parse_object_id
from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES
from camctl.operations.models import AttemptTicket

_ACTIVE: dict[Path, DownloadWorkspace] = {}
_ACTIVE_LOCK = threading.Lock()


def download_owned(ticket: AttemptTicket, staging: Path | None = None):
    """查询本进程实际拥有者；未绑定目录时仍保留已知原票据的责任。"""
    with _ACTIVE_LOCK:
        if staging is not None:
            return download_path(staging, ticket) in _ACTIVE
        return any(owner.ticket == ticket for owner in _ACTIVE.values())


def result_download_owned(staging: Path, ticket: AttemptTicket, role: str) -> bool:
    with _ACTIVE_LOCK:
        return result_download_path(staging, ticket, role) in _ACTIVE


def result_role_owned(activity_id: str, role: str) -> bool:
    with _ACTIVE_LOCK:
        return any(owner.ticket.operation == "result" and owner.ticket.target_id == activity_id
                   and getattr(owner, "role", None) == role for owner in _ACTIVE.values())


async def recover_result_download(staging: Path, ticket: AttemptTicket, role: str):
    """上层核实原尝试及保存责任后，回收固定角色的独占目录。"""
    workspace = ResultDownloadWorkspace(staging, ticket, role)
    if not workspace.claim():
        return False
    try:
        workspace.owned = True
        def remove():
            try:
                parent = workspace.path.parent.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(parent.st_mode):
                raise ValueError("结果下载父目录不是直接普通目录")
            workspace.recover_remove()
        await settled_file_io(remove)
        return True
    finally:
        workspace.release()


async def load_download_cursor(staging: Path):
    """只读固定长度的维护游标；游标不授权删除任何目录。"""
    def load():
        try:
            fd = os.open(staging / ".read-download-cleanup-cursor",
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return 0
        with os.fdopen(fd, "rb") as stream:
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode) or observed.st_size > 32:
                raise ValueError("下载维护游标不是有限普通文件")
            value = stream.read(33)
        if re.fullmatch(rb"[0-9]{1,19}\n", value) is None:
            raise ValueError("下载维护游标格式不符")
        return int(value)
    return await settled_file_io(load)


async def save_download_cursor(staging: Path, after: int):
    """原责任无论保留或处理都推进；完整替换后下一 run 才能继续。"""
    def save():
        temporary = staging / ".read-download-cleanup-cursor.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"{after}\n".encode("ascii"))
        os.replace(temporary, staging / ".read-download-cleanup-cursor")
    await settled_file_io(save)


async def recover_download(staging: Path, ticket: AttemptTicket):
    """上层已核实停止与原归属；当前进程持有资源时仍拒绝回收。"""
    workspace = DownloadWorkspace(staging, ticket)
    if not workspace.claim():
        return False
    try:
        workspace.owned = True
        await workspace.cleanup()
        return True
    finally:
        workspace.release()


def download_path(staging: Path, ticket: AttemptTicket) -> Path:
    if not isinstance(ticket, AttemptTicket) or ticket.operation != "read":
        raise ValueError("下载工作目录必须携带原 READ 票据")
    ObjectId(ticket.run_id)
    ObjectId(ticket.attempt_id)
    if ticket.target_id is None:
        raise ValueError("READ 必须携带原副本身份")
    ObjectId(int(ticket.target_id))
    if ticket.responsibility_key != f"read/{ticket.target_id}":
        raise ValueError("READ 责任键与原副本身份不符")
    return staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"


def result_download_path(staging: Path, ticket: AttemptTicket, role: str) -> Path:
    """由原 RESULTS 尝试和实际读取角色确定独占工作目录。"""
    if not isinstance(ticket, AttemptTicket) or ticket.operation != "result":
        raise ValueError("来源下载必须属于原 RESULTS 尝试")
    ObjectId(ticket.run_id)
    ObjectId(ticket.attempt_id)
    parse_object_id(ticket.target_id)
    if ticket.responsibility_key != f"results/{ticket.target_id}":
        raise ValueError("来源下载责任与原活动不符")
    if role not in ("original", "thumbnail"):
        raise ValueError("来源下载角色无效")
    return staging / "result-downloads" / f"{ticket.run_id}-{ticket.attempt_id}-{role}"


async def settled_file_io(function, *args, dispose=None):
    """取消后仍等待实际文件操作；新打开资源按原结果关闭。"""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as error:
            cancellation = error if cancellation is None else cancellation
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
    if cancellation is not None:
        if dispose is not None:
            try:
                await settled_file_io(dispose, result)
            except BaseException as error:
                raise cancellation from error
        raise cancellation
    return result


class DownloadWorkspace:
    """先独占创建再使用；仅删除当前拥有或上层已授权恢复的目录。"""

    def __init__(self, staging: Path, ticket: AttemptTicket):
        self._initialize(download_path(staging, ticket), ticket)

    def _initialize(self, path: Path, ticket: AttemptTicket):
        self.path = path
        self.ticket = ticket
        self.owned = False
        self.claimed = False
        self.fd = None
        self.close_error = None

    @property
    def files_closed(self):
        return self.fd is None

    async def prepare(self):
        if not self.claim():
            raise ValueError("原下载仍有实际进程或文件读取拥有者")
        await settled_file_io(self._prepare)
        return self.path

    def claim(self):
        with _ACTIVE_LOCK:
            if self.claimed:
                return True
            if self.path in _ACTIVE:
                return False
            _ACTIVE[self.path] = self
            self.claimed = True
            return True

    def release(self):
        with _ACTIVE_LOCK:
            if self.claimed and self.files_closed:
                _ACTIVE.pop(self.path, None)
                self.claimed = False

    def _parent(self):
        # staging 由部署绑定；本模块拒绝跟随自身管理目录的符号链接。
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("下载父目录不是直接普通目录")

    def _prepare(self):
        self._parent()
        self.path.mkdir(mode=0o700)
        self.owned = True

    async def cleanup(self):
        try:
            if self.owned:
                await settled_file_io(self.remove)
        finally:
            self.release()

    def remove(self):
        """调用者已证明相关进程及文件读取实际结束。"""
        if not self.files_closed:
            raise OSError("原下载文件关闭未确认，保留拥有者与目录") from self.close_error
        try:
            observed = self.path.lstat()
        except FileNotFoundError:
            self.owned = False
            return
        if not stat.S_ISDIR(observed.st_mode):
            raise ValueError("原下载工作目录类型不符")
        shutil.rmtree(self.path)
        self.owned = False

    async def open_verified(self, size: int, sha256: str, offset: int):
        return await settled_file_io(self._open_verified, size, sha256, offset,
                                     dispose=lambda stream: stream.close())

    def _open_verified(self, size, sha256, offset):
        with os.scandir(self.path) as entries:
            names = [entry.name for entry in islice(entries, 2)]
        if len(names) != 1:
            raise ValueError("指定下载必须恰好生成一个普通文件")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(self.path / names[0], flags)
        self.fd = fd
        try:
            observed = os.fstat(fd)
            if not stat.S_ISREG(observed.st_mode) or observed.st_size != size:
                raise ValueError("下载文件类型或完整长度与固定源不符")
            digest = hashlib.sha256()
            while data := os.read(fd, DEFAULT_CHUNK_SIZE_BYTES):
                digest.update(data)
            if digest.hexdigest() != sha256:
                raise ValueError("下载内容与拍摄时固定的 SHA-256 不符")
            os.lseek(fd, offset, os.SEEK_SET)
            return DownloadStream(self)
        except BaseException as error:
            try:
                self.close_file()
            except BaseException as close_error:
                raise BaseExceptionGroup("下载核验与关闭均失败", [error, close_error]) from None
            raise

    def close_file(self):
        """关闭出错后 fd 是否已复用未知；保留责任，不重复调用 os.close。"""
        if self.close_error is not None:
            raise self.close_error
        if self.fd is not None:
            try:
                os.close(self.fd)
            except BaseException as error:
                self.close_error = error
                raise
            self.fd = None


class ResultDownloadWorkspace(DownloadWorkspace):
    """原来源观察目录；删除责任须由结果及后续文件事实单独授权。"""

    def __init__(self, staging: Path, ticket: AttemptTicket, role: str):
        self._initialize(result_download_path(staging, ticket, role), ticket)
        self.role = role
        self._observed_fds = set()
        self._close_errors = []
        self._unconfirmed_close = False

    @property
    def files_closed(self):
        return not self._observed_fds and not self._unconfirmed_close

    def opened_file(self, fd):
        self._observed_fds.add(fd)

    def closed_file(self, fd, error):
        if error is None:
            self._observed_fds.discard(fd)
        else:
            self._close_errors.append(error)
            if self.close_error is None:
                self.close_error = error

    @property
    def close_errors(self):
        return tuple(self._close_errors)

    def remove(self):
        raise ValueError("来源观察目录须沿已保存的文件责任清理")

    def recover_remove(self):
        """原尝试已授权后，登记递归删除内部的实际关闭失败。"""
        if not self.files_closed:
            raise OSError("原来源工作文件关闭未确认") from self.close_error
        try:
            observed = self.path.lstat()
        except FileNotFoundError:
            self.owned = False
            return
        if not stat.S_ISDIR(observed.st_mode):
            raise ValueError("原来源工作目录不是直接普通目录")
        close_paths = []
        def failed(function, path, exc_info):
            error = exc_info[1]
            if function is os.close:
                self._unconfirmed_close = True
                self._close_errors.append(error)
                close_paths.append(str(path))
                if self.close_error is None:
                    self.close_error = error
                error.add_note(f"来源目录关闭失败，路径: {path}")
            raise error
        try:
            shutil.rmtree(self.path, onerror=failed)
        except BaseException as error:
            if self._close_errors:
                from camctl.operations.error_diagnostics import attach_diagnostic_facts
                attach_diagnostic_facts(error, {"photo_cleanup_close_errors": self.close_errors,
                                                "photo_cleanup_close_paths": close_paths})
            raise
        self.owned = False

    async def observe(self, media_type: str):
        return await settled_file_io(self._observe, media_type)

    def _observe(self, media_type: str):
        from camctl.host_files.capture_sources import (
            CapturedArtifact, CaptureFileError, _open_directory, _read_file_facts_at,
            _stat_entry, _sync_directory_fd,
        )
        if not self.owned or not self.claimed:
            raise ValueError("来源观察目录没有当前实际拥有者")
        with _open_directory(self.path, "source_workspace_open", owner=self) as directory_fd:
            with os.scandir(directory_fd) as entries:
                names = [entry.name for entry in islice(entries, 2)]
            if len(names) != 1:
                raise CaptureFileError("source_file_count", self.path,
                                       detail="指定下载必须生成唯一文件")
            name = names[0]
            path = self.path / name
            observed = _stat_entry(directory_fd, name, path, "source_observe")
            if observed is None or not stat.S_ISREG(observed.st_mode) or observed.st_size <= 0:
                raise CaptureFileError("source_type", path, detail="下载不是非空普通文件")
            facts = _read_file_facts_at(directory_fd, name, path, media_type,
                expected_size=observed.st_size, expected_sha256=None, file_kind="source", owner=self)
            _sync_directory_fd(directory_fd, self.path, "source_workspace_sync")
            return CapturedArtifact(name, facts.size_bytes, facts.sha256, media_type)


class CaptureCleanupWorkspace(ResultDownloadWorkspace):
    """清理原 START 专属采集目录时持有实际文件操作责任。"""

    def __init__(self, staging: Path, ticket: AttemptTicket):
        if (not isinstance(ticket, AttemptTicket)
                or not ((ticket.operation == "control" and ticket.responsibility_key.startswith("start/"))
                        or (ticket.operation == "result" and ticket.responsibility_key.startswith("results/")))):
            raise ValueError("采集目录清理必须属于原 START 或 RESULTS 票据")
        parse_object_id(ticket.target_id)
        parse_object_id(ticket.responsibility_key.split("/", 1)[1])
        self._initialize(staging / "capture-work" / ticket.target_id, ticket)
        self.role = None
        self._observed_fds = set()
        self._close_errors = []
        self._unconfirmed_close = False

    def claim(self):
        with _ACTIVE_LOCK:
            if self.claimed:
                return True
            if self.path in _ACTIVE or any(
                    owner.ticket.operation in ("control", "result")
                    and owner.ticket.target_id == self.ticket.target_id for owner in _ACTIVE.values()):
                return False
            _ACTIVE[self.path] = self
            self.claimed = True
            return True


class PhotoCacheReadWorkspace(ResultDownloadWorkspace):
    """原 READ 票据拥有照片缓存文件和用途目录的实际关闭责任。"""

    def __init__(self, staging: Path, ticket: AttemptTicket):
        self._initialize(download_path(staging, ticket), ticket)
        self.role = None
        self._observed_fds = set()
        self._close_errors = []
        self._unconfirmed_close = False
        self._file_close_error = None

    def claim(self):
        return DownloadWorkspace.claim(self)

    def close_file(self):
        if self._file_close_error is not None:
            raise self._file_close_error
        if self.fd is None:
            return
        fd = self.fd
        try:
            os.close(fd)
        except BaseException as error:
            self._file_close_error = error
            self.closed_file(fd, error)
            raise
        self.closed_file(fd, None)
        self.fd = None


class DownloadStream:
    """经摘要核验的有限缓冲读取；close 成功才证明临时副本回收。"""

    def __init__(self, workspace):
        self.workspace = workspace
        self.stopped = threading.Event()

    def read(self, limit):
        if self.stopped.is_set():
            return b""
        return os.read(self.workspace.fd, min(limit, DEFAULT_CHUNK_SIZE_BYTES))

    def cancel(self):
        self.stopped.set()

    def close(self):
        self.workspace.close_file()
        try:
            self.workspace.remove()
        finally:
            self.workspace.release()
