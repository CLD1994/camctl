"""逐产物候选只读取得一份原片的完整派生关系。"""

from contextlib import closing
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.catalog import OutputKind
from camctl.outputs.definitions import SelectionMode
from camctl.outputs.sources import select_for_original
from camctl.persistence.repositories import outputs

from .test_qualification import _seed_environment, _seed_plan, _seed_action, _seed_device_file, _seed_output
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture
def family_database(tmp_path):
    _, owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        _seed_plan(connection, 1)
        for action in (11, 12):
            _seed_action(connection, action, 1, action_type=2)
        for identity, source in ((501, 11), (502, 11), (503, 11), (504, 12)):
            _seed_device_file(connection, identity, source)
            _seed_output(connection, identity + 200, source, identity)
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id, purpose, relative_path, retention_state,"
            " cleanup_state, size_bytes, created_event_id, last_event_id, change_count)"
            " VALUES (801, 11, 4, 'derived/801.mp4', 3, 1, 80, 1, 1, 1)"
        )
        connection.execute("UPDATE outputs SET kind=2, device_file_id=NULL, intermediate_file_id=801 WHERE id=703")
        connection.execute("UPDATE outputs SET kind=3 WHERE id=702")
        connection.execute("UPDATE device_files SET role=3, size_bytes=100, original_device_file_id=501,"
                           " pairing_evidence_json='{}' WHERE id=502")
        connection.executemany("INSERT INTO output_origins (output_id, original_output_id) VALUES (?, 701)",
                               ((702,), (703,)))
        _seed_device_file(connection, 505, 11)
        _seed_output(connection, 705, 11, 505)
        connection.commit()
        yield owned
    finally:
        connection.close()


@pytest.mark.parametrize("identity", [701, 702, 703])
def test_related_output_lookup_keeps_original_source_and_file_sizes(family_database, identity):
    connection = family_database.connection
    before = tuple(connection.iterdump())
    result = outputs.load_output_family(connection, identity)
    assert result.source_action_id == 11
    assert (result.original.output_id, result.original.kind, result.original.size_bytes) == (701, OutputKind.ORIGINAL, 4096)
    assert (result.preview.output_id, result.preview.kind, result.preview.size_bytes) == (702, OutputKind.PREVIEW, 100)
    assert (result.repaired.output_id, result.repaired.kind, result.repaired.size_bytes) == (703, OutputKind.REPAIRED, 80)
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("missing", ["preview", "repaired", "both"])
def test_related_output_lookup_distinguishes_absent_derivatives(family_database, missing):
    connection = family_database.connection
    ids = {"preview": (702,), "repaired": (703,), "both": (702, 703)}[missing]
    for identity in ids:
        connection.execute("DELETE FROM output_origins WHERE output_id=?", (identity,))
        connection.execute("DELETE FROM outputs WHERE id=?", (identity,))
    connection.commit()
    result = outputs.load_output_family(connection, 701)
    assert (result.preview is None) == (missing in ("preview", "both"))
    assert (result.repaired is None) == (missing in ("repaired", "both"))


@pytest.mark.parametrize("fault", ["missing_origin", "original_has_origin", "wrong_original_kind",
                                  "cross_source", "duplicate_preview", "file_owner", "missing_target"])
def test_related_output_lookup_rejects_inconsistent_relationships(family_database, fault):
    connection = family_database.connection
    target = 702
    if fault == "missing_origin":
        connection.execute("DELETE FROM output_origins WHERE output_id=702")
    elif fault == "original_has_origin":
        connection.execute("INSERT INTO output_origins (output_id, original_output_id) VALUES (701, 705)")
    elif fault == "wrong_original_kind":
        connection.execute("UPDATE outputs SET kind=3 WHERE id=701")
    elif fault == "cross_source":
        connection.execute("UPDATE outputs SET source_action_id=12 WHERE id=703")
    elif fault == "duplicate_preview":
        connection.execute("UPDATE outputs SET kind=3 WHERE id=705")
        connection.execute("INSERT INTO output_origins (output_id, original_output_id) VALUES (705, 701)")
    elif fault == "file_owner":
        connection.execute("UPDATE intermediate_files SET owner_action_id=12 WHERE id=801")
    else:
        target = 999
    connection.commit()
    before = tuple(connection.iterdump())
    with pytest.raises(ConsistencyError):
        outputs.load_output_family(connection, target)
    assert tuple(connection.iterdump()) == before


def test_family_lookup_uses_size_selection_without_fixing_members(family_database):
    connection = family_database.connection
    family = outputs.load_output_family(connection, 702)
    item = select_for_original(family.source_action_id, family.original, SelectionMode.PREVIEW,
                               preview=family.preview, repaired=family.repaired)
    assert item.output_id == 703
    assert (item.preview_size, item.repaired_size) == (100, 80)
    with closing(connection.execute("SELECT count(*) FROM obtain_source_selections")) as cursor:
        assert cursor.fetchone() == (0,)


def test_family_lookup_preserves_unknown_complete_size(family_database):
    connection = family_database.connection
    connection.execute("UPDATE intermediate_files SET size_bytes=NULL WHERE id=801")
    connection.commit()
    family = outputs.load_output_family(connection, 703)
    assert family.repaired.size_bytes is None
    result = select_for_original(family.source_action_id, family.original, SelectionMode.PREVIEW,
                                 preview=family.preview, repaired=family.repaired)
    assert (result.output_id, result.error_code) == (702, 6)


@pytest.mark.parametrize("target", [701, 702, 703])
@pytest.mark.parametrize("change", [
    "UPDATE device_files SET role=1 WHERE id=501",
    "UPDATE device_files SET role=1, original_device_file_id=NULL, pairing_evidence_json=NULL WHERE id=502",
    "UPDATE device_files SET original_device_file_id=505 WHERE id=502",
    "UPDATE device_files SET original_device_file_id=NULL, pairing_evidence_json=NULL WHERE id=502",
    "UPDATE device_files SET completion_state=1, completion_evidence_json=NULL, size_bytes=NULL WHERE id=501",
    "UPDATE device_files SET completion_state=1, completion_evidence_json=NULL, size_bytes=NULL WHERE id=502",
    "UPDATE intermediate_files SET purpose=2 WHERE id=801",
    "UPDATE intermediate_files SET retention_state=1 WHERE id=801",
    "UPDATE intermediate_files SET retention_state=2, cleanup_state=2 WHERE id=801",
    "UPDATE outputs SET device_file_id=NULL, intermediate_file_id=801 WHERE id=701",
])
def test_family_lookup_rejects_file_relationship_conflicts(family_database, target, change):
    connection = family_database.connection
    # 承载种类反例交换引用，保留各文件只被一个产物引用的 SQL 约束。
    if change.endswith("WHERE id=701") and "UPDATE outputs" in change:
        connection.execute("UPDATE outputs SET intermediate_file_id=NULL, device_file_id=503 WHERE id=703")
    connection.execute(change)
    connection.commit()
    before = tuple(connection.iterdump())
    with pytest.raises(ConsistencyError):
        outputs.load_output_family(connection, target)
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("target", [701, 702, 703])
@pytest.mark.parametrize("availability,presence", [(3, 3), (4, 3), (5, 1)])
def test_family_lookup_preserves_later_file_existence_states(family_database, target, availability, presence):
    connection = family_database.connection
    connection.execute("UPDATE outputs SET availability=?, cleanup_status=?, error_json=? WHERE source_action_id=11",
                       (availability, 4 if availability == 3 else 1, None if availability == 3 else '{}'))
    connection.execute("UPDATE device_files SET presence_state=? WHERE source_action_id=11", (presence,))
    connection.commit()
    before = tuple(connection.iterdump())
    result = outputs.load_output_family(connection, target)
    assert (result.original.output_id, result.preview.output_id, result.repaired.output_id) == (701, 702, 703)
    assert (result.original.availability, result.preview.availability, result.repaired.availability) == (availability,) * 3
    assert tuple(connection.iterdump()) == before


class FamilyConnection(_FaultConnection):
    def __init__(self, connection, fail=None):
        super().__init__(connection, "unused")
        self.fail = fail
        self.cursors = []
        self.related_counts = []

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        stage = ("origin" if sql.startswith("SELECT original_output_id, id, output_id FROM output_origins") else
                 "related" if sql.startswith("SELECT output_id, id, original_output_id FROM output_origins") else None)
        if stage is None:
            return cursor
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.close.side_effect = cursor.close

        def fetch():
            if self.fail == stage:
                raise sqlite3.OperationalError("read failed")
            rows = cursor.fetchall() if stage == "related" else cursor.fetchone()
            if stage == "related":
                self.related_counts.append(len(rows))
            return rows

        if stage == "related":
            probe.fetchall.side_effect = fetch
        else:
            probe.fetchone.side_effect = fetch
        self.cursors.append(probe)
        return probe


@pytest.mark.parametrize("state", ["success", "absent", "origin", "related", "decode"])
def test_family_queries_close_on_all_read_exits(family_database, state):
    connection = family_database.connection
    if state == "absent":
        connection.execute("DELETE FROM output_origins")
        connection.execute("DELETE FROM outputs WHERE id IN (702, 703)")
    if state == "decode":
        connection.execute('UPDATE outputs SET media_json=\'{"x":1,"x":2}\' WHERE id=702')
    connection.commit()
    probe = FamilyConnection(connection, state)
    before = tuple(connection.iterdump())
    if state in ("origin", "related", "decode"):
        with pytest.raises(ConsistencyError if state == "decode" else sqlite3.OperationalError):
            outputs.load_output_family(probe, 701)
    else:
        outputs.load_output_family(probe, 701)
    assert probe.cursors
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(connection.iterdump()) == before


def test_family_lookup_bounds_duplicate_detection(family_database):
    connection = family_database.connection
    for identity in range(506, 511):
        _seed_device_file(connection, identity, 11)
        _seed_output(connection, identity + 200, 11, identity)
        connection.execute("UPDATE outputs SET kind=3 WHERE id=?", (identity + 200,))
        connection.execute("INSERT INTO output_origins (output_id, original_output_id) VALUES (?, 701)", (identity + 200,))
    connection.commit()
    probe = FamilyConnection(connection)
    with pytest.raises(ConsistencyError):
        outputs.load_output_family(probe, 701)
    assert probe.related_counts == [3]
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


def test_family_query_execution_failure_never_means_no_derivative(family_database):
    failing = _FaultConnection(family_database.connection, "SELECT output_id, id, original_output_id FROM output_origins")
    with pytest.raises(sqlite3.OperationalError):
        outputs.load_output_family(failing, 701)
