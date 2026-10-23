"""安装版 Action6 检查／来源真实提交后的可终止同步点。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import time


def _json_value(value):
    if is_dataclass(value):
        return asdict(value)
    return repr(value)


def _snapshot(owned, action_id: int) -> dict:
    db = owned.connection
    assert not db.in_transaction
    queries = {
        "action": "SELECT id,status,cancel_requested FROM actions WHERE id=?",
        "activities": "SELECT id,result_set_state,wait_completed_event_id FROM device_activities WHERE action_id=?",
        "sets": "SELECT id,version,state,member_count,published_event_id,assessment_json FROM segment_sets WHERE action_id=? ORDER BY id",
        "inspections": "SELECT i.id,i.member_id,i.state,i.result_event_id,i.result_json FROM segment_inspections i WHERE i.action_id=? ORDER BY i.id",
        "copies": "SELECT c.id,c.inspection_id,c.source_device_file_id,c.target_file_id,c.committed_bytes,c.source_size,c.verification_state,c.target_sha256 FROM file_copies c JOIN segment_inspections i ON i.id=c.inspection_id WHERE i.action_id=? ORDER BY c.id",
        "work_files": "SELECT id,relative_path,retention_state,cleanup_state,size_bytes,sha256 FROM intermediate_files WHERE owner_action_id=? ORDER BY id",
        "outputs": "SELECT id,kind,device_file_id,intermediate_file_id,availability FROM outputs WHERE source_action_id=? ORDER BY id",
        "scopes": "SELECT s.id,s.set_id,s.ordinal,s.output_id FROM segment_scope_members s JOIN segment_sets g ON g.id=s.set_id WHERE g.action_id=? ORDER BY s.id",
        "waiters": "SELECT d.action_id,a.status,s.id,s.status,i.id,i.output_id,i.status FROM action_dependencies d JOIN actions a ON a.id=d.action_id JOIN obtain_source_selections s ON s.dependency_id=d.id LEFT JOIN obtain_items i ON i.selection_id=s.id WHERE d.depends_on_action_id=? ORDER BY s.id,i.id",
        "deliveries": "SELECT d.id,d.action_id,d.output_id,d.status,c.id,c.verification_state,c.committed_bytes,c.source_size,c.target_sha256 FROM deliveries d LEFT JOIN file_copies c ON c.delivery_id=d.id WHERE d.action_id IN (SELECT action_id FROM action_dependencies WHERE depends_on_action_id=?) ORDER BY d.id",
    }
    result = {}
    for name, sql in queries.items():
        result[name] = [list(row) for row in db.execute(sql, (action_id,)).fetchall()]
    return result


def _hold_after_commit(stage: str, owned, request, key) -> None:
    marker = Path(os.environ["CAMCTL_ACTION6_TEST_OBSERVER_MARKER"])
    if marker.exists():
        return
    action_id = getattr(request, "action_id", None)
    if type(action_id) is not int:
        action_id = getattr(request, "inspection_id", None)
        if type(action_id) is int:
            row = owned.connection.execute(
                "SELECT action_id FROM segment_inspections WHERE id=?", (action_id,)
            ).fetchone()
            action_id = None if row is None else row[0]
    if type(action_id) is not int:
        reference = getattr(request, "ref", None)
        action_id = getattr(reference, "action_id", None)
    if type(action_id) is not int:
        raise AssertionError(f"Action6 {stage} 同步点缺少动作身份")
    record = {
        "stage": stage,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "utc_ns": time.time_ns(),
        "key": str(key),
        "request": _json_value(request),
        "state": _snapshot(owned, action_id),
    }
    temporary = marker.with_suffix(".new")
    temporary.write_text(json.dumps(record, default=str), encoding="utf-8")
    temporary.replace(marker)
    while True:
        time.sleep(0.05)


def install() -> None:
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository

    stage = os.environ["CAMCTL_ACTION6_TEST_OBSERVER_STAGE"]
    if stage == "inspection_saved":
        original = SegmentInspectionRepository.save_result

        def observe(self, request, key, owned):
            outcome = original(self, request, key, owned)
            if outcome.kind is DbOutcomeKind.COMPLETED:
                _hold_after_commit(stage, owned, request, key)
            return outcome

        SegmentInspectionRepository.save_result = observe
    elif stage == "source_committed":
        original = CaptureRepository.finish_segment_recording

        def observe(self, request, key, owned, *, batch_size=128):
            outcome = original(self, request, key, owned, batch_size=batch_size)
            if outcome.kind is DbOutcomeKind.COMPLETED:
                _hold_after_commit(stage, owned, request, key)
            return outcome

        CaptureRepository.finish_segment_recording = observe
    elif stage == "local_read":
        from camctl.host_files.io import LocalSourceReader

        gate = Path(os.environ["CAMCTL_ACTION6_TEST_LOCAL_READ_GATE_DIR"])
        marker = gate / "local-read-entered.json"
        release = gate / "release-local-read"
        original = LocalSourceReader.read_chunk

        def observe(self, limit):
            result = original(self, limit)
            if result.data is not None and not marker.exists():
                record = {
                    "pid": os.getpid(),
                    "pgid": os.getpgrp(),
                    "position": self.position(),
                    "bytes_read": len(result.data),
                    "utc_ns": time.time_ns(),
                }
                temporary = marker.with_suffix(".new")
                temporary.write_text(json.dumps(record), encoding="utf-8")
                temporary.replace(marker)
                while not release.exists():
                    time.sleep(0.05)
            return result

        LocalSourceReader.read_chunk = observe
    else:
        raise ValueError(f"未登记的 Action6 同步点: {stage}")
