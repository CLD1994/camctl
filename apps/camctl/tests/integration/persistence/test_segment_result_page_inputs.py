"""原结果页原始身份与族索引沿同一实际页事务保存。"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.history.events import RowImage, load_event_registry
from camctl.history.validators import EventContext
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, file_discovery_row
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.result_pages import SaveResultPageCommand
from camctl.persistence.transaction import (BoundedCommandPlan, CommandPlan,
    commit_operation, event_envelope, next_row_id, row_facts)
from camctl.history.reads import ResultPageWriteScope
from camctl.capture.files import FileObservationSave, file_identity_key
from camctl.contracts.values import ConsistencyError

from ..capture.test_baseline_start import owned
from ..capture.test_capture_contract import register_capture_guards
from ..capture.test_result_page_history import _actual, _baseline_world, _request

register_operation_guards()
register_capture_guards()


def _osmo_entry(path: str, *, size: int | None, complete: bool = True) -> dict:
    suffix = path.rpartition(".")[2].lower()
    return {
        "identity": path,
        "locator": {"path": path},
        "complete": complete,
        "size_bytes": size,
        "kind": "video",
        "format_id": suffix,
        "paired_identity": None,
    }


async def _save_raw_page(owned, tmp_path, entries, *, baseline=(), error=None,
                         cursor=None, next_cursor=None, finalized=True):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, baseline)
    actual = _actual(ticket, entries=entries, cursor=cursor, next_cursor=next_cursor,
                     finalized=finalized, error=error)
    request = _request(runtime, ticket, actual)
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return runtime, ticket, request, receipt


@pytest.mark.asyncio
async def test_raw_osmo_page_indexes_every_difference_identity_without_pairing(owned, tmp_path):
    root = "/mnt/media_rw/emulated/DCIM"
    nfc = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    nfd = "cafe\N{COMBINING ACUTE ACCENT}"
    paths = sorted((
        f"{root}/CAM/Take01.OSV",
        f"{root}/CAM/Take01.LrF",
        f"{root}/CAM/take01.lrf",
        f"{root}/cam/Take01.lrf",
        f"{root}/CAM/Take01.MP4",
        f"{root}/CAM/opaque",
        f"{root}/CAM/{nfc}.OSV",
        f"{root}/CAM/{nfd}.lrf",
    ))
    entries = [_osmo_entry(path, size=0 if path.rpartition(".")[2].lower() == "osv" else None,
                           complete=path.rpartition(".")[2].lower() == "osv") for path in paths]
    error = ErrorValue(
        "adb_metadata_failed", "device_file_access", {"reason": "bounded_test_failure"})
    runtime, ticket, request, receipt = await _save_raw_page(
        owned, tmp_path, entries, error=error)

    saved = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [entry.identity for entry in saved.page.entries] == paths
    assert all(entry.paired_identity is None for entry in saved.page.entries)
    assert [identity for identity, _ in saved.file_ids] == paths
    rows = owned.connection.execute(
        "SELECT identity,family_directory,family_stem,family_role,file_id,"
        "page_event_id,created_event_id FROM result_page_files "
        "WHERE page_event_id=? ORDER BY identity COLLATE BINARY",
        (receipt.value.event_id,),
    ).fetchall()
    expected = {}
    for path in paths:
        suffix = path.rpartition(".")[2].lower()
        if suffix in ("osv", "lrf"):
            expected[path] = (
                path.rpartition("/")[0],
                path.rpartition("/")[2].rsplit(".", 1)[0],
                int(enum_for("result_page_files.family_role").ORIGINAL)
                if suffix == "osv" else int(enum_for("result_page_files.family_role").PREVIEW),
            )
        else:
            expected[path] = (None, None, None)
    assert {row[0]: row[1:4] for row in rows} == expected
    assert {row[4] for row in rows} == {file_id for _, file_id in saved.file_ids}
    assert {row[5] for row in rows} == {receipt.value.event_id}
    index_event_id = rows[0][6]
    assert {row[6] for row in rows} == {index_event_id}
    event_type, reason, transaction_id = owned.connection.execute(
        "SELECT event_type,json_extract(body_json,'$.reason'),transaction_id "
        "FROM history_events WHERE id=?", (index_event_id,)).fetchone()
    registry = load_event_registry()["events"]["RESULT_PAGE"]
    assert event_type == registry["id"]
    assert reason == registry["branches"]["INDEX_FILES"]["reason"]
    assert transaction_id == owned.connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?",
        (receipt.value.event_id,),
    ).fetchone()[0]
    index_rows = json.loads(owned.connection.execute(
        "SELECT body_json FROM history_events WHERE id=?", (index_event_id,),
    ).fetchone()[0])["rows"]
    assert len(index_rows) == len(paths) <= 128
    assert all(row[6] == index_event_id for row in rows)


def _tamper_index_event(event, tamper, root, extra_file_id=None):
    row = event.rows[0]
    values = dict(row.after.values)
    row_id = row.row_id
    if tamper == "index_replaces_identity":
        values["identity"] = f"{root}/substituted.OSV"
    elif tamper == "index_wrong_page":
        values["page_event_id"] = event.event_id + 1
    elif tamper == "index_wrong_created_event":
        values["created_event_id"] = event.event_id - 1
    elif tamper == "index_wrong_family":
        values["family_stem"] = f"{values['family_stem']}-substituted"
    elif tamper == "index_wrong_owner":
        values["action_id"] += 1000
    elif tamper == "index_wrong_row_id":
        row_id += 1000
    elif tamper == "index_adds_extra_relation":
        if extra_file_id is None:
            raise AssertionError("extra relation needs a real existing file")
        values.update(identity=f"{root}/outside-page.OSV",
                      file_id=extra_file_id, family_stem="outside-page")
    else:
        raise AssertionError(tamper)
    changed = replace(row, row_id=row_id, after=RowImage(True, values))
    if tamper == "index_adds_extra_relation":
        extra = replace(changed, row_id=row_id + 1)
        return replace(event, rows=(changed, extra))
    return replace(event, rows=(changed,))


async def _save_out_of_page_file(runtime, ticket, owned, identity):
    run = row_facts(owned.connection, "operation_runs", ticket.run_id)
    saved = runtime.capture.save_file_observation(
        FileObservationSave(run["action_id"], identity, {"path": identity},
            runtime.wall_us(), identity.rpartition("/")[2], "video"),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    return saved.value.file_id


@pytest.mark.asyncio
async def test_128_file_page_keeps_existing_page_bound_and_index_event_bound(owned, tmp_path):
    root = "/mnt/media_rw/emulated/DCIM/CAM"
    entries = [
        _osmo_entry(f"{root}/{index:03d}.OSV", size=index, complete=True)
        for index in range(128)
    ]
    runtime, ticket, _, receipt = await _save_raw_page(owned, tmp_path, entries)
    page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert len(page.page.entries) == len(page.file_ids) == 128
    row_count, index_event_id = owned.connection.execute(
        "SELECT COUNT(*),MIN(created_event_id) FROM result_page_files "
        "WHERE page_event_id=?", (receipt.value.event_id,),
    ).fetchone()
    assert row_count == 128
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM json_each((SELECT body_json FROM history_events WHERE id=?), '$.rows')",
        (index_event_id,),
    ).fetchone()[0] == 128
    assert owned.connection.execute(
        "SELECT t.id,t.last_event_id FROM history_transactions t "
        "JOIN history_events e ON e.transaction_id=t.id WHERE e.id=?",
        (index_event_id,),
    ).fetchone() == (
        owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE id=?", (index_event_id,)
        ).fetchone()[0],
        index_event_id,
    )


@pytest.mark.asyncio
async def test_family_prefix_and_run_file_indexes_bound_actual_reads(owned, tmp_path):
    root = "/mnt/media_rw/emulated/DCIM/CAM"
    family_suffixes = [
        f"{o}{s}{v}"
        for o in "oO" for s in "sS" for v in "vV"
    ]
    family_paths = [f"{root}/Take.{suffix}" for suffix in family_suffixes]
    unrelated_paths = [f"{root}/other-{index:03d}.LRF" for index in range(120)]
    paths = sorted((*family_paths, *unrelated_paths))
    runtime, ticket, _, receipt = await _save_raw_page(
        owned, tmp_path,
        [_osmo_entry(path, size=None, complete=False) for path in paths],
    )
    relation = owned.connection.execute(
        "SELECT run_id,attempt_id,MIN(page_event_id) FROM result_page_files "
        "WHERE page_event_id=? GROUP BY run_id,attempt_id",
        (receipt.value.event_id,),
    ).fetchone()
    run_id, attempt_id, page_event_id = relation
    transaction_id, final_event_id = owned.connection.execute(
        "SELECT t.id,t.last_event_id FROM history_transactions t "
        "JOIN history_events e ON e.transaction_id=t.id WHERE e.id=?",
        (receipt.value.event_id,),
    ).fetchone()
    assert final_event_id > receipt.value.event_id

    family_role = int(enum_for("result_page_files.family_role").ORIGINAL)
    family_predicate = (
        "attempt_id=? AND family_directory=? AND family_stem=? AND family_role=? "
        "AND page_event_id<=?"
    )
    family_args = (attempt_id, root, "Take", family_role, final_event_id)
    count_sql = (
        "SELECT COUNT(*) FROM (SELECT file_id FROM result_page_files WHERE "
        + family_predicate + " GROUP BY file_id)"
    )
    sample_sql = (
        "SELECT file_id FROM result_page_files WHERE " + family_predicate
        + " GROUP BY file_id ORDER BY file_id LIMIT 2"
    )
    query_plan = [row[3] for row in owned.connection.execute(
        "EXPLAIN QUERY PLAN " + count_sql, family_args)]
    assert any("USING COVERING INDEX result_page_files_family" in item
               for item in query_plan), query_plan
    sample_plan = [row[3] for row in owned.connection.execute(
        "EXPLAIN QUERY PLAN " + sample_sql, family_args)]
    assert any("USING COVERING INDEX result_page_files_family" in item
               for item in sample_plan), sample_plan

    def vm_steps(sql, parameters):
        steps = 0

        def tick():
            nonlocal steps
            steps += 1
            return 0

        owned.connection.set_progress_handler(tick, 1)
        try:
            rows = owned.connection.execute(sql, parameters).fetchall()
        finally:
            owned.connection.set_progress_handler(None, 0)
        return rows, steps

    count_rows, indexed_count_steps = vm_steps(count_sql, family_args)
    sample_rows, indexed_sample_steps = vm_steps(sample_sql, family_args)
    scan_count_rows, scan_count_steps = vm_steps(
        count_sql.replace("FROM result_page_files WHERE", "FROM result_page_files NOT INDEXED WHERE"),
        family_args,
    )
    scan_sample_rows, scan_sample_steps = vm_steps(
        sample_sql.replace("FROM result_page_files WHERE", "FROM result_page_files NOT INDEXED WHERE"),
        family_args,
    )
    assert count_rows == scan_count_rows == [(len(family_paths),)]
    assert sample_rows == scan_sample_rows
    assert len(sample_rows) <= 2
    assert indexed_count_steps < scan_count_steps, (
        indexed_count_steps, scan_count_steps)
    assert indexed_sample_steps < scan_sample_steps, (
        indexed_sample_steps, scan_sample_steps)

    run_file_sql = (
        "SELECT page_event_id FROM result_page_files "
        "WHERE run_id=? AND file_id=? ORDER BY page_event_id LIMIT 2"
    )
    sample_file_id = sample_rows[0][0]
    run_file_plan = [row[3] for row in owned.connection.execute(
        "EXPLAIN QUERY PLAN " + run_file_sql, (run_id, sample_file_id))]
    assert any("USING COVERING INDEX result_page_files_run_file" in item
               for item in run_file_plan), run_file_plan
    run_rows = owned.connection.execute(
        run_file_sql, (run_id, sample_file_id)).fetchall()
    assert run_rows == [(page_event_id,)]
    assert transaction_id > 0
    print(f"family_count_plan={query_plan!r}")
    print(f"family_sample_plan={sample_plan!r}")
    print(f"family_count_vm_steps=indexed:{indexed_count_steps},full_scan:{scan_count_steps}")
    print(f"family_sample_vm_steps=indexed:{indexed_sample_steps},full_scan:{scan_sample_steps}")
    print(f"family_count_rows={count_rows!r};sample_rows={sample_rows!r};sample_limit=2")
    print(f"run_file_plan={run_file_plan!r};rows={run_rows!r}")


@pytest.mark.parametrize("count", [1, 127])
@pytest.mark.asyncio
async def test_one_and_127_relation_rows_keep_single_index_event_bound(owned, tmp_path, count):
    root = "/mnt/media_rw/emulated/DCIM/CAM"
    entries = [_osmo_entry(f"{root}/{index:03d}.LRF", size=None, complete=False)
               for index in range(count)]
    runtime, ticket, _, receipt = await _save_raw_page(owned, tmp_path, entries)
    page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert len(page.file_ids) == count
    relation_count, index_event_id = owned.connection.execute(
        "SELECT COUNT(*),MIN(created_event_id) FROM result_page_files WHERE page_event_id=?",
        (receipt.value.event_id,),
    ).fetchone()
    assert relation_count == count
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM json_each((SELECT body_json FROM history_events WHERE id=?), '$.rows')",
        (index_event_id,),
    ).fetchone() == (count,)


@pytest.mark.asyncio
async def test_page_difference_indexes_reused_and_new_files_together(owned, tmp_path):
    from camctl.contracts.values import new_operation_key

    root = "/mnt/media_rw/emulated/DCIM/CAM"
    old_identity = f"{root}/already-seen.OSV"
    new_identity = f"{root}/new.OSV"
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ())
    old = runtime.capture.save_file_observation(
        FileObservationSave(1, old_identity, {"path": old_identity},
            runtime.wall_us(), "already-seen.OSV", "video"),
        new_operation_key(), owned)
    assert old.kind is DbOutcomeKind.COMPLETED, old.error
    request = _request(runtime, ticket, _actual(ticket, entries=[
        _osmo_entry(old_identity, size=0), _osmo_entry(new_identity, size=7)], finalized=True))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    saved = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [identity for identity, _ in saved.file_ids] == [old_identity, new_identity]
    assert saved.file_ids[0][1] == old.value.file_id
    transaction_id = owned.connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?", (receipt.value.event_id,)
    ).fetchone()[0]
    creates = owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE transaction_id=? AND event_type=? "
        "AND json_extract(body_json,'$.reason')=?",
        (transaction_id,
         load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]["id"],
         load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]["branches"]["CREATE"]["reason"]),
    ).fetchone()[0]
    assert creates == 1
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM result_page_files WHERE page_event_id=?",
        (receipt.value.event_id,),
    ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_zero_difference_is_not_an_empty_or_final_device_page(owned, tmp_path):
    root = "/mnt/media_rw/emulated/DCIM"
    identity = f"{root}/already.OSV"
    cursor = DirectoryCursor(DeviceBinding("cam-1", "dji-action6"),
                             (root, "/mnt/media_rw/sd/DCIM"), 0, identity)
    runtime, ticket, _, receipt = await _save_raw_page(
        owned, tmp_path,
        [_osmo_entry(identity, size=0)],
        baseline=("already.OSV",),
        next_cursor=cursor,
        finalized=False,
    )
    page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [entry.identity for entry in page.page.entries] == [identity]
    assert page.file_ids == ()
    assert page.page.next_cursor == cursor
    assert page.page.scan_complete is False
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM result_page_files WHERE page_event_id=?",
        (receipt.value.event_id,),
    ).fetchone() == (0,)
    transaction_id = owned.connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?", (receipt.value.event_id,)
    ).fetchone()[0]
    registry = load_event_registry()["events"]["RESULT_PAGE"]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE transaction_id=? AND event_type=? "
        "AND json_extract(body_json,'$.reason')=?",
        (transaction_id, registry["id"], registry["branches"]["INDEX_FILES"]["reason"]),
    ).fetchone() == (0,)


@pytest.mark.parametrize("corruption", ["missing", "created_event_id"])
@pytest.mark.asyncio
async def test_page_read_rejects_corrupt_relation_without_rebuilding(owned, tmp_path, corruption):
    root = "/mnt/media_rw/emulated/DCIM/CAM"
    identity = f"{root}/corrupt.OSV"
    runtime, ticket, _, receipt = await _save_raw_page(
        owned, tmp_path, [_osmo_entry(identity, size=0)])
    row_id, created_event_id = owned.connection.execute(
        "SELECT id,created_event_id FROM result_page_files WHERE page_event_id=?",
        (receipt.value.event_id,),
    ).fetchone()
    assert created_event_id > receipt.value.event_id
    if corruption == "missing":
        owned.connection.execute("DELETE FROM result_page_files WHERE id=?", (row_id,))
    else:
        owned.connection.execute(
            "UPDATE result_page_files SET created_event_id=? WHERE id=?",
            (receipt.value.event_id, row_id),
        )
    with pytest.raises(ConsistencyError):
        runtime.capture.read_result_pages(ticket, None, 1, owned)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM result_page_files WHERE page_event_id=?",
        (receipt.value.event_id,),
    ).fetchone() == ((0,) if corruption == "missing" else (1,))


@pytest.mark.parametrize("page_kind", ["empty_device_page", "nonempty_zero_difference"])
@pytest.mark.asyncio
async def test_bounded_zero_difference_preserves_raw_page_without_index(
        owned, tmp_path, page_kind):
    from camctl.persistence.transaction import _apply_rows

    root = "/mnt/media_rw/emulated/DCIM"
    identity = f"{root}/already-seen.OSV"
    baseline = ("already-seen.OSV",) if page_kind == "nonempty_zero_difference" else ()
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, baseline)
    cursor = DirectoryCursor(
        DeviceBinding("cam-1", "dji-action6"),
        (root, "/mnt/media_rw/sd/DCIM"), 0, identity,
    )
    entries = ([_osmo_entry(identity, size=0)]
               if page_kind == "nonempty_zero_difference" else [])
    request = _request(runtime, ticket, _actual(
        ticket, entries=entries,
        next_cursor=cursor if entries else None,
        finalized=not entries,
    ))
    key = new_operation_key()
    completed = []

    class BoundedEmptyDifference:
        def plan(self, scope):
            finite = SaveResultPageCommand(request, key).plan(scope)
            assert len(finite.events) == 1
            current = deepcopy(finite.state_rows)
            previous = None

            def context(event, transaction):
                nonlocal previous
                if previous is not None:
                    _apply_rows(current, (previous,))
                previous = event
                return EventContext(transaction, finite.owners, deepcopy(current),
                    transaction_rows=deepcopy(finite.state_rows),
                    read_coverage=finite.read_coverage, baseline_reads=finite.baseline_reads)

            return BoundedCommandPlan(
                events=iter(finite.events), context=context, result=finite.result,
                complete=lambda connection: completed.append(True),
                result_page_write_scope=finite.result_page_write_scope)

    receipt = commit_operation(BoundedEmptyDifference(), key, owned)
    assert receipt.kind == "completed", receipt.error
    assert completed == [True]
    assert receipt.result.event_id == receipt.boundary.last_event_id
    saved = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [entry.identity for entry in saved.page.entries] == [entry["identity"] for entry in entries]
    assert saved.file_ids == ()
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM result_page_files WHERE page_event_id=?",
        (receipt.result.event_id,),
    ).fetchone() == (0,)
    registry = load_event_registry()["events"]["RESULT_PAGE"]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE transaction_id=? AND event_type=? "
        "AND json_extract(body_json,'$.reason')=?",
        (receipt.boundary.txn_id, registry["id"],
         registry["branches"]["INDEX_FILES"]["reason"]),
    ).fetchone() == (0,)


@pytest.mark.parametrize("tamper", [
    "append_omits_difference", "append_adds_out_of_page_file",
    "index_replaces_identity", "index_adds_extra_relation", "index_wrong_page",
    "index_wrong_created_event", "index_wrong_family", "index_wrong_owner",
    "index_wrong_row_id",
])
@pytest.mark.asyncio
async def test_atomic_page_guard_recomputes_the_difference_and_index_rows(owned, tmp_path, tamper):
    root = "/mnt/media_rw/emulated/DCIM/CAM"
    identity = f"{root}/guard.OSV"
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ())
    outside_identity = f"{root}/outside-page.OSV"
    outside_file_id = None
    if tamper in ("append_adds_out_of_page_file", "index_adds_extra_relation"):
        outside_file_id = await _save_out_of_page_file(
            runtime, ticket, owned, outside_identity)
    request = _request(runtime, ticket, _actual(
        ticket, entries=[_osmo_entry(identity, size=0)], finalized=True))
    key = new_operation_key()

    class TamperedSave:
        def plan(self, scope):
            plan = SaveResultPageCommand(request, key).plan(scope)
            events = list(plan.events)
            event_specs = load_event_registry()["events"]
            page_type = event_specs["RESULT_PAGE"]["id"]
            append_reason = event_specs["RESULT_PAGE"]["branches"]["APPEND"]["reason"]
            index_reason = event_specs["RESULT_PAGE"]["branches"]["INDEX_FILES"]["reason"]
            index = next(i for i, event in enumerate(events)
                         if (event.event_type, event.reason) == (page_type, index_reason))
            if tamper in ("append_omits_difference", "append_adds_out_of_page_file"):
                append = next(i for i, event in enumerate(events)
                              if (event.event_type, event.reason) == (page_type, append_reason))
                evidence = dict(events[append].evidence)
                raw = dict(evidence["result_page"])
                if tamper == "append_omits_difference":
                    raw["file_ids"] = []
                else:
                    raw["file_ids"] = [*raw["file_ids"],
                                       [outside_identity, outside_file_id]]
                evidence["result_page"] = raw
                events[append] = replace(events[append], evidence=evidence)
            else:
                indexed = events[index]
                events[index] = _tamper_index_event(
                    indexed, tamper, root, outside_file_id)
            owners = dict(plan.owners)
            if tamper == "index_adds_extra_relation":
                indexed = events[index]
                owners[("result_page_files", indexed.rows[-1].row_id)] = owners[
                    ("result_page_files", indexed.rows[0].row_id)]
            return replace(plan, events=tuple(events), owners=owners)

    receipt = commit_operation(TamperedSave(), key, owned)
    assert receipt.kind == "rolled_back"
    assert receipt.error is not None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)
    ).fetchone() == (0,)
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (
        (1 if outside_file_id is not None else 0),)
    assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)
@pytest.mark.parametrize("count", [1, 127, 128])
@pytest.mark.asyncio
async def test_bounded_entry_accepts_only_actual_append_then_index(owned, tmp_path, count):
    from camctl.persistence.transaction import _apply_rows

    root = "/mnt/media_rw/emulated/DCIM/CAM"
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ())
    request = _request(runtime, ticket, _actual(
        ticket, entries=[_osmo_entry(f"{root}/bounded-{index:03d}.OSV", size=0)
                         for index in range(count)], finalized=True))
    key = new_operation_key()
    completed = []

    class BoundedSave:
        def plan(self, scope):
            finite = SaveResultPageCommand(request, key).plan(scope)
            current = deepcopy(finite.state_rows)
            created_ids = [event.rows[0].row_id for event in finite.events
                           if event.event_type == load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]["id"]]
            for file_id in created_ids:
                current.get("device_files", {}).pop(file_id, None)
            events = iter(finite.events)
            previous = None

            def context(event, transaction):
                nonlocal previous
                if previous is not None:
                    _apply_rows(current, (previous,))
                previous = event
                return EventContext(transaction, finite.owners, deepcopy(current),
                    transaction_rows=deepcopy(finite.state_rows),
                    read_coverage=finite.read_coverage, baseline_reads=finite.baseline_reads)

            def complete(connection):
                completed.append(True)

            return BoundedCommandPlan(
                events=events,
                context=context,
                result=finite.result,
                complete=complete,
                result_page_write_scope=finite.result_page_write_scope,
            )

    receipt = commit_operation(BoundedSave(), key, owned)
    assert receipt.kind == "completed", receipt.error
    assert completed == [True]
    assert receipt.result.event_id == receipt.boundary.last_event_id - 1
    saved = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert saved.ref == receipt.result
    assert len(saved.file_ids) == count
    index = owned.connection.execute(
        "SELECT event_type,json_extract(body_json,'$.reason') FROM history_events WHERE id=?",
        (receipt.boundary.last_event_id,),
    ).fetchone()
    registry = load_event_registry()["events"]["RESULT_PAGE"]
    assert index == (registry["id"], registry["branches"]["INDEX_FILES"]["reason"])
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM json_each((SELECT body_json FROM history_events WHERE id=?), '$.rows')",
        (receipt.boundary.last_event_id,),
    ).fetchone() == (count,)


@pytest.mark.parametrize("tamper", [
    "append_omits_difference", "append_adds_out_of_page_file",
    "index_replaces_identity", "index_adds_extra_relation", "index_wrong_page",
    "index_wrong_created_event", "index_wrong_family", "index_wrong_owner",
    "index_wrong_row_id",
])
@pytest.mark.asyncio
async def test_bounded_entry_rejects_mutated_page_group(owned, tmp_path, tamper):
    from camctl.persistence.transaction import _apply_rows

    root = "/mnt/media_rw/emulated/DCIM/CAM"
    identity = f"{root}/bounded-tamper.OSV"
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ())
    outside_identity = f"{root}/outside-page.OSV"
    outside_file_id = None
    if tamper in ("append_adds_out_of_page_file", "index_adds_extra_relation"):
        outside_file_id = await _save_out_of_page_file(
            runtime, ticket, owned, outside_identity)
    request = _request(runtime, ticket, _actual(
        ticket, entries=[_osmo_entry(identity, size=0)], finalized=True))
    key = new_operation_key()
    completed = []
    registry = load_event_registry()["events"]
    page_type = registry["RESULT_PAGE"]["id"]
    append_reason = registry["RESULT_PAGE"]["branches"]["APPEND"]["reason"]
    index_reason = registry["RESULT_PAGE"]["branches"]["INDEX_FILES"]["reason"]

    class TamperedBoundedSave:
        def plan(self, scope):
            finite = SaveResultPageCommand(request, key).plan(scope)
            current = deepcopy(finite.state_rows)
            create_type = registry["DEVICE_FILE_OBSERVED"]["id"]
            for event in finite.events:
                if event.event_type == create_type:
                    current.get("device_files", {}).pop(event.rows[0].row_id, None)
            events = list(finite.events)
            previous = None
            owners = dict(finite.owners)
            if tamper in ("append_omits_difference", "append_adds_out_of_page_file"):
                append_index = next(i for i, event in enumerate(events)
                                    if (event.event_type, event.reason) == (page_type, append_reason))
                evidence = dict(events[append_index].evidence)
                raw = dict(evidence["result_page"])
                if tamper == "append_omits_difference":
                    raw["file_ids"] = []
                else:
                    raw["file_ids"] = [*raw["file_ids"],
                                       [outside_identity, outside_file_id]]
                evidence["result_page"] = raw
                events[append_index] = replace(events[append_index], evidence=evidence)
            else:
                index = next(i for i, event in enumerate(events)
                             if (event.event_type, event.reason) == (page_type, index_reason))
                events[index] = _tamper_index_event(
                    events[index], tamper, root, outside_file_id)
                if tamper == "index_adds_extra_relation":
                    owners[("result_page_files", events[index].rows[-1].row_id)] = owners[
                        ("result_page_files", events[index].rows[0].row_id)]
            event_iter = iter(events)

            def context(event, transaction):
                nonlocal previous
                if previous is not None:
                    _apply_rows(current, (previous,))
                previous = event
                return EventContext(transaction, owners, deepcopy(current),
                    transaction_rows=deepcopy(finite.state_rows),
                    read_coverage=finite.read_coverage, baseline_reads=finite.baseline_reads)

            return BoundedCommandPlan(
                events=event_iter, context=context, result=finite.result,
                complete=lambda connection: completed.append(True),
                result_page_write_scope=finite.result_page_write_scope)

    receipt = commit_operation(TamperedBoundedSave(), key, owned)
    assert receipt.kind == "rolled_back"
    assert receipt.error is not None
    assert completed == []
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)
    ).fetchone() == (0,)
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (
        (1 if outside_file_id is not None else 0),)
    assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.asyncio
async def test_create_only_prefix_cannot_complete_without_page(owned, tmp_path, bounded):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ())
    run = row_facts(owned.connection, "operation_runs", ticket.run_id)
    action = row_facts(owned.connection, "actions", run["action_id"])
    activity = row_facts(owned.connection, "device_activities", run["activity_id"])
    attempt_id = owned.connection.execute(
        "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id),
    ).fetchone()[0]
    attempt = row_facts(owned.connection, "operation_attempts", attempt_id)
    identity = "/mnt/media_rw/emulated/DCIM/CAM/no-append.OSV"
    file_id = next_row_id(owned.connection, "device_files")
    command = FileObservationSave(action["id"], identity, {"path": identity},
        1_750_000_000_000_000, "no-append.OSV", "video")
    row = file_discovery_row(command, file_id,
        file_identity_key(action["device_id"], action["driver_id"], identity))
    event = load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]
    operation_key = new_operation_key()
    completed = []

    class CreateOnly:
        def plan(self, scope):
            allocation = scope.allocate(1)
            proposal = event_envelope(allocation.first_event_id, allocation.txn_id,
                event["id"], event["branches"]["CREATE"]["reason"], (row,),
                command.occurred_at)
            owners = {("device_files", file_id): ("device_file", file_id)}
            state = {
                "actions": {action["id"]: action},
                "operation_runs": {run["id"]: run},
                "operation_attempts": {attempt_id: attempt},
                "device_activities": {activity["id"]: activity},
                "device_files": {}, "outputs": {},
            }

            def context(actual, transaction):
                return EventContext(transaction, owners, state)

            page_scope = ResultPageWriteScope(
                action["id"], activity["id"], run["id"], attempt_id, 1)
            if bounded:
                return BoundedCommandPlan(
                    events=(proposal,), context=context, result=None,
                    complete=lambda connection: completed.append(True),
                    result_page_write_scope=page_scope,
                )
            return CommandPlan(
                events=(proposal,), owners=owners, state_rows=state, result=None,
                complete_result=lambda connection, result: completed.append(True),
                result_page_write_scope=page_scope,
            )

    receipt = commit_operation(CreateOnly(), operation_key, owned)
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, ConsistencyError)
    assert "规范组" in str(receipt.error)
    assert completed == []
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(operation_key),),
    ).fetchone() == (0,)
