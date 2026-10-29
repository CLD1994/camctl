"""容量用例的独立业务进程；不导入 pytest 或拍摄准备夹具。"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
import sys
import time


def decode_request(raw):
    from camctl.capture.media import RecordingFailure
    from camctl.capture.segment_finish import FinishSegmentRecording, result_ref_from_document

    if not isinstance(raw, dict) or set(raw) != {
        "ref", "activity_id", "run_id", "occurred_at", "failure", "failure_event_id"
    }:
        raise ValueError("容量 worker 必须取得完整原完成申请")
    failure = raw["failure"]
    return FinishSegmentRecording(
        result_ref_from_document(raw["ref"]), raw["activity_id"], raw["run_id"],
        raw["occurred_at"], None if failure is None else RecordingFailure(**failure),
        raw["failure_event_id"],
    )


def result_document(result):
    return asdict(result)


def _receipt(receipt):
    return {
        "kind": receipt.kind,
        "result": None if receipt.result is None else result_document(receipt.result),
        "boundary": None if receipt.boundary is None else asdict(receipt.boundary),
    }


def _history_position(connection):
    return connection.execute(
        "SELECT COUNT(*),COALESCE(MAX(id),0) FROM history_events"
    ).fetchone()


def execute(manifest):
    """执行已准备数据库的原收场及核实，完整结果留给组外断言。"""
    import camctl
    from camctl.capture.files import FileChecksumSave
    from camctl.contracts.values import OperationKey
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.segment_transactions import (
        finish_segment_recording, verify_segment_finish,
    )
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    if not isinstance(manifest, dict) or set(manifest) != {
        "version", "database", "request", "key", "batch_size", "observations"
    } or manifest["version"] != 1:
        raise ValueError("容量 worker 输入版本或成员不同")
    database = Path(manifest["database"]).resolve(strict=True)
    request = decode_request(manifest["request"])
    key = OperationKey(manifest["key"])
    batch = manifest["batch_size"]
    owned = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        actual_database = str(Path(owned.connection.execute("PRAGMA database_list").fetchone()[2]).resolve())
        before = _history_position(owned.connection)
        started = time.perf_counter()
        saved = finish_segment_recording(request, key, owned, batch_size=batch)
        timing = {"finish": time.perf_counter() - started}
        if saved.kind != "completed":
            raise AssertionError(f"原收场没有完成: {saved.kind}, {saved.error}")
        started = time.perf_counter()
        verified = verify_segment_finish(request, key, owned, execution_finished=True, batch_size=batch)
        timing["verify"] = time.perf_counter() - started
        after_write = _history_position(owned.connection)
        started = time.perf_counter()
        repeated = finish_segment_recording(request, key, owned, batch_size=batch)
        timing["retry"] = time.perf_counter() - started
        after_repeated = _history_position(owned.connection)
        timing.update(later_observations=0.0, later_verify=0.0)
        rounds = []
        repository = CaptureRepository()
        for observation in manifest["observations"]:
            prior = _history_position(owned.connection)
            started = time.perf_counter()
            observed = repository.save_file_checksum(
                FileChecksumSave(observation["file_id"], 2, observation["occurred_at"],
                                 sha256=observation["sha256"]),
                OperationKey(observation["key"]), owned,
            )
            timing["later_observations"] += time.perf_counter() - started
            if observed.kind is not DbOutcomeKind.COMPLETED:
                raise AssertionError(f"后续原片观察未保存: {observed.kind}, {observed.error}")
            current = _history_position(owned.connection)
            started = time.perf_counter()
            original = verify_segment_finish(request, key, owned, execution_finished=True, batch_size=batch)
            timing["later_verify"] += time.perf_counter() - started
            rounds.append({
                "observation": observation, "before": prior, "after": current,
                "after_verify": _history_position(owned.connection),
                "verified": None if original is None else result_document(original),
            })
        return {
            "version": 1, "database": actual_database, "key": str(key),
            "timing_seconds": timing,
            "runtime": {"pid": os.getpid(), "executable": sys.executable,
                        "python": sys.version, "sqlite": sqlite3.sqlite_version,
                        "camctl_file": str(Path(camctl.__file__).resolve())},
            "request": manifest["request"], "write": _receipt(saved),
            "verified": None if verified is None else result_document(verified),
            "repeated": _receipt(repeated), "history_before": before,
            "history_after_write": after_write, "history_after_repeated": after_repeated,
            "rounds": rounds,
        }
    finally:
        owned.connection.close()


def main():
    input_path, result_path = map(Path, sys.argv[1:])
    manifest = json.loads(input_path.read_text())
    result = execute(manifest)
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(result_path)


if __name__ == "__main__":
    main()
