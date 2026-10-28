"""扫描配对在隔离协作者下固定边界和核实原操作。"""
import importlib.abc
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

SOURCE = Path(__file__).parents[3] / 'src' / 'camctl' / 'persistence' / 'repositories' / 'result_file_pairing.py'


class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'camctl' or fullname.startswith('camctl.'):
            raise AssertionError(f'禁止加载真实包: {fullname}')


@dataclass(frozen=True)
class Boundary:
    txn_id: int
    last_event_id: int


@dataclass(frozen=True)
class Request:
    preview_file_id: int
    scan: object
    occurred_at: int


class ConsistencyError(Exception): pass
class TransactionError(Exception): pass


def load_pairing():
    modules = {}
    def module(name, **values):
        item = ModuleType(name)
        item.__dict__.update(values)
        modules[name] = item
        return item
    class EnumValues:
        def __getattr__(self, name): return 1
    module('camctl.capture.files', OwnershipFromResultPageSave=object, PairResultFilesSave=Request)
    module('camctl.capture.segment_models', FileCompletionState=object, OwnershipState=object)
    module('camctl.contracts.enums', enum_for=lambda name: EnumValues())
    module('camctl.contracts.history_values', HistoryBoundary=Boundary)
    module('camctl.contracts.json_values', json_equal=lambda a, b: a == b)
    module('camctl.contracts.values', ConsistencyError=ConsistencyError)
    module('camctl.history.events', load_event_registry=lambda: {'events': {'DEVICE_FILE_OBSERVED': {'id': 2, 'branches': {'PAIR_FROM_RESULT_SCAN': {'reason': 3}}}}})
    module('camctl.history.reads', CaptureResultReadScope=type('CaptureResultReadScope', (), {}))
    module('camctl.history.validators', EventValidationError=type('EventValidationError', (Exception,), {}), register_guard=lambda *a: None, validate_event_structure=lambda event: None)
    module('camctl.persistence.event_reads', SqlEventReads=type('SqlEventReads', (), {}))
    module('camctl.persistence.fact_reads', opened_operation_fact_scope=None, read_event=None, read_operation_transaction=None, read_transaction=None)
    module('camctl.persistence.transaction', CommandPlan=lambda events, owners, state, **kw: SimpleNamespace(events=events, owners=owners, state=state, **kw), TransactionError=TransactionError, event_envelope=None, next_row_id=None, read_transaction_range=None, row_change=None, row_facts=None, update_change=None)
    pages = module('camctl.persistence.repositories.result_pages')
    module('camctl.persistence.repositories.result_file_ownership', ownership_guard_boundary=None, ownership_request_from_event=None, request_document=None)
    module('camctl.persistence.repositories.result_file_proofs', read_file_at_boundary=None, read_fixed_file=None, read_result_file_input=None)
    module('camctl.persistence.repositories.capture', _load_related_outputs=lambda *args: None)
    for name in ('camctl', 'camctl.persistence', 'camctl.persistence.repositories'):
        package = module(name)
        package.__path__ = []
    name = 'camctl.persistence.repositories.isolated_pairing'
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: result, **modules}), patch.object(sys, 'meta_path', [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(result)
    return result, pages, modules


def test_first_plan_uses_boundary_inside_write_transaction():
    pairing, pages, modules = load_pairing()
    source = Boundary(8, 27)
    request = Request(11, SimpleNamespace(ticket='ticket', event_id=22), 100)
    seen = []
    pairing.read_operation_transaction = lambda db, key: None
    pages._latest_history_boundary = lambda db: source
    pages.capture_result_scope = lambda *args: 'scope'
    pairing._classify = lambda db, fixed: (seen.append(fixed) or (1, {'id': 11}, {'attempt_id': 2}))
    pairing._one = lambda *args: None
    pairing.next_row_id = lambda *args: 99
    pairing._rows = lambda *args: ()
    pairing.event_envelope = lambda *args: SimpleNamespace(evidence=args[-1])
    pairing.request_document = lambda fixed: {'boundary': fixed.source_boundary}
    scope = SimpleNamespace(connection=object(), max_txn_id=8, max_event_id=27,
                            allocate=lambda n: SimpleNamespace(first_event_id=28, txn_id=9))
    with patch.dict(sys.modules, modules), patch.object(sys, 'meta_path', [BlockCamctl(), *sys.meta_path]):
        plan = pairing.PairResultFilesCommand(request, 'key').plan(scope)
    assert seen[0].source_boundary == source
    assert plan.events[0].evidence['result_scan_pairing_request']['boundary'] == source


def test_replay_uses_saved_boundary_after_later_commits():
    pairing, pages, _ = load_pairing()
    request = Request(11, 'scan', 100)
    original_boundary = Boundary(8, 27)
    original = pairing._FixedPairing.from_request(request, original_boundary)
    transaction = SimpleNamespace(first_event_id=28, last_event_id=28, txn_id=9)
    event = SimpleNamespace(event_type=2, reason=3, transaction_id=9, rows=[SimpleNamespace(row_id=99, after=SimpleNamespace(values={'id': 99}))])
    pairing.read_operation_transaction = lambda db, key: transaction
    pairing.read_event = lambda db, event_id: SimpleNamespace(event=event)
    pairing._request = lambda event: original
    pairing.request_document = lambda fixed: (fixed.preview_file_id, fixed.scan, fixed.occurred_at, fixed.source_boundary)
    pages._page_before_boundary = lambda db, txn: original_boundary
    pairing._check_event = lambda db, event, fixed, **kw: (None, None, None) if fixed is original else pytest.fail('必须核实原边界')
    pairing.row_facts = lambda *args: {'id': 99}
    assert pairing._verify(request, 'key', object()) == 99


def test_replay_rejects_changed_original_input():
    pairing, pages, _ = load_pairing()
    request = Request(11, 'changed scan', 100)
    transaction = SimpleNamespace(first_event_id=28, last_event_id=28, txn_id=9)
    pairing.read_operation_transaction = lambda db, key: transaction
    pairing.read_event = lambda db, event_id: SimpleNamespace(event=SimpleNamespace(event_type=2, reason=3, transaction_id=9))
    pairing._request = lambda event: pairing._FixedPairing(11, 'scan', 100, Boundary(8, 27))
    with pytest.raises(TransactionError):
        pairing._verify(request, 'key', object())


def test_first_plan_rejects_incomplete_or_mismatched_transaction_boundary():
    pairing, pages, _ = load_pairing()
    pairing.read_operation_transaction = lambda db, key: None
    pages._latest_history_boundary = lambda db: Boundary(8, 27)
    scope = SimpleNamespace(connection=object(), max_txn_id=8, max_event_id=28)
    with pytest.raises(ConsistencyError):
        pairing.PairResultFilesCommand(Request(11, 'scan', 100), 'key').plan(scope)


def test_unknown_pairing_does_not_allocate_or_reserve_key():
    pairing, pages, _ = load_pairing()
    pairing.read_operation_transaction = lambda db, key: None
    pages._latest_history_boundary = lambda db: Boundary(8, 27)
    pairing._classify = lambda db, fixed: None
    scope = SimpleNamespace(connection=object(), max_txn_id=8, max_event_id=27,
                            allocate=lambda n: pytest.fail('未知结论不能分配身份'))
    plan = pairing.PairResultFilesCommand(Request(11, 'scan', 100), 'key').plan(scope)
    assert plan.read_only and plan.result is None and plan.events == ()


def test_existing_key_is_verified_before_reading_current_boundary():
    pairing, pages, _ = load_pairing()
    request = Request(11, 'scan', 100)
    pairing.read_operation_transaction = lambda db, key: object()
    pairing._verify = lambda actual, key, db: 99 if actual == request and key == 'key' else pytest.fail('申请或键改变')
    pages._latest_history_boundary = lambda db: pytest.fail('原键不能替换原边界')
    plan = pairing.PairResultFilesCommand(request, 'key').plan(SimpleNamespace(connection=object()))
    assert plan.read_only and plan.result == 99


def test_in_flight_operation_cannot_be_verified_as_missing():
    pairing, _, _ = load_pairing()
    with pytest.raises(ConsistencyError):
        pairing.verify_result_file_pairing(Request(11, 'scan', 100), 'key', object(), execution_finished=False)


@pytest.mark.parametrize('fault', ['boundary', 'transaction', 'event', 'projection'])
def test_replay_rejects_invalid_original_evidence(fault):
    pairing, pages, _ = load_pairing()
    request = Request(11, 'scan', 100)
    source = Boundary(8, 27)
    transaction = SimpleNamespace(first_event_id=28, last_event_id=29 if fault == 'transaction' else 28, txn_id=9)
    event = SimpleNamespace(event_type=999 if fault == 'event' else 2, reason=3, transaction_id=9,
                            rows=[SimpleNamespace(row_id=99, after=SimpleNamespace(values={'id': 99}))])
    pairing.read_operation_transaction = lambda db, key: transaction
    pairing.read_event = lambda db, event_id: SimpleNamespace(event=event)
    pairing._request = lambda event: pairing._FixedPairing.from_request(request, source)
    pairing.request_document = lambda fixed: (fixed.preview_file_id, fixed.scan, fixed.occurred_at, fixed.source_boundary)
    pages._page_before_boundary = lambda db, txn: Boundary(7, 26) if fault == 'boundary' else source
    pairing._check_event = lambda *a, **kw: None
    pairing.row_facts = lambda *args: {'id': 100} if fault == 'projection' else {'id': 99}
    with pytest.raises((ConsistencyError, TransactionError)):
        pairing._verify(request, 'key', object())


def test_guard_rejects_forged_boundary_even_if_classification_would_accept():
    pairing, pages, _ = load_pairing()
    scope = pairing.CaptureResultReadScope()
    reads = pairing.SqlEventReads()
    reads.scope, reads.connection = scope, object()
    context = SimpleNamespace(current_reads=reads, transaction=object())
    pairing._request = lambda event: pairing._FixedPairing(11, 'scan', 100, Boundary(7, 26))
    pairing.ownership_guard_boundary = lambda *args: Boundary(8, 27)
    pages._page_before_boundary = lambda *args: Boundary(8, 27)
    pairing._check_event = lambda *args, **kwargs: pytest.fail('伪造边界不能进入分类')
    with pytest.raises(pairing.EventValidationError):
        pairing._pairing_guard(object(), context)


@pytest.mark.parametrize('preview_first', [False, True])
@pytest.mark.parametrize('case', ['unique', 'missing', 'ambiguous', 'unreliable'])
def test_cross_page_family_classification_uses_complete_scan(preview_first, case):
    pairing, pages, _ = load_pairing()
    pairing._ROLE = SimpleNamespace(ORIGINAL=1, PREVIEW=2)
    pairing._STATE = SimpleNamespace(PAIRED=1, UNPAIRED=2)
    pairing._WHY = SimpleNamespace(MISSING=1, AMBIGUOUS=2, INVALID=3, UNIQUE=4, CONFLICT=5)
    pairing.OwnershipState = SimpleNamespace(OWNED='owned')
    pairing.FileCompletionState = SimpleNamespace(COMPLETE='complete')
    preview = ('A.LRF', 11, 2)
    original = ('A.OSV', 12, 1)
    other = ('A.MP4', 13, 1)
    entries = [preview] if case == 'missing' else [preview, original]
    if case == 'ambiguous': entries.append(other)
    entries.sort(key=lambda item: (item[2] != 2) if preview_first else (item[2] == 2))
    rows = []
    saved = {}
    files = {}
    for index, (identity, file_id, role) in enumerate(entries):
        page_id = 20 + index
        entry = SimpleNamespace(identity=identity, complete=True, size_bytes=10, locator={'id': file_id})
        saved[page_id] = SimpleNamespace(ref=SimpleNamespace(event_id=page_id),
            page=SimpleNamespace(entries=[entry]), file_ids=[(identity, file_id)])
        files[file_id] = {'id': file_id, 'original_device_file_id': None, 'size_bytes': 10, 'locator_json': {'id': file_id}}
        rows.append((file_id, page_id, identity, role))
    class Cursor:
        def __iter__(self): return iter(rows)
        def close(self): pass
    connection = SimpleNamespace(execute=lambda *args: Cursor())
    request = pairing._FixedPairing(11, SimpleNamespace(ticket=SimpleNamespace(run_id=1), event_id=30), 100, Boundary(8, 40))
    pairing._scan = lambda *args: ({'id': 7, 'execution_spec_json': {'result_policy': 'video'}}, {'id': 3})
    pairing.read_file_at_boundary = lambda db, file_id, boundary: files[file_id]
    pairing._one = lambda *args: ('dir', 'A', 2)
    originals = tuple(row[1] for row in entries if row[2] == 1)
    pairing._family_counts = lambda *args: ((len(originals), originals[:2]), (1, (11,)))
    pages.read_saved_page = lambda db, page_id: (saved[page_id], None)
    pages.result_file_family = lambda identity, result_policy: ('dir', 'A', 2 if identity.endswith('LRF') else 1)
    pairing.read_result_file_input = lambda db, page, file_id, boundary: SimpleNamespace(
        ownership='unknown' if case == 'unreliable' and file_id == 11 else 'owned',
        completion='complete', file_event_id=page.event_id + 10)
    result = pairing._classify(connection, request)
    if case == 'unreliable':
        assert result is None
    else:
        values = result[2]
        assert values['state'] == (1 if case == 'unique' else 2)
        assert values['reason'] == {'unique': 4, 'missing': 1, 'ambiguous': 2}[case]
        assert values['preview_page_event_id'] == next(row[1] for row in rows if row[0] == 11)
        assert values['source_txn_id'] == 8 and values['source_event_id'] == 40


def test_event_decoding_preserves_original_request_and_transaction_boundary():
    pairing, _, _ = load_pairing()
    @dataclass(frozen=True)
    class Ownership:
        page: object
        file_id: int
        occurred_at: int
    pairing.OwnershipFromResultPageSave = Ownership
    pairing.ownership_document = lambda request: {'page': request.page}
    pairing.ownership_request = lambda event: Ownership(event.evidence['ownership_from_result_page_request']['page'],
                                                       event.evidence['ownership_from_result_page_request']['file_id'], event.occurred_at)
    request = Request(11, {'scan': 'fixed'}, 100)
    fixed = pairing._FixedPairing.from_request(request, Boundary(8, 27))
    event = SimpleNamespace(occurred_at=100, evidence={'result_scan_pairing_request': pairing.request_document(fixed)})
    decoded = pairing._request(event)
    assert decoded.request() == request and decoded.source_boundary == Boundary(8, 27)
    event.evidence['result_scan_pairing_request']['extra'] = 'forged'
    with pytest.raises(ConsistencyError):
        pairing._request(event)


def test_guard_checks_actual_scope_owner_and_saved_preview():
    pairing, pages, _ = load_pairing()
    scope = pairing.CaptureResultReadScope()
    reads = pairing.SqlEventReads()
    reads.scope, reads.connection = scope, object()
    fixed = pairing._FixedPairing(11, SimpleNamespace(ticket='ticket', event_id=22), 100, Boundary(8, 27))
    preview = {'id': 11, 'original_device_file_id': None, 'pairing_evidence_json': None,
               'source_action_id': 7, 'role': 2}
    row = SimpleNamespace(table='device_file_pairing_facts', row_id=99)
    event = SimpleNamespace(rows=[row])
    context = SimpleNamespace(current_reads=reads, transaction=object(),
                              owners={(row.table, row.row_id): ('device_file', 11)},
                              state_rows={'device_files': {11: dict(preview)}})
    pairing._request = lambda event: fixed
    pairing.ownership_guard_boundary = lambda *args: Boundary(8, 27)
    pages._page_before_boundary = lambda *args: Boundary(8, 27)
    pages.capture_result_scope = lambda *args: scope
    pairing._check_event = lambda db, event, request, **kw: (7, preview, {})
    pairing._pairing_guard(event, context)
    context.state_rows['device_files'][11]['role'] = 1
    with pytest.raises(pairing.EventValidationError):
        pairing._pairing_guard(event, context)


def test_replay_checks_exact_canonical_request_even_when_python_values_compare_equal():
    pairing, pages, _ = load_pairing()
    request = Request(11, True, 100)
    original = pairing._FixedPairing(11, 1, 100, Boundary(8, 27))
    transaction = SimpleNamespace(first_event_id=28, last_event_id=28, txn_id=9)
    event = SimpleNamespace(event_type=2, reason=3, transaction_id=9)
    pairing.read_operation_transaction = lambda *args: transaction
    pairing.read_event = lambda *args: SimpleNamespace(event=event)
    pairing._request = lambda event: original
    pairing.request_document = lambda fixed: {'scan': fixed.scan}
    pairing.json_equal = lambda left, right: type(left['scan']) is type(right['scan']) and left == right
    pages._page_before_boundary = lambda *args: Boundary(8, 27)
    with pytest.raises(TransactionError):
        pairing._verify(request, 'key', object())


def test_missing_key_is_verified_only_after_actual_execution_finishes():
    from contextlib import nullcontext
    pairing, _, _ = load_pairing()
    class Connection:
        in_transaction = False
        def __init__(self): self.commands = []
        def execute(self, command): self.commands.append(command)
    connection = Connection()
    pairing.opened_operation_fact_scope = lambda *args: nullcontext()
    pairing.read_operation_transaction = lambda db, key: None
    assert pairing.verify_result_file_pairing(Request(11, 'scan', 100), 'key',
                                              SimpleNamespace(connection=connection), execution_finished=True) is None
    assert connection.commands == ['BEGIN', 'ROLLBACK']


def test_segment_observations_submits_original_scan_without_reading_boundary():
    modules = {}
    def module(name, names):
        result = ModuleType(name)
        result.__dict__.update({item: object for item in names.split()})
        modules[name] = result
        return result
    files = module('camctl.capture.files', 'FileCompletionSave FilePresenceSave OwnershipFromResultPageSave PairResultFilesSave')
    files.PairResultFilesSave = Request
    module('camctl.capture.media', 'RecordingFailure')
    module('camctl.capture.diagnostics', 'preparation_failure_diagnostic')
    module('camctl.capture.models', 'ActivityReleaseSave')
    module('camctl.capture.segment_errors', 'segment_error_header')
    module('camctl.capture.segment_finish', 'FinishSegmentRecording SegmentFinishResult')
    module('camctl.capture.segment_models', 'CompletionFacts DefaultScopeState EvidenceState SegmentAccumulator SegmentAssessmentReason SegmentAssessmentStatus SegmentSourceKind')
    module('camctl.capture.segments', 'advance_segment_assessment finish_segment_assessment iter_segment_gaps')
    module('camctl.contracts.enums', 'enum_for')
    module('camctl.contracts.values', 'ConsistencyError new_operation_key')
    module('camctl.devices.capture_timing', 'CaptureTiming')
    module('camctl.devices.recording_completion', 'is_completed_file_wait')
    module('camctl.operations.models', 'AttemptStatus').AttemptStatus = SimpleNamespace(UNKNOWN=object())
    module('camctl.persistence.models', 'db_failure DbOutcomeKind db_outcome_for')
    module('camctl.persistence.business', 'SharedSave settle_required')
    module('camctl.persistence.repositories.segments', 'SegmentRepository')
    module('camctl.persistence.repositories.result_sources', 'ResultSourceKind')
    module('camctl.persistence.transaction', 'row_facts')
    module('camctl.logging_runtime.performance', 'measure_performance')
    module('camctl.logging_runtime.transaction_timing', 'db_operation_context')
    module('camctl.logging_runtime.diagnostics', 'DiagnosticState record_error')
    module('camctl.logging_runtime.events', 'record_event')
    scans = module('camctl.capture.result_scans', 'SavedResultEntries')
    module('camctl.persistence.repositories.segment_result_sets', '_boundary')._boundary = lambda db: pytest.fail('新申请不能预读历史边界')
    module('camctl.persistence.repositories.result_file_pairing', 'read_pairing_fact')
    class Entries:
        last_ref = SimpleNamespace(event_id=30)
        async def pages(self):
            yield SimpleNamespace(file_ids=[('clip.LRF', 11)], page=SimpleNamespace(entries=[]))
    scans.SavedResultEntries = Entries
    for name in ('camctl', 'camctl.capture', 'camctl.operations', 'camctl.persistence', 'camctl.persistence.repositories'):
        package = module(name, '')
        package.__path__ = []
    path = SOURCE.parents[1].parent / 'capture' / 'segment_completion.py'
    name = 'camctl.capture.isolated_segment_completion'
    spec = importlib.util.spec_from_file_location(name, path)
    completion = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: completion, **modules}), patch.object(sys, 'meta_path', [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(completion)
        completion.row_facts = lambda *args: {}
        calls = []
        async def save(runtime, action, repository, method, request):
            calls.append((action, method, request))
        completion._save = save
        class Connection:
            def execute(self, query, params):
                assert 'device_file_pairing_facts' in query
                return SimpleNamespace(fetchone=lambda: None)
        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=Connection()))
        ticket = SimpleNamespace(target_id='7', run_id=2, attempt_id=3)
        listing = SimpleNamespace(entries=Entries(), ticket=ticket, scan_complete=True, set_finalized=True)
        runtime = SimpleNamespace(database=Database(), capture=SimpleNamespace(read_result_source=lambda *args: SimpleNamespace(context=None)), wall_us=lambda: 100)
        coroutine = completion.register_segment_observations(runtime, 9, listing)
        with pytest.raises(StopIteration):
            coroutine.send(None)
    assert calls == [(9, 'save_result_file_pairing', Request(11, listing.entries.last_ref, 100))]


def test_public_pairing_request_freezes_scan_and_time_without_boundary():
    modules = {}
    def module(name, **values):
        item = ModuleType(name)
        item.__dict__.update(values)
        modules[name] = item
        return item
    class EnumValues:
        def __getattr__(self, name): return 1
    module('camctl.contracts.enums', enum_for=lambda name: EnumValues())
    module('camctl.contracts.values', ObjectId=int, UtcMicros=int)
    module('camctl.persistence.transaction', encode_json_value=lambda value: '')
    @dataclass(frozen=True)
    class Ticket:
        run_id: int = 1
        attempt_id: int = 1
        operation: str = 'result'
        target_id: str = '7'
        responsibility_key: str = 'results/7'
    @dataclass(frozen=True)
    class Page:
        ticket: object
        page_no: int = 1
        event_id: int = 2
        next_cursor: object = None
    module('camctl.capture.result_pages', ResultPageRef=Page)
    module('camctl.devices.directory', DirectoryCursor=type('DirectoryCursor', (), {}))
    module('camctl.operations.models', AttemptTicket=Ticket)
    for package_name in ('camctl', 'camctl.capture', 'camctl.contracts', 'camctl.devices', 'camctl.operations', 'camctl.persistence'):
        package = module(package_name)
        package.__path__ = []
    path = SOURCE.parents[1].parent / 'capture' / 'files.py'
    name = 'camctl.capture.isolated_files'
    spec = importlib.util.spec_from_file_location(name, path)
    files = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: files, **modules}), patch.object(sys, 'meta_path', [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(files)
        page = Page(Ticket())
        request = files.PairResultFilesSave(11, page, 100)
        assert (request.preview_file_id, request.scan, request.occurred_at) == (11, page, 100)
        assert not hasattr(request, 'source_boundary')
        with pytest.raises(TypeError):
            files.PairResultFilesSave(11, page, Boundary(8, 27), 100)
