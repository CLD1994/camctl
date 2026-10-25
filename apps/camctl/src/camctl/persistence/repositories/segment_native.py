"""从原生结果的冻结成员独立恢复专属分类。"""

from types import SimpleNamespace
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.persistence.read_reuse import reuse_read, typed_read_key
from camctl.persistence.transaction import row_facts


def native_classification(db, setrow):
    """两遍读取同一冻结成员，空尾分类由可靠顺序和最后非零原片证明。"""
    key = (__name__, "native_classification", typed_read_key(tuple(
        setrow[field] for field in ("action_id", "id", "version", "run_id", "source_kind", "activity_id")
    )))
    return reuse_read(db, key, lambda: _load_native_classification(db, setrow))


def _load_native_classification(db, setrow):
    from .segments import read_segment_members
    from .segment_result_sets import _one
    from camctl.capture.native_segments import classify_native_members

    ref = SegmentResultRef(setrow["action_id"], setrow["id"], setrow["version"])

    def members():
        cursor = None
        while True:
            page = read_segment_members(
                ref, cursor, 128, SimpleNamespace(connection=db)
            )
            yield from (stored.member for stored in page.items)
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    latest = _one(
        db,
        "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (setrow["run_id"],),
    )
    attempt = None if latest is None else row_facts(db, "operation_attempts", latest[0])
    return classify_native_members(
        members,
        complete=setrow["source_kind"]
        == int(enum_for("segment_sets.source_kind").COMPLETE_SCAN),
        activity_id=setrow["activity_id"],
        explicit_error=attempt is not None
        and attempt["status"] == int(enum_for("operation_attempts.status").FAILED)
        and attempt["error_json"] is not None,
    )
