"""照片缓存读取绑定直接文件身份及关闭责任；不访问文件系统。"""
import importlib.util
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    class Settlement(Enum): WAITING = "waiting"; UNCONFIRMED = "unconfirmed"
    class PreparationError(ValueError):
        def __init__(self, message, *, settlement):
            super().__init__(message); self.settlement = settlement
    read = ModuleType("camctl.devices.read_session")
    read.ReadChunk = read.ReadEnd = object
    read.ReadPreparationError, read.ReadPreparationSettlement = PreparationError, Settlement
    models = ModuleType("camctl.host_files.models"); models.BoundDirectories = models.FileRef = object
    paths = ModuleType("camctl.host_files.paths"); paths.resolve_file = lambda *args: None
    segments = ModuleType("camctl.host_files.segments"); segments.DEFAULT_CHUNK_SIZE_BYTES = 1024
    for stub in (read, models, paths, segments): monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).parents[3] / "src/camctl/host_files/io.py"
    name = "camctl.host_files._isolated_photo_cache_reader"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, Settlement, PreparationError


class Owner:
    def __init__(self, close_error=None):
        self.fd, self.claimed, self.close_error, self.close_calls = None, False, close_error, 0
    def claim(self): self.claimed = True; return True
    def close_file(self):
        self.close_calls += 1
        if self.close_error is not None: raise self.close_error
        self.fd = None
    def release(self):
        if self.fd is None: self.claimed = False


def configure(module, *, opened_size=100, named_mode=0o100644):
    opened = SimpleNamespace(st_mode=0o100644, st_size=opened_size, st_dev=1, st_ino=2)
    named = SimpleNamespace(st_mode=named_mode, st_size=opened_size, st_dev=1, st_ino=2)
    module.os = SimpleNamespace(O_RDONLY=1, O_NOFOLLOW=2, O_NONBLOCK=4, SEEK_SET=0,
        open=lambda *args: 17, fstat=lambda fd: opened, stat=lambda *args, **kwargs: named,
        lseek=lambda *args: None)


def test_photo_cache_reader_holds_original_fd_until_close(api):
    module, _, _ = api
    configure(module)
    owner = Owner()
    reader = module.OwnedPhotoCacheReader(Path("/fake/cache"), 0, 100, owner)
    assert owner.fd == 17 and owner.claimed
    reader.close()
    assert not owner.claimed and owner.fd is None and owner.close_calls == 1


@pytest.mark.parametrize("size,mode", [(99, 0o100644), (100, 0o120777)])
def test_photo_cache_rejects_wrong_size_or_link(api, size, mode):
    module, _, _ = api
    configure(module, opened_size=size, named_mode=mode)
    owner = Owner()
    with pytest.raises(module.FileIoError):
        module.OwnedPhotoCacheReader(Path("/fake/cache"), 0, 100, owner)
    assert not owner.claimed and owner.close_calls == 1


def test_failed_close_during_photo_cache_open_retains_original_owner(api):
    module, settlement, error_type = api
    configure(module, opened_size=99)
    owner = Owner(OSError("close failed"))
    with pytest.raises(error_type) as caught:
        module.OwnedPhotoCacheReader(Path("/fake/cache"), 0, 100, owner)
    assert caught.value.settlement is settlement.UNCONFIRMED
    assert owner.claimed and owner.fd == 17 and owner.close_calls == 1
