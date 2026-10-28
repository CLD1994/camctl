"""首次来源下载目录与原 RESULTS 票据绑定；导入时隔离资源。"""
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def api(monkeypatch):
    @dataclass(frozen=True)
    class Ticket:
        attempt_id: int
        operation: str
        target_id: str
        responsibility_key: str
        run_id: int
    values = ModuleType("camctl.contracts.values")
    def object_id(value):
        if type(value) is not int or not 0 < value <= 9223372036854775807:
            raise ValueError("invalid id")
        return value
    def parse_id(value):
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or value.startswith("0"):
            raise ValueError("invalid id")
        return object_id(int(value))
    values.ObjectId, values.parse_object_id = object_id, parse_id
    segments = ModuleType("camctl.host_files.segments")
    segments.DEFAULT_CHUNK_SIZE_BYTES = 1024
    models = ModuleType("camctl.operations.models")
    models.AttemptTicket = Ticket
    for stub in (values, segments, models):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    source = Path(__file__).parents[3] / "src/camctl/host_files/downloads.py"
    name = "camctl.host_files._isolated_photo_downloads"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, Ticket


@pytest.mark.parametrize("role", ["original", "thumbnail"])
def test_result_workspace_uses_exact_result_attempt_and_role(api, role):
    module, Ticket = api
    ticket = Ticket(2, "result", "7", "results/7", 9)
    workspace = module.ResultDownloadWorkspace(Path("/staging"), ticket, role)
    assert workspace.path == Path(f"/staging/result-downloads/9-2-{role}")
    assert workspace.ticket is ticket and not workspace.owned and not workspace.claimed
    with pytest.raises(ValueError):
        workspace.remove()


@pytest.mark.parametrize("operation,target,responsibility,role", [
    ("read", "7", "results/7", "original"),
    ("result", "7", "results/8", "original"),
    ("result", "7", "results/7", "../original"),
    ("result", "07", "results/07", "original"),
    ("result", "9223372036854775808", "results/9223372036854775808", "original"),
])
def test_result_workspace_rejects_unowned_or_noncanonical_path(api, operation, target, responsibility, role):
    module, Ticket = api
    with pytest.raises(ValueError):
        module.result_download_path(Path("/staging"), Ticket(2, operation, target, responsibility, 9), role)


def test_result_workspace_retains_ownership_when_any_close_is_unconfirmed(api):
    module, Ticket = api
    ticket = Ticket(2, "result", "7", "results/7", 9)
    workspace = module.ResultDownloadWorkspace(Path("/staging"), ticket, "original")
    assert workspace.claim()
    workspace.opened_file(10)
    workspace.opened_file(11)
    workspace.closed_file(10, None)
    error = OSError("close failed")
    workspace.closed_file(11, error)
    workspace.release()
    assert not workspace.files_closed
    assert workspace.close_error is error
    assert module.download_owned(ticket)


def test_result_cleanup_retains_owner_when_recursive_close_is_unconfirmed(api, monkeypatch):
    """原关闭失败必须阻止同票据的后续读取或清理。"""
    from types import SimpleNamespace
    module, Ticket = api
    ticket = Ticket(2, "result", "7", "results/7", 9)
    monkeypatch.setattr(type(Path("/staging")), "lstat", lambda self: SimpleNamespace(st_mode=0o40700))
    error = OSError(5, "close failed", "/staging/result-downloads/9-2-original")
    def remove(path, *, onerror):
        onerror(module.os.close, path, (OSError, error, None))
    module.shutil = SimpleNamespace(rmtree=remove)
    async def immediate(function, *args, **kwargs):
        return function(*args)
    module.settled_file_io = immediate
    diagnostic = ModuleType("camctl.operations.error_diagnostics")
    diagnostic.attach_diagnostic_facts = lambda actual, facts: None
    monkeypatch.setitem(sys.modules, diagnostic.__name__, diagnostic)
    operation = module.recover_result_download(Path("/staging"), ticket, "original")
    with pytest.raises(OSError) as actual:
        operation.send(None)
    assert actual.value is error
    assert module.download_owned(ticket)
    assert not module.ResultDownloadWorkspace(Path("/staging"), ticket, "original").claim()
