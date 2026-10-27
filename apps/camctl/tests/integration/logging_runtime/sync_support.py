"""在真实文件描述符的同步边界观察、暂停或注入故障。"""

import os
import stat
import threading


class FileSyncProbe:
    """仅匹配原日志及其父目录，不干预数据库、标记和副本同步。"""

    def __init__(self, monkeypatch, path, *, block=None, fail=None):
        self.path = path
        self.block, self.fail = block, fail
        self.entered, self.release = threading.Event(), threading.Event()
        self.events = []
        self._fsync = os.fsync
        monkeypatch.setattr(os, "fsync", self._sync)

    def _kind(self, fd):
        opened = os.fstat(fd)
        if stat.S_ISREG(opened.st_mode):
            path, kind = self.path, "file"
        elif stat.S_ISDIR(opened.st_mode):
            path, kind = self.path.parent, "directory"
        else:
            return None
        try:
            named = path.stat()
        except FileNotFoundError:
            return None
        if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
            return kind
        return None

    def _sync(self, fd):
        kind = self._kind(fd)
        if kind is None:
            return self._fsync(fd)
        self.events.append((kind, "started"))
        if kind == self.block and not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("测试未释放原日志同步门闩")
        if kind == self.fail:
            raise OSError(f"controlled original {kind} sync failure")
        result = self._fsync(fd)
        self.events.append((kind, "completed"))
        return result
