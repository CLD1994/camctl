"""原角色页恢复不读取资源、文件或数据库。"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    values = ModuleType("camctl.contracts.values")
    values.ConsistencyError = ValueError
    pages = ModuleType("camctl.persistence.repositories.result_pages")
    saved = {}
    pages._at = lambda connection, event_id: (saved[event_id], {})
    for module in (values, pages):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    path = Path(__file__).parents[3] / "src/camctl/capture/photo_result_recovery.py"
    name = "camctl.capture._isolated_photo_page_recovery"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, saved


class FakeCursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def close(self): pass


class FakeConnection:
    def __init__(self, rows): self.rows, self.arguments = rows, None
    def execute(self, sql, arguments):
        self.arguments = arguments
        return FakeCursor(self.rows)


def page(event, role):
    ticket = SimpleNamespace(run_id=9, target_id="7")
    ref = SimpleNamespace(ticket=ticket, event_id=event)
    observation = SimpleNamespace(type="result_files_listed", version=3, data={"role": role})
    result = SimpleNamespace(entries=(object(),), outcome=SimpleNamespace(observations=(observation,)))
    return SimpleNamespace(ref=ref, page=result)


def test_saved_roles_are_returned_even_without_file_completion(api):
    module, saved = api
    saved.update({10: page(10, "original"), 20: page(20, "thumbnail")})
    connection = FakeConnection([(10,), (20,)])
    refs = module.saved_photo_page_refs(connection, action_id=1, activity_id=7, run_id=9)
    assert [ref.event_id for ref in refs] == [10, 20]
    assert connection.arguments == (1, 7, 9)


def test_saved_duplicate_role_cannot_authorize_another_source(api):
    module, saved = api
    saved.update({10: page(10, "original"), 20: page(20, "original")})
    with pytest.raises(ValueError):
        module.saved_photo_page_refs(FakeConnection([(10,), (20,)]), action_id=1, activity_id=7, run_id=9)
