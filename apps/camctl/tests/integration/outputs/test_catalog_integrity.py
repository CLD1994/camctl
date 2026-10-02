"""完整来源读取与首次固定都消费可靠的产物和文件关联。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.sources import SelectionMode, select_outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs

from .test_output_family import family_database
from .test_qualification import _NOW, _seed_action, _seed_device_file, _seed_output, _seed_processing
from .test_sources import _fixed_resolution
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture
def selection_database(family_database):
    owned = family_database
    connection = owned.connection
    _seed_action(connection, 30, 1, action_type=4)
    connection.execute("UPDATE actions SET source_resolution_state=2, resolved_source_plan_id=1 WHERE id=30")
    connection.execute('UPDATE actions SET input_fields_json=\'{"params":{"source":{"action_instance_id":"11"}}}\' WHERE id=30')
    connection.execute("UPDATE actions SET status=3 WHERE id=11")
    connection.execute("INSERT INTO action_dependencies (id, action_id, depends_on_action_id) VALUES (41, 30, 11)")
    connection.execute("INSERT INTO obtain_source_selections"
                       " (id, dependency_id, status, error_code, error_details_json) VALUES (61, 41, 1, NULL, NULL)")
    connection.commit()
    return owned


_CONFLICTS = [
    pytest.param("DELETE FROM device_files WHERE id=501", id="missing-original-file"),
    pytest.param("DELETE FROM device_files WHERE id=502", id="missing-preview-file"),
    pytest.param("DELETE FROM intermediate_files WHERE id=801", id="missing-repair-file"),
    pytest.param("DELETE FROM outputs WHERE id=701", id="missing-original-output"),
    pytest.param("DELETE FROM output_origins WHERE output_id=702", id="missing-preview-origin"),
    pytest.param("DELETE FROM output_origins WHERE output_id=703", id="missing-repair-origin"),
    pytest.param("UPDATE device_files SET role=1 WHERE id=501", id="original-role"),
    pytest.param("UPDATE device_files SET role=1, original_device_file_id=NULL, pairing_evidence_json=NULL WHERE id=502", id="preview-role"),
    pytest.param("UPDATE device_files SET original_device_file_id=505 WHERE id=502", id="different-pair"),
    pytest.param("UPDATE device_files SET original_device_file_id=NULL, pairing_evidence_json=NULL WHERE id=502", id="missing-pair"),
    pytest.param("UPDATE device_files SET completion_state=1, completion_evidence_json=NULL, size_bytes=NULL WHERE id=501", id="incomplete-original"),
    pytest.param("UPDATE device_files SET completion_state=1, completion_evidence_json=NULL, size_bytes=NULL WHERE id=502", id="incomplete-preview"),
    pytest.param("UPDATE intermediate_files SET purpose=2 WHERE id=801", id="repair-purpose"),
    pytest.param("UPDATE intermediate_files SET retention_state=1 WHERE id=801", id="repair-not-promoted"),
    pytest.param("UPDATE intermediate_files SET retention_state=2, cleanup_state=2 WHERE id=801", id="repair-automatic-cleanup"),
    pytest.param("UPDATE intermediate_files SET owner_action_id=12 WHERE id=801", id="repair-owner"),
    pytest.param("UPDATE device_files SET source_action_id=12 WHERE id=502", id="preview-source"),
    pytest.param("UPDATE outputs SET source_action_id=12 WHERE id=702", id="derived-output-source"),
    pytest.param("UPDATE output_origins SET original_output_id=704 WHERE output_id=702", id="different-source-origin"),
    pytest.param("UPDATE output_origins SET original_output_id=703 WHERE output_id=702", id="derived-origin"),
]


@pytest.mark.parametrize("entry", ["read", "fix"])
@pytest.mark.parametrize("change", _CONFLICTS)
def test_catalog_conflicts_never_become_selection_facts(selection_database, entry, change):
    owned = selection_database
    connection = owned.connection
    snapshot = select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(connection, 11), SelectionMode.DEFAULT)
    # 故障注入模拟应永久保留的关联行缺失；保留单行约束，检查读取边界。
    connection.execute(change)
    connection.commit()
    before = tuple(connection.iterdump())
    if entry == "read":
        with pytest.raises(ConsistencyError):
            outputs.load_selection_facts(connection, 11)
    else:
        result = outputs.OutputsRepository().fix_selection(
            outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(connection.iterdump()) == before


def test_complete_catalog_preserves_order_and_ignores_unrelated_bad_files(selection_database):
    connection = selection_database.connection
    connection.execute("UPDATE device_files SET role=1 WHERE id=504")
    connection.commit()
    before = tuple(connection.iterdump())
    facts = outputs.load_selection_facts(connection, 11)
    assert facts.source_completed
    assert [(item.output_id, item.original_output_id, item.size_bytes) for item in facts.outputs] == [
        (701, None, 4096), (702, 701, 100), (703, 701, 80), (705, None, 4096)]
    assert tuple(connection.iterdump()) == before


def test_complete_catalog_accepts_derived_id_before_original(selection_database):
    connection = selection_database.connection
    connection.execute("UPDATE outputs SET id=799 WHERE id=701")
    connection.execute("UPDATE output_origins SET original_output_id=799 WHERE original_output_id=701")
    connection.commit()
    facts = outputs.load_selection_facts(connection, 11)
    assert tuple(item.output_id for item in facts.outputs) == (702, 703, 705, 799)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT)
    assert snapshot.selected_output_ids == (705, 703)


def test_fixed_selection_does_not_expand_when_new_output_has_bad_file(selection_database):
    owned = selection_database
    connection = owned.connection
    snapshot = select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(connection, 11), SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, snapshot, _NOW)
    repository = outputs.OutputsRepository()
    key = new_operation_key()
    first = repository.fix_selection(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    _seed_device_file(connection, 506, 11)
    _seed_output(connection, 706, 11, 506)
    connection.execute("UPDATE device_files SET role=1 WHERE id=506")
    connection.commit()
    before = tuple(connection.iterdump())
    for operation_key in (key, new_operation_key()):
        result = repository.fix_selection(command, operation_key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.snapshot.selected_output_ids == (703, 705)
    assert tuple(connection.iterdump()) == before


class CatalogConnection(_FaultConnection):
    def __init__(self, connection, fault=None, query="catalog"):
        super().__init__(connection, "unused")
        self.fault = fault
        self.query = query
        self.error = sqlite3.OperationalError("selection read failed")
        self.cursors = []
        self.batches = []

    def execute(self, sql, parameters=()):
        prefixes = {
            "catalog": ("SELECT id FROM outputs WHERE source_action_id", "SELECT o.id, o.source_action_id"),
            "processing": ("SELECT check_state, repair_state, discard_state FROM recording_processing",),
            "items": ("SELECT requested_output_id, output_id, basis, original_output_id",),
            "exists": ("SELECT 1 FROM outputs WHERE id",),
            "pending_items": ("SELECT 1 FROM obtain_items WHERE selection_id",),
        }
        is_catalog = sql.startswith(prefixes[self.query])
        if is_catalog and self.fault == "execute":
            raise self.error
        cursor = self._connection.execute(sql, parameters)
        if not is_catalog:
            return cursor
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.close.side_effect = cursor.close

        def fetchmany(size):
            if self.fault == "fetch":
                raise self.error
            rows = cursor.fetchmany(size)
            self.batches.append((size, len(rows)))
            return rows

        probe.fetchmany.side_effect = fetchmany
        probe.fetchall.side_effect = self.error if self.fault == "fetch" else cursor.fetchall
        probe.fetchone.side_effect = self.error if self.fault == "fetch" else cursor.fetchone
        self.cursors.append(probe)
        return probe


@pytest.mark.parametrize("entry", ["read", "fix"])
@pytest.mark.parametrize("state", ["success", "empty", "fetch", "execute", "decode"])
def test_catalog_queries_release_cursors_and_preserve_read_errors(selection_database, entry, state):
    owned = selection_database
    connection = owned.connection
    if state == "empty":
        connection.execute("DELETE FROM output_origins")
        connection.execute("DELETE FROM outputs WHERE source_action_id=11")
        connection.commit()
    snapshot = select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(connection, 11), SelectionMode.DEFAULT)
    if state == "decode":
        connection.execute('UPDATE outputs SET media_json=\'{"x":1,"x":2}\' WHERE id=702')
        connection.commit()
    probe = CatalogConnection(connection, state)
    before = tuple(connection.iterdump())
    failed = state in ("fetch", "execute", "decode")
    error_type = ConsistencyError if state == "decode" else sqlite3.OperationalError
    if entry == "read":
        if failed:
            with pytest.raises(error_type):
                outputs.load_selection_facts(probe, 11)
        else:
            facts = outputs.load_selection_facts(probe, 11)
            assert len(facts.outputs) == (0 if state == "empty" else 4)
    else:
        result = outputs.OutputsRepository().fix_selection(outputs.FixSelection(61, snapshot, _NOW),
                    new_operation_key(), replace(owned, connection=probe))
        assert result.kind is (DbOutcomeKind.ROLLED_BACK if failed else DbOutcomeKind.COMPLETED), result.error
        if failed:
            assert isinstance(result.error, error_type), result.error
    if state != "execute":
        assert probe.cursors
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    if failed or entry == "read":
        assert tuple(connection.iterdump()) == before


def test_catalog_enumerates_multiple_bounded_batches_without_reloading_family_files(selection_database, monkeypatch):
    connection = selection_database.connection
    for identity in range(1000, 1130):
        _seed_device_file(connection, identity + 1000, 11)
        _seed_output(connection, identity, 11, identity + 1000)
    connection.commit()
    read_row = create_autospec(outputs.row_facts, side_effect=outputs.row_facts)
    monkeypatch.setattr(outputs, "row_facts", read_row)
    probe = CatalogConnection(connection)
    facts = outputs.load_selection_facts(probe, 11)
    assert tuple(item.output_id for item in facts.outputs) == (701, 702, 703, 705, *range(1000, 1130))
    assert len([size for _, size in probe.batches if size]) >= 2
    assert all(0 <= count <= requested <= 128 for requested, count in probe.batches)
    # 每个真实文件最多加载一次，防止按每个派生 ID 重新加载整个关联集合。
    for table, identity in (("device_files", 501), ("device_files", 502), ("intermediate_files", 801)):
        calls = [call for call in read_row.call_args_list if call.args[1:] == (table, identity)]
        assert len(calls) == 1
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("entry", ["read", "fix"])
@pytest.mark.parametrize("state", ["present", "absent", "fetch", "execute"])
def test_processing_query_releases_cursor_and_preserves_error(selection_database, entry, state):
    owned = selection_database
    connection = owned.connection
    if state != "absent":
        connection.execute("DELETE FROM output_origins WHERE output_id=703")
        connection.execute("DELETE FROM outputs WHERE id=703")
        connection.execute("DELETE FROM intermediate_files WHERE id=801")
        _seed_processing(connection, 51, 11, 501)
        connection.execute("UPDATE recording_processing SET check_decision=2, check_basis_json='{}',"
                           " repair_state=2, repair_basis_json='{}' WHERE id=51")
        connection.commit()
    snapshot = select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(connection, 11), SelectionMode.DEFAULT)
    probe = CatalogConnection(connection, state, "processing")
    before = tuple(connection.iterdump())
    failed = state in ("fetch", "execute")
    if entry == "read":
        if failed:
            with pytest.raises(sqlite3.OperationalError) as raised:
                outputs.load_selection_facts(probe, 11)
            assert raised.value is probe.error
        else:
            assert outputs.load_selection_facts(probe, 11).source_completed
    else:
        result = outputs.OutputsRepository().fix_selection(outputs.FixSelection(61, snapshot, _NOW),
                    new_operation_key(), replace(owned, connection=probe))
        assert result.kind is (DbOutcomeKind.ROLLED_BACK if failed else DbOutcomeKind.COMPLETED), result.error
        if failed:
            assert result.error is probe.error
    if state != "execute":
        assert len(probe.cursors) == 1
        probe.cursors[0].close.assert_called_once_with()
    if failed or entry == "read":
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("entry", ["read", "new_key", "original_key"])
@pytest.mark.parametrize("state,query,fault", [
    ("present", "items", None), ("present", "exists", None),
    ("empty", "items", None), ("missing", "exists", None),
    ("decode", "items", None), ("fetch", "items", "fetch"),
    ("fetch", "exists", "fetch"), ("execute", "items", "execute"),
    ("execute", "exists", "execute"),
])
def test_saved_selection_queries_release_cursors_on_all_exits(selection_database, entry, state, query, fault):
    owned = selection_database
    connection = owned.connection
    if state == "empty":
        connection.execute("DELETE FROM output_origins")
        connection.execute("DELETE FROM outputs WHERE source_action_id=11")
        connection.commit()
    snapshot = select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(connection, 11), SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, snapshot, _NOW)
    repository = outputs.OutputsRepository()
    key = new_operation_key()
    first = repository.fix_selection(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    if state == "missing":
        connection.execute("DELETE FROM outputs WHERE id=703")
    if state == "decode":
        connection.execute("UPDATE obtain_items SET status=4, error_code=3,"
                           " error_details_json=? WHERE output_id=703",
                           ('{"output_id":"703","output_id":"703"}',))
    connection.commit()
    probe = CatalogConnection(connection, fault, query)
    before = tuple(connection.iterdump())
    failed = state in ("missing", "decode", "fetch", "execute")
    error_type = sqlite3.OperationalError if fault else ConsistencyError
    if entry == "read":
        if failed:
            with pytest.raises(error_type) as raised:
                outputs.load_selection(probe, 61)
            if fault:
                assert raised.value is probe.error
        else:
            assert outputs.load_selection(probe, 61).selected_output_ids == snapshot.selected_output_ids
    else:
        result = repository.fix_selection(command, key if entry == "original_key" else new_operation_key(),
                                          replace(owned, connection=probe))
        assert result.kind is (DbOutcomeKind.ROLLED_BACK if failed else DbOutcomeKind.COMPLETED), result.error
        if failed:
            assert isinstance(result.error, error_type), result.error
            if fault:
                assert result.error is probe.error
        else:
            assert result.value.snapshot.selected_output_ids == snapshot.selected_output_ids
    if fault != "execute":
        assert probe.cursors
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(connection.iterdump()) == before
