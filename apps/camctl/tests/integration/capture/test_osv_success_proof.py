"""修复成功的保存、独立守卫及提升共同核对原媒体事实与身份。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media_flow import run_recording_media, load_processing_status
from camctl.capture.processing import RepairSuccess, RepairResultSave, RepairOutcome
from camctl.contracts.json_values import parse_exact_json
from camctl.persistence.repositories.transactions import encode_json_value
from camctl.contracts.values import new_operation_key
from camctl.host_files.media import MediaProbe
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_osv_input_failure import prepared_input
from .test_recording_media_link import _NOW
from ..support.capture_media_cases import (
    processing_success_event, promotion_event, validate_media_event, capture_finish_request, media_cases,
)

_OUTPUT_PROBE_FAULTS = (
    "missing_probe", "tool_error", "container", "short_container",
    "missing_video", "extra_video", "cover", "codec", "dimensions", "missing_audio",
    "audio_codec", "short_video", "short_audio", "unknown_duration",
)


async def prepared_repair(tmp_path, *, check_required=False):
    owned, runtime, flow, source_id, source, tools, entry = (await prepared_input(tmp_path, check_required, "valid"))
    try:
        await run_recording_media(flow, 1, 1, source_id, target_extension="osv")
        status = load_processing_status(owned, 1)
        assert status.repair_state == 4
        output = replace(source, duration_s=Decimal("60"), streams=tuple(
            replace(s, duration_s=Decimal("60")) for s in source.streams if s.attached_pic is not True))
        return owned, status.repair_output_file_id, source, output
    except BaseException as error:
        try:
            owned.connection.close()
        except BaseException as closing:
            error.add_note(f"关闭观察连接时出错：{closing!r}")
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["valid", "tool_failed"])
@pytest.mark.parametrize("copy_present", [True,False])
async def test_saved_reliable_input_failure_survives_later_observation_and_missing_copy(tmp_path, later, copy_present):
    from camctl.capture.media_facts import saved_osv_input_issues
    owned, output_id, source, output = await prepared_repair(tmp_path, check_required=True)
    try:
        rows = owned.connection.execute("SELECT id,body_json FROM history_events WHERE event_type=19 ORDER BY id").fetchall()
        probes = [(event_id,parse_exact_json(raw)) for event_id,raw in rows if "source_probe" in parse_exact_json(raw)["evidence"]]
        assert len(probes) == 2
        probes[0][1]["evidence"]["source_probe"]["duration_s"] = Decimal("55")
        if later == "tool_failed":
            from camctl.capture.media_facts import probe_document
            probes[1][1]["evidence"]["source_probe"] = probe_document(MediaProbe(None,"tool_failed: exit=1"))
        for event_id,body in probes:
            owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?",(encode_json_value(body),event_id))
        if not copy_present:
            owned.connection.execute("DELETE FROM file_copies")
        owned.connection.commit()
        assert any(issue.code == "container_too_short" for issue in saved_osv_input_issues(owned.connection,1,60000))
        assert CaptureRepository().complete_repair_output(RepairSuccess(1,output_id,2048,"a"*64,_NOW+200,
            output_probe=output),new_operation_key(),owned).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


def mutate(probe, fault):
    if fault == "missing_probe": return None
    if fault == "tool_error": return MediaProbe(None, "tool_failed: exit=1")
    if fault == "container": return replace(probe, format_name="matroska,webm")
    if fault == "short_container": return replace(probe, duration_s=Decimal("55"))
    streams = list(probe.streams)
    if fault == "missing_video": streams.pop(1)
    if fault == "extra_video": streams.append(replace(streams[0], index=8))
    if fault == "cover": streams.append(replace(streams[0], index=8, attached_pic=True))
    if fault == "codec": streams[0] = replace(streams[0], codec_name="h264")
    if fault == "dimensions": streams[0] = replace(streams[0], width=1920)
    if fault == "missing_audio": streams = [s for s in streams if s.codec_type != "audio"]
    if fault == "audio_codec": streams[2] = replace(streams[2], codec_name="opus")
    if fault == "short_video": streams[0] = replace(streams[0], duration_s=Decimal("55"))
    if fault == "short_audio": streams[2] = replace(streams[2], duration_s=Decimal("55"))
    if fault == "unknown_duration": streams[0] = replace(streams[0], duration_s=None)
    return replace(probe, streams=tuple(streams))


def alter_source_history(owned, change):
    rows = owned.connection.execute("SELECT id,body_json FROM history_events WHERE event_type=19").fetchall()
    for event_id, raw in rows:
        body = parse_exact_json(raw)
        if "source_probe" in body["evidence"]:
            body["evidence"]["source_probe"] = change(body["evidence"]["source_probe"])
            if body["evidence"]["source_probe"] is None: del body["evidence"]["source_probe"]
            owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?", (encode_json_value(body), event_id))
    owned.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["complete", "result"])
async def test_both_success_commands_reject_invalid_output_probes(tmp_path, entry):
    owned, output_id, source, output = await prepared_repair(tmp_path)
    try:
        if entry == "result":
            owned.connection.execute("UPDATE intermediate_files SET size_bytes=2048,sha256=? WHERE id=?", ("a"*64, output_id))
            owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        for fault in _OUTPUT_PROBE_FAULTS:
            try:
                candidate = mutate(output, fault)
                if entry == "complete":
                    command = RepairSuccess(1, output_id, 2048, "a"*64, _NOW+200, output_probe=candidate)
                    save = CaptureRepository().complete_repair_output
                else:
                    command = RepairResultSave(1, RepairOutcome.SUCCEEDED, _NOW+200,
                        output_file_id=output_id, output_probe=candidate)
                    save = CaptureRepository().save_repair_result
                outcome = save(command, new_operation_key(), owned)
                assert outcome.kind is DbOutcomeKind.ROLLED_BACK, (entry, fault, outcome.error)
                assert not owned.connection.in_transaction, (entry, fault, "事务未结束")
                assert tuple(owned.connection.iterdump()) == before, (entry, fault, "状态库发生变化")
            except Exception as error:
                error.add_note(f"entry={entry}, fault={fault}")
                raise
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["source_missing",
    "source_short", "source_unknown", "source_tool_error", "source_container", "source_stream_short",
    "source_identity", "copy_missing", "copy_unverified", "copy_path", "copy_size", "wrong_output"])
async def test_both_success_commands_reject_incomplete_media_or_wrong_identity(tmp_path, fault):
    owned, output_id, source, output = await prepared_repair(tmp_path)
    try:
        if fault == "source_missing": alter_source_history(owned, lambda original: None)
        if fault == "source_short": alter_source_history(owned, lambda original: {**original, "duration_s": Decimal("55")})
        if fault in ("source_unknown", "source_tool_error", "source_container", "source_stream_short"):
            from camctl.capture.media_facts import read_probe, probe_document
            mutation = {"source_unknown":"unknown_duration", "source_tool_error":"tool_error",
                "source_container":"container", "source_stream_short":"short_video"}[fault]
            alter_source_history(owned, lambda original: probe_document(mutate(read_probe(original), mutation)))
        if fault == "source_identity":
            owned.connection.execute("UPDATE recording_processing SET source_device_file_id=NULL WHERE id=1")
            owned.connection.commit()
        if fault == "copy_missing":
            owned.connection.execute("DELETE FROM file_copies")
            owned.connection.commit()
        if fault == "copy_unverified":
            owned.connection.execute("UPDATE file_copies SET verification_state=1")
            owned.connection.commit()
        if fault == "copy_path":
            owned.connection.execute("UPDATE intermediate_files SET relative_path='recording-inputs/999.osv' WHERE purpose=2")
            owned.connection.commit()
        if fault == "copy_size":
            owned.connection.execute("UPDATE intermediate_files SET size_bytes=1 WHERE purpose=2")
            owned.connection.commit()
        if fault == "wrong_output":
            output_id += 1
            owned.connection.execute("INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,retention_state,cleanup_state,size_bytes,sha256,created_event_id,last_event_id,change_count) VALUES (?,1,4,?,1,1,NULL,NULL,1,1,1)",
                (output_id, f"derived/{output_id}.mp4"))
            owned.connection.commit()
        repository = CaptureRepository()
        for entry in ("complete", "result"):
            try:
                if entry == "complete":
                    command = RepairSuccess(1, output_id, 2048, "a"*64, _NOW+200, output_probe=output)
                    save = repository.complete_repair_output
                else:
                    owned.connection.execute("UPDATE intermediate_files SET size_bytes=2048,sha256=? WHERE id=?", ("a"*64, output_id))
                    owned.connection.commit()
                    command = RepairResultSave(1, RepairOutcome.SUCCEEDED, _NOW+200,
                        output_file_id=output_id, output_probe=output)
                    save = repository.save_repair_result
                before = tuple(owned.connection.iterdump())
                outcome = save(command, new_operation_key(), owned)
                assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.error
                assert not owned.connection.in_transaction
                assert tuple(owned.connection.iterdump()) == before
            except Exception as error:
                error.add_note(f"entry={entry}, fault={fault}")
                raise
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["missing", "wrong_plan"])
async def test_independent_success_guards_require_the_original_plan_reader(tmp_path, scope):
    from camctl.history.validators import EventValidationError
    owned, output_id, source, output = await prepared_repair(tmp_path)
    try:
        command = RepairSuccess(1, output_id, 2048, 'a' * 64, _NOW + 200, output_probe=output)
        event, proposal = processing_success_event(owned, command)
        owned.connection.execute('BEGIN')
        validate_media_event(owned, event, proposal)
        with pytest.raises(EventValidationError):
            validate_media_event(owned, event, proposal, scope=scope)
        owned.connection.rollback()
        outcome = CaptureRepository().complete_repair_output(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        event, proposal = promotion_event(owned, output_id)
        owned.connection.execute('BEGIN')
        validate_media_event(owned, event, proposal)
        with pytest.raises(EventValidationError):
            validate_media_event(owned, event, proposal, scope=scope)
    finally:
        owned.connection.rollback()
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_probe", "missing_video", "short_container", "short_audio", "codec"])
async def test_independent_processed_guard_rejects_bad_output_even_with_complete_bytes(tmp_path, fault):
    from camctl.capture.media_facts import probe_document
    from camctl.history.validators import EventValidationError
    owned, output_id, source, output = await prepared_repair(tmp_path)
    try:
        command = RepairSuccess(1, output_id, 2048, 'a' * 64, _NOW + 200, output_probe=output)
        event, proposal = processing_success_event(owned, command)
        owned.connection.execute('BEGIN')
        validate_media_event(owned, event, proposal)
        mutated = replace(event, evidence={} if fault == 'missing_probe' else {
            'output_probe': probe_document(mutate(output, fault))})
        with pytest.raises(EventValidationError):
            validate_media_event(owned, mutated, proposal)
    finally:
        owned.connection.rollback()
        owned.connection.close()


@pytest.mark.asyncio
async def test_promotion_rejects_saved_success_with_missing_media_proof(tmp_path):
    owned, output_id, source, output = await prepared_repair(tmp_path)
    try:
        owned.connection.execute('UPDATE recording_processing SET repair_state=5,repair_output_file_id=? WHERE id=1', (output_id,))
        owned.connection.execute('UPDATE intermediate_files SET size_bytes=2048,sha256=? WHERE id=?', ('a' * 64, output_id))
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        result = CaptureRepository().finish_capture(capture_finish_request(owned, output_id), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["complete", "result"])
@pytest.mark.parametrize("commit", ["normal", "before", "after"])
async def test_qualified_success_reopens_and_replays_original_probe_without_new_tools(tmp_path, entry, commit):
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    from ..bootstrap.test_binding_transactions import _CommitFailure
    owned, output_id, source, output = await prepared_repair(tmp_path)
    if entry == "complete":
        command = RepairSuccess(1, output_id, 2048, "a"*64, _NOW+200, output_probe=output)
        save = CaptureRepository().complete_repair_output
    else:
        owned.connection.execute("UPDATE intermediate_files SET size_bytes=2048,sha256=? WHERE id=?", ("a"*64, output_id))
        owned.connection.commit()
        command = RepairResultSave(1, RepairOutcome.SUCCEEDED, _NOW+200,
            output_file_id=output_id, output_probe=output)
        save = CaptureRepository().save_repair_result
    key = new_operation_key()
    first = save(command, key, owned if commit == "normal" else replace(owned,
        connection=_CommitFailure(owned.connection, commit == "after")))
    assert first.kind is (DbOutcomeKind.COMPLETED if commit == "normal" else DbOutcomeKind.UNKNOWN), first.error
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        recovered = save(command, key, reopened)
        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        assert load_processing_status(reopened, 1).source_probe == source
        before = tuple(reopened.connection.iterdump())
        assert save(command, key, reopened).kind is DbOutcomeKind.COMPLETED
        assert tuple(reopened.connection.iterdump()) == before
        changed = replace(command, output_probe=mutate(output, "codec"))
        assert save(changed, key, reopened).kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(reopened.connection.iterdump()) == before
        # 重送只读取历史原申请；没有媒体端口，也没有第二次输出准备登记。
        assert reopened.connection.execute("SELECT COUNT(*) FROM intermediate_files WHERE purpose=4").fetchone() == (1,)
        from camctl.history.reads import PlanReadScope
        from camctl.persistence.event_reads import SqlEventReads
        from camctl.contracts.values import ConsistencyError
        reopened.connection.execute("BEGIN")
        SqlEventReads(reopened.connection, PlanReadScope(1,128)).repair_media_proof(1,1,output_id,saved=True)
        with pytest.raises(ConsistencyError):
            SqlEventReads(reopened.connection, PlanReadScope(2,128)).repair_media_proof(1,1,output_id,saved=True)
        with pytest.raises(ConsistencyError):
            SqlEventReads(reopened.connection, PlanReadScope(1,128)).repair_media_proof(1,42,output_id,saved=True)
        reopened.connection.rollback()
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", _OUTPUT_PROBE_FAULTS)
async def test_promotion_and_reuse_recheck_saved_media_qualification(tmp_path, fault):
    from camctl.history.reads import PlanReadScope
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.contracts.values import ConsistencyError
    from camctl.capture.media_facts import probe_document
    owned, output_id, source, output = await prepared_repair(tmp_path)
    command = RepairSuccess(1, output_id, 2048, "a"*64, _NOW+200, output_probe=output)
    key = new_operation_key()
    try:
        assert CaptureRepository().complete_repair_output(command,key,owned).kind is DbOutcomeKind.COMPLETED
        event_id, raw = owned.connection.execute("SELECT id,body_json FROM history_events WHERE event_type=19 ORDER BY id DESC LIMIT 1").fetchone()
        body = parse_exact_json(raw)
        candidate = mutate(output,fault)
        if candidate is None:
            del body["evidence"]["output_probe"]
        else:
            body["evidence"]["output_probe"] = probe_document(candidate)
        owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?", (encode_json_value(body),event_id))
        owned.connection.commit()
        owned.connection.execute("BEGIN")
        with pytest.raises(ConsistencyError):
            SqlEventReads(owned.connection, PlanReadScope(1,128)).repair_media_proof(1,1,output_id,saved=True)
        owned.connection.rollback()
        bad_command = replace(command,output_probe=candidate)
        assert CaptureRepository().complete_repair_output(bad_command,key,owned).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()
