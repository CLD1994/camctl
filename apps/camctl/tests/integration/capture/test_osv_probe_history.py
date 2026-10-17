"""逐流输入事实只进入内部历史，重开状态库后仍能恢复。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from camctl.capture.media_flow import load_processing_status
from camctl.capture.processing import CheckResultSave, RepairStart
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import open_existing, DbConfig, DbOpenMode
from camctl.host_files.media import MediaProbe, MediaStream

from .test_media_processing import (environment, _save, _check_required,
    _completed_observation, _NOW, _assert_saved, _json_column)


def dual_probe():
    return MediaProbe(Decimal("75"), None, (
        MediaStream(0, "video", "hevc", 3840, 3840, Decimal("75"), False),
        MediaStream(2, "video", "hevc", 3840, 3840, Decimal("75"), False),
        MediaStream(3, "audio", "aac", None, None, Decimal("75"), False),
        MediaStream(1, "video", "mjpeg", 320, 180, None, True),
    ), "mov,mp4,m4a,3gp,3g2,mj2")



def test_source_probe_history_roundtrip_and_original_request_identity(environment):
    owned = environment
    _assert_saved(_save(owned, _check_required()))
    key = new_operation_key()
    command = CheckResultSave(1, _completed_observation("75"), _NOW + 2,
        source_probe=dual_probe())
    repository = CaptureRepository()
    result = repository.save_check_result(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert set(_json_column(owned, "media_json")) == {"check_status", "duration"}
    body = parse_exact_json(owned.connection.execute(
        "SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert body["evidence"]["source_probe"]["streams"][1]["index"] == 2
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    owned.connection.close()
    reopened = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert load_processing_status(reopened, 1).source_probe == dual_probe()
        replay = repository.save_check_result(command, key, reopened)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        altered = replace(command, source_probe=replace(dual_probe(), format_name="matroska"))
        conflict = repository.save_check_result(altered, key, reopened)
        assert conflict.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        reopened.connection.close()


def test_direct_timing_repair_start_preserves_input_probe_without_claiming_check(environment):
    from camctl.capture.processing import (RepairBasis, RepairReason,
        RepairDecisionChoice, RepairDecisionSave)
    from .test_media_processing import _check_not_needed
    owned = environment
    _assert_saved(_save(owned, _check_not_needed()))
    _assert_saved(_save(owned, RepairDecisionSave(1, RepairDecisionChoice.PENDING,
        RepairBasis(RepairReason.THRESHOLD_REACHED, 60000, Decimal("70"),
            control_elapsed_ns=75_000_000_000), _NOW + 2)))
    owned.connection.execute("DELETE FROM intermediate_files WHERE id=700")
    owned.connection.commit()
    command = RepairStart(1, "mp4", _NOW + 3, source_probe=dual_probe())
    repository, key = CaptureRepository(), new_operation_key()
    saved = repository.start_repair_output(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    status = load_processing_status(owned, 1)
    assert status.check_decision == 2 and status.check_state == 1
    assert status.source_probe == dual_probe()
    repeated = repository.start_repair_output(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    changed = repository.start_repair_output(
        replace(command, source_probe=replace(dual_probe(), format_name="matroska")), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK
