"""预览选择消费来源最后一次结果尝试，不把未登记解释为缺席。"""

from types import SimpleNamespace
from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.operations.models import AttemptTicket
from camctl.outputs.preview_facts import unconfirmed_previews


class PreviewReads:
    """在调用方的同一读取边界分批消费已经保存的结果页。"""

    def __init__(self, connection):
        self.connection = connection

    def require_writable(self):
        from camctl.persistence.runtime import require_preview_selection_format

        with closing(self.connection.execute("SELECT format_version FROM database_metadata WHERE id=1")) as cursor:
            row = cursor.fetchone()
        require_preview_selection_format(None if row is None else row[0])

    def unconfirmed(self, source, originals, *, before_event_id=None):
        if not originals or (source["execution_spec_json"] or {}).get("video_result_mode") is None:
            return frozenset()
        boundary = source["last_event_id"] + 1 if before_event_id is None else before_event_id
        with closing(self.connection.execute(
            "SELECT r.id,r.activity_id,a.attempt_no,a.result_first_page_event_id,a.result_last_page_event_id"
            " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
            " WHERE r.action_id=? AND r.kind=? AND r.responsibility_key='results/' || r.activity_id"
            " AND a.intent_event_id<? ORDER BY r.id DESC,a.attempt_no DESC LIMIT 1",
            (source["id"], int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS), boundary),
        )) as cursor:
            row = cursor.fetchone()
        if row is None or row[3] is None or row[4] is None or row[4] >= boundary:
            return frozenset(originals)
        run_id, activity_id, attempt_no, _, _ = row
        ticket = AttemptTicket(attempt_no, "result", str(activity_id), f"results/{activity_id}", run_id)

        def pages():
            from .result_pages import read_pages

            cursor = None
            while True:
                batch = read_pages(ticket, cursor, 128, SimpleNamespace(connection=self.connection))
                for saved in batch.items:
                    yield saved.page
                cursor = batch.next_cursor
                if cursor is None:
                    return

        return unconfirmed_previews(originals, pages())
