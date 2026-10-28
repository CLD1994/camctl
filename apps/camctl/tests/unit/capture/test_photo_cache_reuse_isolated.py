"""照片缓存 H 截点与等待者使用受控历史替身。"""
import importlib.util
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    class ActionType(IntEnum): CAMERA_TAKE_PHOTO = 1
    class OutputKind(IntEnum): ORIGINAL = 1; PREVIEW = 2
    class Purpose(IntEnum): CAPTURE_ORIGINAL = 1; CAPTURE_PREVIEW = 2
    class Status(IntEnum): SUCCEEDED = 3; FAILED = 4; CANCELED = 5
    class Retention(IntEnum): REQUIRED = 1
    class Cleanup(IntEnum): NOT_NEEDED = 1
    class RunKind(IntEnum): CHECK_CAPTURE_RESULTS = 4
    class Role(IntEnum): ORIGINAL = 1; PREVIEW = 2
    class Completion(IntEnum): COMPLETE = 3
    enums = ModuleType("camctl.contracts.enums")
    enums.enum_for = lambda name: {"actions.type": ActionType, "outputs.kind": OutputKind,
        "intermediate_files.purpose": Purpose, "actions.status": Status,
        "intermediate_files.retention_state": Retention, "intermediate_files.cleanup_state": Cleanup,
        "operation_runs.kind": RunKind, "device_files.role": Role,
        "device_files.completion_state": Completion}[name]
    @dataclass(frozen=True)
    class Boundary:
        txn_id: int
        last_event_id: int
    history = ModuleType("camctl.contracts.history_values"); history.HistoryBoundary = Boundary
    json_values = ModuleType("camctl.contracts.json_values"); json_values.json_equal = lambda a, b: a == b
    values = ModuleType("camctl.contracts.values"); values.ConsistencyError = ValueError
    requests = ModuleType("camctl.history.photo_cache_request")
    requests.parse_photo_cache_request = lambda raw: SimpleNamespace(**raw)
    validators = ModuleType("camctl.history.validators")
    validators.validate_event_structure = lambda event: event.kind
    facts = ModuleType("camctl.persistence.fact_reads")
    records, states = {}, {}
    facts.read_event = lambda connection, event_id: records[event_id]
    facts.iter_transaction_events = lambda connection, transaction: transaction.events
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, identity: states.get((table, identity))
    reuse = ModuleType("camctl.outputs.segment_copy_reuse")
    seen = []
    reuse._read_scope = lambda connection: nullcontext()
    reuse._selection_is_bound_at_boundary = lambda **kwargs: (seen.append(kwargs) or (kwargs["boundary"], True))
    capture = ModuleType("camctl.persistence.repositories.capture_facts")
    pages = ModuleType("camctl.persistence.repositories.result_pages")
    entry = SimpleNamespace(identity="identity", size_bytes=100, locator={"camera": "original"})
    observations = (SimpleNamespace(type="result_files_listed", version=3, data={"role": "original"}),
        SimpleNamespace(type="photo_result_attempt", version=2,
                        data={"role": "original", "comparison": {"selected": "sync"}}))
    page = SimpleNamespace(ref=SimpleNamespace(ticket=SimpleNamespace(run_id=10)),
        page=SimpleNamespace(entries=(entry,), outcome=SimpleNamespace(observations=observations)),
        file_ids=(("identity", 3),))
    pages.read_saved_page = lambda connection, event_id: (page, {})
    source_transactions = ModuleType("camctl.persistence.repositories.source_transactions")
    source_transactions._created_before = lambda *args: "1"
    definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    definitions.DRIVER_ID = "hasselblad_cfv100c"
    for stub in (enums, history, json_values, values, requests, validators, facts, transaction, reuse, capture, pages, definitions, source_transactions):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).parents[3] / "src/camctl/outputs/photo_cache_reuse.py"
    name = "camctl.outputs._isolated_photo_cache_reuse"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, states, records, seen


class Cursor:
    def __init__(self, values): self.values = values
    def fetchall(self): return self.values
    def fetchone(self): return self.values[0]
    def close(self): pass


class Connection:
    def __init__(self, selection_existed=True): self.selection_existed = selection_existed
    def execute(self, sql, parameters):
        if "SELECT EXISTS" in sql:
            return Cursor([(int(self.selection_existed),)])
        return Cursor([(2,)] if "FROM intermediate_files" in sql else [(8,)])


def test_photo_h_uses_whole_transaction_when_output_event_is_inside_it(api):
    module, states, records, seen = api
    action = {"id": 1, "type": 1, "driver_id": "hasselblad_cfv100c", "execution_spec_json": {"output_storage": "device"}}
    output = {"id": 4, "source_action_id": 1, "device_file_id": 3, "intermediate_file_id": None,
              "kind": 1, "created_event_id": 11}
    file = {"id": 2, "retention_state": 1, "cleanup_state": 1, "size_bytes": 100,
            "sha256": "a" * 64, "relative_path": "cache/2.3fr", "last_error_json": None}
    device = {"source_action_id": 1, "role": 1, "completion_state": 3, "size_bytes": 100,
              "sha256": "a" * 64, "locator_json": {"camera": "original"},
              "completion_evidence_json": {"observation": {"photo_source": {
                  "result_page_event_id": 7, "role": "original"}}}}
    states.update({("actions", 1): action, ("outputs", 4): output, ("intermediate_files", 2): file,
                   ("device_files", 3): device, ("operation_runs", 10): {"action_id": 1, "kind": 4}})
    row = SimpleNamespace(table="outputs", row_id=4, before=SimpleNamespace(exists=False),
        after=SimpleNamespace(exists=True, values=output))
    output_event = SimpleNamespace(kind=("OUTPUT_REGISTERED", "ORIGINAL"), rows=(row,))
    action_row = SimpleNamespace(table="actions", row_id=1, after=SimpleNamespace(values={"status": 3}))
    finish = SimpleNamespace(kind=("ACTION_FINISHED", "SUCCEED"), rows=(action_row,))
    transaction = SimpleNamespace(txn_id=9, first_event_id=10, last_event_id=15,
                                  events=(SimpleNamespace(event=finish), SimpleNamespace(event=output_event)))
    records[11] = SimpleNamespace(event=output_event, transaction=transaction)
    request = {"archive_error": None, "action_id": 1, "file_id": 2, "device_file_id": 3,
               "size_bytes": 100, "sha256": "a" * 64, "result_page_event_id": 7, "selected": "sync", "occurred_at": 88}
    cache_row = SimpleNamespace(table="intermediate_files", row_id=2,
        before=SimpleNamespace(exists=True, values={"size_bytes": None, "sha256": None}),
        after=SimpleNamespace(exists=True, values={"size_bytes": 100, "sha256": "a" * 64}))
    cache_event = SimpleNamespace(kind=("INTERMEDIATE_FILE_CHANGED", "LIFECYCLE"), event_id=8,
                                  evidence={"photo_cache_request": request}, rows=(cache_row,), occurred_at=88)
    records[8] = SimpleNamespace(event=cache_event, transaction=SimpleNamespace(first_event_id=8, last_event_id=8))
    records[7] = SimpleNamespace(event=SimpleNamespace(kind=("RESULT_PAGE", "APPEND")),
                                 transaction=SimpleNamespace(last_event_id=7))
    result = module.read_bound_photo_cache(output_id=4, obtain_action_id=5, selection_id=6,
                                           owned=SimpleNamespace(connection=Connection()))
    assert result.intermediate_file_id == 2
    assert result.published_boundary.last_event_id == 15
    assert seen[0]["boundary"].last_event_id == 15
    assert module.read_bound_photo_cache(output_id=4, obtain_action_id=5, selection_id=6,
        owned=SimpleNamespace(connection=Connection(selection_existed=False))) is None
    assert len(seen) == 1
    module._selection_is_bound_at_boundary = lambda **kwargs: (kwargs["boundary"], False)
    assert module.read_bound_photo_cache(output_id=4, obtain_action_id=5, selection_id=6,
                                        owned=SimpleNamespace(connection=Connection())) is None
