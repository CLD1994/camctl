"""检查流程终结前核对原尝试是否已经交付。"""

from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.history.events import load_event_registry


class AvailabilityFinishReads:
    """同一事务连接的当前事实及已验证前序，不采信未来提案。"""

    def __init__(self, connection, validated=()):
        self.connection = connection
        self.validated = tuple(validated)

    def require_settled(self, run_id):
        running = int(enum_for("operation_attempts.status").RUNNING)
        after = 0
        while True:
            with closing(self.connection.execute(
                "SELECT id FROM operation_attempts WHERE run_id=? AND id>?"
                " AND (status=? OR result_event_id IS NULL OR result_json IS NULL) ORDER BY id LIMIT 128",
                (run_id, after, running),
            )) as cursor:
                page = tuple(cursor.fetchmany(128))
            if not page:
                return
            for (attempt_id,) in page:
                settled = False
                for accepted in self.validated:
                    event = accepted.envelope
                    if event.event_type != load_event_registry()["events"]["ATTEMPT_RESULT"]["id"]:
                        continue
                    for row in event.rows:
                        if (row.table == "operation_attempts" and row.row_id == attempt_id
                                and row.before.exists and row.after.exists
                                and row.after.values.get("status") in (
                                    int(enum_for("operation_attempts.status").SUCCEEDED),
                                    int(enum_for("operation_attempts.status").FAILED),
                                    int(enum_for("operation_attempts.status").UNKNOWN))
                                and row.after.values.get("result_event_id") == event.event_id
                                and row.after.values.get("result_json") is not None):
                            settled = True
                if not settled:
                    raise ConsistencyError("检查尝试或原结果仍未收场，不能终结流程")
            after = page[-1][0]
            if len(page) < 128:
                return
