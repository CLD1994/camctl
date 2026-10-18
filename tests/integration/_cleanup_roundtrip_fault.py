"""R6 两个持久层窗口：真实保存后丢回执，以及成员终态后伴随收场前。"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def _rows(database, sql, parameters=()):
    with sqlite3.connect("file:" + database + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def main():
    from camctl.cli import main as cli
    from camctl.contracts.enums import enum_for
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories.outputs import OutputsRepository

    manifest = json.loads(Path(os.environ["CAMCTL_CLEANUP_MANIFEST"]).read_text())
    database = manifest["database"]
    phase = os.environ["CAMCTL_CLEANUP_PERSISTENCE_FAULT"]
    marker = Path(manifest["marker"])
    trace = marker.with_name("fault-trace.jsonl")
    fired = None

    def record(stage, **data):
        stat = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(") ", 1)[1].split()
        result = dict(stage=stage, pid=os.getpid(), ppid=os.getppid(), pgid=os.getpgrp(),
                      starttime=int(stat[19]), utc_ns=time.time_ns(), **data)
        with trace.open("a") as stream:
            stream.write(json.dumps(result, default=str) + "\n")
        return result

    def hit(stage, **data):
        value = record(stage, **data)
        temporary = marker.with_suffix(".new")
        temporary.write_text(json.dumps(value, default=str))
        temporary.replace(marker)
        while True:
            time.sleep(0.05)

    def member(owned):
        actual = owned.connection.execute("PRAGMA database_list").fetchone()[2]
        if Path(actual).resolve() != Path(database).resolve():
            return None
        candidates = _rows(database,
            "SELECT c.* FROM cleanup_items c JOIN actions a ON a.id=c.action_id "
            "JOIN plans p ON p.id=a.plan_id WHERE p.request_id=? AND c.output_id=?",
            (manifest["request_id"], manifest["fault_output_id"]))
        return candidates[0] if candidates else None

    original_finish = OutputsRepository.finish_cleanup_attempt
    original_stale = OperationRepository.finish_stale_runs

    def finish(self, request, key, owned):
        nonlocal fired
        target = member(owned)
        ticket = request.ticket
        matches = (phase == "commit_unknown" and fired is None and target is not None
                   and ticket.operation == "delete" and ticket.target_id == str(target["id"])
                   and ticket.responsibility_key == f"delete/{target['id']}")
        result = original_finish(self, request, key, owned)
        if matches and result.kind is DbOutcomeKind.COMPLETED:
            attempt, = _rows(database,
                "SELECT t.*,h.operation_key FROM operation_attempts t "
                "JOIN operation_runs r ON r.id=t.run_id "
                "JOIN history_events e ON e.id=t.result_event_id "
                "JOIN history_transactions h ON h.id=e.transaction_id "
                "WHERE r.cleanup_item_id=? AND r.id=? AND t.attempt_no=?",
                (target["id"], ticket.run_id, ticket.attempt_id))
            assert attempt["operation_key"] == str(key)
            assert attempt["result_json"] is not None
            assert attempt["status"] != int(enum_for("operation_attempts.status").RUNNING)
            fired = dict(item_id=target["id"], ticket=asdict(ticket), key=str(key), attempt=attempt)
            record("committed_before_unknown", **fired)
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("R6 保存提交回执丢失"))
        return result

    def stale(self, request, key, owned):
        target = member(owned)
        if (phase == "terminal_companions" and target is not None
                and f"delete/{target['id']}" in request.responsibility_keys):
            assert target["status"] in {int(enum_for("cleanup_items.status")[name])
                                       for name in ("SUCCEEDED", "FAILED", "CANCELED")}
            assert target["final_event_id"] is not None
            attempts = _rows(database,
                "SELECT t.* FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id "
                "WHERE r.cleanup_item_id=?", (target["id"],))
            assert attempts and all(t["status"] != int(enum_for("operation_attempts.status").RUNNING)
                                    for t in attempts)
            runs = _rows(database, "SELECT * FROM operation_runs WHERE cleanup_item_id=?", (target["id"],))
            assert any(r["status"] in {int(enum_for("operation_runs.status").PENDING),
                                       int(enum_for("operation_runs.status").ACTIVE)} for r in runs)
            hit("terminal_before_companions", item=target, attempts=attempts,
                runs=runs, keys=request.responsibility_keys, key=str(key))
        return original_stale(self, request, key, owned)

    OutputsRepository.finish_cleanup_attempt = finish
    OperationRepository.finish_stale_runs = stale
    exitcode = None
    error = None
    try:
        exitcode = cli()
        return exitcode
    except BaseException as failure:
        error = repr(failure)
        raise
    finally:
        record("cli_finally", exitcode=exitcode, error=error, fault_hit=fired is not None)
        if fired is not None:
            target, = _rows(database, "SELECT * FROM cleanup_items WHERE id=?", (fired["item_id"],))
            assert target["status"] not in {int(enum_for("cleanup_items.status")[name])
                                           for name in ("SUCCEEDED", "FAILED", "CANCELED")}
            attempt, = _rows(database, "SELECT * FROM operation_attempts WHERE id=?", (fired["attempt"]["id"],))
            assert attempt["result_event_id"] == fired["attempt"]["result_event_id"]
            assert attempt["result_json"] == fired["attempt"]["result_json"]
            sys.stdout.flush()
            sys.stderr.flush()
            hit("cli_exited_after_unknown", exitcode=exitcode, error=error, item=target, **fired)


if __name__ == "__main__":
    raise SystemExit(main())
