"""将可靠原页的单文件事实存入临时派生索引，按设备键有界续读。"""

from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3

from camctl.capture.segment_models import (
    SegmentMember, SegmentMemberRole, OwnershipState, FileCompletionState, PreviewPairingState,
)
from camctl.capture.segment_storage import document, decode_enum, key
from camctl.contracts.json_values import parse_exact_json
from camctl.persistence.transaction import row_facts, encode_json_value


@contextmanager
def ordered_listing_members(owned, listing):
    from camctl.persistence.repositories.segment_result_sets import (
        _boundary,
        _current_input,
    )
    from camctl.persistence.repositories.segments import _input_member

    db = owned.connection
    boundary = _boundary(db)
    attempt = row_facts(
        db,
        "operation_attempts",
        db.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (listing.ticket.run_id, listing.ticket.attempt_id),
        ).fetchone()[0],
    )
    with (
        TemporaryDirectory(prefix="camctl-segment-order-") as folder,
        closing(sqlite3.connect(str(Path(folder) / "order.db"))) as ordered,
    ):
        ordered.execute(
            "CREATE TABLE members (directory INTEGER, position INTEGER, identity TEXT, file_id INTEGER PRIMARY KEY, value TEXT)"
        )
        for saved in listing.entries.pages():
            for _, file_id in saved.file_ids:
                item = _current_input(
                    db, saved.ref, file_id, attempt, listing.entries.last_ref, boundary
                )
                member, _ = _input_member(db, item)
                ordered.execute(
                    "INSERT INTO members VALUES (?,?,?,?,?)",
                    (
                        0 if member.key is None else member.key.directory_number,
                        0 if member.key is None else member.key.index,
                        member.identity,
                        file_id,
                        encode_json_value(document(member)),
                    ),
                )

        def members():
            for (raw,) in ordered.execute(
                "SELECT value FROM members ORDER BY directory,position,identity,file_id"
            ):
                value = parse_exact_json(raw)
                value["key"] = key(value["key"])
                for field, kind in (
                    ("role", SegmentMemberRole),
                    ("ownership", OwnershipState),
                    ("completion", FileCompletionState),
                    ("pairing_state", PreviewPairingState),
                ):
                    value[field] = decode_enum(kind, value[field])
                yield SegmentMember(**value)

        yield members
