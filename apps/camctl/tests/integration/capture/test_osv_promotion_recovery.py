"""正式提升后的原键重送与终态新键恢复仍沿原媒体成功证据。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.processing import RepairSuccess
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishDisposition
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.persistence.transaction import encode_json_value

from .test_osv_success_proof import prepared_repair, alter_source_history
from .test_output_promotion import _repaired_draft, promotion_environment
from .test_recording_finish import _finish, _original_draft
from .test_recording_media_link import _NOW


async def promoted(tmp_path):
    owned, output_id, source, probe = await prepared_repair(tmp_path)
    saved = CaptureRepository().complete_repair_output(RepairSuccess(41,output_id,2048,
        "a"*64,_NOW+200,output_probe=probe),new_operation_key(),owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    source_id = owned.connection.execute("SELECT source_device_file_id FROM recording_processing WHERE id=41").fetchone()[0]
    command = _finish(drafts=(_repaired_draft(output_id,batch=source_id),_original_draft(source_id)))
    key = new_operation_key()
    outcome = CaptureRepository().finish_capture(command,key,owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert owned.connection.execute("SELECT retention_state FROM intermediate_files WHERE id=?",(output_id,)).fetchone() == (3,)
    return owned, command, key, outcome.value


def reopen(owned):
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    return open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize("key_kind",["original","new"])
@pytest.mark.parametrize("reopened",[False,True])
async def test_qualified_promoted_osv_recovers_without_writes_and_pins_original_boundary(tmp_path,monkeypatch,key_kind,reopened):
    from camctl.persistence.repositories import capture
    owned, command, original_key, original = await promoted(tmp_path)
    if reopened: owned = reopen(owned)
    try:
        boundaries = []
        real_proof = capture.require_repair_media_proof
        def observed(*args,**kwargs):
            boundaries.append(kwargs.get("at_event_id"))
            return real_proof(*args,**kwargs)
        monkeypatch.setattr(capture,"require_repair_media_proof",observed)
        event_id = owned.connection.execute("SELECT created_event_id FROM outputs WHERE id=?",(original.output_ids[0],)).fetchone()[0]
        before = tuple(owned.connection.iterdump())
        outcome = CaptureRepository().finish_capture(command if key_kind == "original" else
            replace(command,occurred_at=command.occurred_at+999),
            original_key if key_kind == "original" else new_operation_key(),owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition is FinishDisposition.ALREADY
        assert outcome.value.output_ids == original.output_ids
        assert outcome.value.action_status == original.action_status
        assert outcome.value.plan_status == original.plan_status
        assert tuple(owned.connection.iterdump()) == before
        assert boundaries == [event_id]
    finally:
        owned.connection.close()


def alter_saved_proof(owned,fault):
    if fault in ("source_missing","source_short"):
        alter_source_history(owned,lambda probe: None if fault == "source_missing" else
            {**probe,"duration_s":Decimal("55")})
        return
    rows = owned.connection.execute("SELECT id,body_json FROM history_events WHERE event_type IN (19,26)").fetchall()
    changed = False
    for event_id,raw in rows:
        body = parse_exact_json(raw)
        if fault == "wrong_start":
            for row in body["rows"]:
                if row["table"] == "intermediate_files" and not row["before"]["exists"] and row["after"]["values"].get("purpose") == 4:
                    row["after"]["values"]["relative_path"] = "derived/999.mp4"
                    changed = True
        elif "output_probe" in body["evidence"]:
            probe = body["evidence"]["output_probe"]
            if fault == "output_missing": del body["evidence"]["output_probe"]
            elif fault == "output_short": probe["duration_s"] = Decimal("55")
            elif fault == "output_wrong": probe["streams"].pop(1)
            changed = True
        owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?",(encode_json_value(body),event_id))
    assert changed
    owned.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("key_kind",["original","new"])
@pytest.mark.parametrize("fault",["source_missing","source_short","output_missing","output_short","output_wrong","wrong_start"])
async def test_promoted_osv_recovery_rejects_unqualified_original_proof_after_reopen(tmp_path,key_kind,fault):
    owned,command,original_key,original = await promoted(tmp_path)
    alter_saved_proof(owned,fault)
    owned = reopen(owned)
    try:
        before = tuple(owned.connection.iterdump())
        outcome = CaptureRepository().finish_capture(command,original_key if key_kind == "original" else new_operation_key(),owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert tuple(row[0] for row in owned.connection.execute("SELECT id FROM outputs ORDER BY id")) == tuple(sorted(original.output_ids))
    finally:
        owned.connection.close()


@pytest.mark.parametrize("key_kind",["original","new"])
def test_legacy_promoted_output_recovers_without_new_osv_facts(tmp_path,key_kind):
    owned = promotion_environment(tmp_path)
    command = _finish(drafts=(_repaired_draft(),_original_draft(11)))
    key = new_operation_key()
    first = CaptureRepository().finish_capture(command,key,owned)
    assert first.kind is DbOutcomeKind.COMPLETED,first.error
    owned = reopen(owned)
    try:
        before = tuple(owned.connection.iterdump())
        recovered = CaptureRepository().finish_capture(command,key if key_kind == "original" else new_operation_key(),owned)
        assert recovered.kind is DbOutcomeKind.COMPLETED,recovered.error
        assert recovered.value.output_ids == first.value.output_ids
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()
