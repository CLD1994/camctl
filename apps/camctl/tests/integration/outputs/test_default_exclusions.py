"""默认取回按最终产物身份排除，保存与恢复沿用原请求。"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventValidationError
from camctl.outputs.definitions import SelectionMode
from camctl.outputs.sources import SelectionSnapshot, select_outputs
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_catalog_integrity import selection_database
from .test_output_family import family_database
from .test_product_competition import competition_database
from .test_qualification import qualification_environment
from .test_sources import _NOW, _fixed_resolution
from ..support.output_planning import capture_operation, validate_static_event
from ..support.output_storage_cases import observe_product_competition
from ..support.outputs import read_test_runtime
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.devices.read_session import ReadSession
from .test_source_digest_failure_settlement import _ReadStream


def _exclude(owned, identities=(703,), action_id=30):
    params = {"source": {"action_instance_id": "11"}}
    if identities is not None:
        params["exclude_output_ids"] = [str(identity) for identity in identities]
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=?, execution_spec_json=? WHERE id=?",
        (json.dumps({"params": params}), '{"selection_mode":1}', action_id),
    )
    owned.connection.commit()


@pytest.mark.parametrize("extra,valid", [
    ({"exclude_output_ids": ["1"]}, True),
    ({"filter": "default", "exclude_output_ids": ["1"]}, True),
    ({"exclude_output_ids": []}, False),
    ({"exclude_output_ids": ["1", "1"]}, False),
    ({"exclude_output_ids": [1]}, False),
    ({"exclude_output_ids": ["01"]}, False),
    ({"exclude_output_ids": ["9223372036854775808"]}, False),
    ({"filter": "preview", "exclude_output_ids": ["1"]}, False),
    ({"output_ids": ["2"], "exclude_output_ids": ["1"]}, False),
    ({"filter": "preview", "purpose": "auto_preview", "exclude_output_ids": ["1"]}, False),
])
def test_public_default_exclusion_combinations(extra, valid):
    validator = create_validator({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                  "$ref": "plan.schema.json#/$defs/obtain_params"})
    params = {"source": {"action_name": "拍摄"}, **extra}
    assert (not validation_errors(validator, params)) is valid


@pytest.mark.parametrize("excluded,remaining", [
    ((703,), (705,)),
    ((701, 702, 999), (703, 705)),
    ((703, 705), ()),
])
def test_default_selection_filters_final_repaired_identity(selection_database, excluded, remaining):
    owned = selection_database
    _exclude(owned, excluded)
    facts = outputs.load_selection_facts(owned.connection, 11)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT,
                              excluded_output_ids=frozenset(excluded))
    assert snapshot.selected_output_ids == remaining
    assert snapshot.source_error_code is None
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.snapshot.selected_output_ids == remaining


def test_selection_guard_rechecks_original_exclusions(selection_database):
    owned = selection_database
    _exclude(owned)
    facts = outputs.load_selection_facts(owned.connection, 11)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT,
                              excluded_output_ids=frozenset({703}))
    proposal = capture_operation(owned, lambda actual: outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), actual))
    event = proposal.events[0]
    validate_static_event(event, proposal)
    changed = deepcopy(proposal.state_rows)
    changed["actions"][30]["input_fields_json"]["params"]["exclude_output_ids"] = ["705"]
    with pytest.raises(EventValidationError):
        validate_static_event(event, replace(proposal, state_rows=changed))


def test_excluded_selection_cannot_restore_excluded_target(selection_database):
    owned = selection_database
    _exclude(owned)
    before = tuple(owned.connection.iterdump())
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(owned.connection, 11), SelectionMode.DEFAULT), _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


def test_all_excluded_finishes_without_delivery_and_recovers(selection_database):
    owned = selection_database
    _exclude(owned, (703, 705))
    repository = outputs.OutputsRepository()
    request = outputs.FixSelection(61, SelectionSnapshot(True), _NOW)
    key = new_operation_key()
    fixed = repository.fix_selection(request, key, owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    reopened = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        for operation_key in (key, new_operation_key()):
            again = repository.fix_selection(request, operation_key, reopened)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert again.value.snapshot.items == ()
        finish_key = new_operation_key()
        for operation_key in (finish_key, finish_key, new_operation_key()):
            receipt = repository.finish_obtain(outputs.FinishObtain(30, _NOW + 1), operation_key, reopened)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            assert (receipt.value.action_status, receipt.value.prepared, receipt.value.failed) == (3, 0, 0)
            assert receipt.value.plan_status == 3
        assert reopened.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)
    finally:
        reopened.connection.close()


@pytest.mark.parametrize("invalid", ["no_exclusion", "no_sources", "pending_source", "recovered_no_exclusion"])
def test_empty_selection_needs_fixed_exclusion_proof(selection_database, invalid):
    owned = selection_database
    _exclude(owned, None if invalid in ("no_exclusion", "recovered_no_exclusion") else (703, 705))
    owned.connection.execute("UPDATE obtain_source_selections SET status=? WHERE id=61",
                             (1 if invalid == "pending_source" else 2,))
    if invalid == "no_sources":
        owned.connection.execute("DELETE FROM obtain_source_selections WHERE id=61")
        owned.connection.execute("DELETE FROM action_dependencies WHERE id=41")
    if invalid == "recovered_no_exclusion":
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=30")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    receipt = outputs.OutputsRepository().finish_obtain(
        outputs.FinishObtain(30, _NOW + 1), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.asyncio
async def test_obtain_runtime_consumes_saved_default_exclusions(selection_database, tmp_path):
    owned = selection_database
    _exclude(owned)
    class Source:
        calls = []
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.calls.append(source)
            return ReadSession(source, offset, _ReadStream(b"x" * source.size_bytes), idle_timeout_s)
    driver = Source()
    async with read_test_runtime(owned, driver, EVIDENCE, staging=tmp_path / "staging",
            occurred_at=lambda: _NOW) as runtime:
        await advance_obtain(runtime)
    assert owned.connection.execute(
        "SELECT output_id FROM obtain_items WHERE selection_id=61 ORDER BY id").fetchall() == [(705,)]
    assert [source.file_id for source in driver.calls] == ["file-0505"]


def test_locally_qualified_final_target_is_excluded_even_after_host_cleanup(selection_database):
    owned = selection_database
    _exclude(owned, (703, 705))
    owned.connection.execute("UPDATE outputs SET availability=3, cleanup_status=4 WHERE id=703")
    owned.connection.commit()
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, SelectionSnapshot(True), _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.snapshot.items == ()


def test_unfixed_excluded_obtain_does_not_block_product_successor(competition_database):
    owned = competition_database
    _exclude(owned, (701,), action_id=91)
    assert not observe_product_competition(owned, action_id=31, output_id=701, occurred_at=_NOW)
    _exclude(owned, None, action_id=91)
    assert observe_product_competition(owned, action_id=31, output_id=701, occurred_at=_NOW)
