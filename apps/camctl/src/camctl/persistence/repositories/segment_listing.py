"""将有限成员批次存入临时派生索引，供纯规则重复读取。"""

from contextlib import asynccontextmanager

from camctl.capture.segment_models import (
    SegmentMember, SegmentMemberRole, OwnershipState, FileCompletionState, PreviewPairingState,
)
from camctl.capture.segment_storage import StoredSegmentMember, document, decode_enum, key
from camctl.contracts.json_values import parse_exact_json
from camctl.persistence.transaction import row_facts, encode_json_value
from camctl.persistence.segment_order import temporary_segment_index


def _member(value):
    value["key"] = key(value["key"])
    for field, kind in (
        ("role", SegmentMemberRole),
        ("ownership", OwnershipState),
        ("completion", FileCompletionState),
        ("pairing_state", PreviewPairingState),
    ):
        value[field] = decode_enum(kind, value[field])
    return SegmentMember(**value)


def _listing_context(owned, ticket):
    from .segment_result_sets import _boundary

    db = owned.connection
    boundary = _boundary(db)
    attempt = row_facts(
        db, "operation_attempts",
        db.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()[0],
    )
    return boundary, attempt


def _listing_page(owned, saved, attempt, final, boundary):
    from .segment_result_sets import _current_input
    from .segments import _input_member

    return tuple(
        (file_id, _input_member(owned.connection, _current_input(
            owned.connection, saved.ref, file_id, attempt, final, boundary,
        ))[0])
        for _, file_id in saved.file_ids
    )


@asynccontextmanager
async def ordered_listing_members(database, listing):
    """每次只在 worker 读取一页，排序与规则迭代留在调用协程。"""
    from camctl.operations.models import AttemptStatus

    if listing.entries == () and (listing.already_saved
            or listing.outcome is not None and listing.outcome.status is AttemptStatus.UNKNOWN):
        yield lambda: iter(())
        return
    boundary, attempt = await database.read(
        lambda owned: _listing_context(owned, listing.ticket))
    with temporary_segment_index() as ordered:
        ordered.execute(
            "CREATE TABLE members (directory INTEGER, position INTEGER, identity TEXT, file_id INTEGER PRIMARY KEY, value TEXT)"
        )
        async for saved in listing.entries.pages():
            page = await database.read(lambda owned: _listing_page(
                owned, saved, attempt, listing.entries.last_ref, boundary))
            for file_id, member in page:
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
                yield _member(parse_exact_json(raw))

        yield members


@asynccontextmanager
async def materialized_segment_members(database, repository, ref):
    """冻结成员逐页跨 worker；临时索引提供纯规则需要的可重复遍历。"""
    with temporary_segment_index() as ordered:
        ordered.execute("CREATE TABLE members (position INTEGER PRIMARY KEY, value TEXT)")
        cursor = None
        position = 0
        while True:
            page = await database.read(
                lambda owned: repository.read_members(ref, cursor, 128, owned))
            for stored in page.items:
                ordered.execute("INSERT INTO members VALUES (?,?)", (
                    position, encode_json_value(document(stored))))
                position += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        def members():
            for (raw,) in ordered.execute("SELECT value FROM members ORDER BY position"):
                value = parse_exact_json(raw)
                value["member"] = _member(value["member"])
                yield StoredSegmentMember(**value)

        yield members


@asynccontextmanager
async def materialized_current_members(database, setrow):
    """当前扫描按实际页资格读取，范围不与保留集合混合。"""
    from .segment_current import read_current_member_page

    with temporary_segment_index() as ordered:
        ordered.execute("CREATE TABLE members (position INTEGER PRIMARY KEY, value TEXT)")
        cursor = None
        position = 0
        while True:
            page = await database.read(lambda owned: read_current_member_page(
                owned.connection, setrow, cursor, 128))
            for member in page.items:
                ordered.execute("INSERT INTO members VALUES (?,?)", (
                    position, encode_json_value(document(member))))
                position += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        def members():
            for (raw,) in ordered.execute("SELECT value FROM members ORDER BY position"):
                yield _member(parse_exact_json(raw))

        yield members
