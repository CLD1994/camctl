"""Read-only state query for the fixture83 bootstrap timeout evidence."""

import json
import sqlite3
from pathlib import Path

from camctl.bootstrap.application import query_work_facts
from camctl.session.work import classify_work


DATABASE = Path(
    "/tmp/camctl-quality-evidence/history/fixture83-final-bootstrap-data/"
    "test_submit_during_initial_dis0/state.db"
)
OUTPUT = Path(
    "/tmp/camctl-quality-evidence/history/fixture83-final-bootstrap-state.json"
)


def main() -> None:
    connection = sqlite3.connect(
        f"file:{DATABASE}?mode=ro", uri=True, timeout=1
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")

    def rows(sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in connection.execute(sql, args).fetchall()]

    status_counts = {}
    for table in (
        "plans", "actions", "outputs", "obtain_items", "deliveries",
        "operation_runs", "reports", "state_syncs",
    ):
        try:
            status_counts[table] = rows(
                f"SELECT status,COUNT(*) AS count FROM {table} "
                "GROUP BY status ORDER BY status"
            )
        except sqlite3.OperationalError:
            status_counts[table] = rows(f"SELECT COUNT(*) AS count FROM {table}")

    facts = query_work_facts(connection)
    decision = classify_work(facts)
    payload = {
        "database": str(DATABASE),
        "read_only": True,
        "integrity_check": rows("PRAGMA quick_check"),
        "status_counts": status_counts,
        "plans": rows(
            "SELECT id,request_id,status FROM plans ORDER BY id LIMIT 20"
        ),
        "actions": rows(
            "SELECT id,plan_id,input_index,type,status,device_id FROM actions "
            "ORDER BY id LIMIT 40"
        ),
        "outputs": rows("SELECT COUNT(*) AS count FROM outputs"),
        "obtain_items": rows(
            "SELECT id,delivery_id,output_id,status FROM obtain_items "
            "ORDER BY id LIMIT 40"
        ),
        "deliveries": rows(
            "SELECT id,action_id,status FROM deliveries ORDER BY id LIMIT 40"
        ),
        "operation_runs": rows(
            "SELECT id,action_id,copy_id,cleanup_item_id,status "
            "FROM operation_runs ORDER BY id LIMIT 40"
        ),
        "reports": rows(
            "SELECT id,status,from_wm,to_wm FROM reports ORDER BY id LIMIT 20"
        ),
        "report_entity_changes": rows(
            "SELECT COUNT(*) AS count,COALESCE(MAX(change_seq),0) AS max_change_seq "
            "FROM report_entity_changes LIMIT 1"
        )[0],
        "runtime_state": rows(
            "SELECT acknowledged_wm FROM runtime_state WHERE id=1 LIMIT 1"
        )[0],
        "production_work_facts": {
            name: getattr(facts, name) for name in facts.__dataclass_fields__
        },
        "production_work_decision": decision.kind.value,
    }
    connection.close()
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
