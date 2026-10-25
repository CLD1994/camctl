"""成员分页越过 worker 时只携带有限值，临时索引归调用协程。"""
import asyncio
from contextlib import contextmanager
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import ListingPhase, ListingRound
from camctl.capture.result_scans import SavedResultEntries
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.capture.segment_models import (
    SegmentKey, SegmentMember, SegmentMemberRole, OwnershipState, FileCompletionState,
)
from camctl.capture.segment_storage import StoredSegmentMember
from camctl.contracts.pages import Page
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories import segment_listing as listing
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

pytestmark = pytest.mark.asyncio


def member(index):
    return SegmentMember(
        f'video-{index}', SegmentKey(1, index), SegmentMemberRole.ORIGINAL,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, index,
    )


def database():
    port = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a"*32, 5, "/staging", "/ready", "/processing"))
    async def read(callback, **kwargs):
        return callback(owned)
    port.read.side_effect = read
    return port, owned


def index_double(monkeypatch):
    """受连接接口约束的内存值账本，不打开真实 SQLite。"""
    index = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    rows = []
    def execute(sql, parameters=()):
        if sql.startswith('INSERT'):
            rows.append(tuple(parameters))
            return create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if sql.startswith('SELECT'):
            cursor.__iter__.side_effect = lambda: iter((row[-1],) for row in sorted(rows))
        return cursor
    index.execute.side_effect = execute
    closed = []
    @contextmanager
    def temporary():
        try:
            yield index
        finally:
            closed.append(True)
    monkeypatch.setattr(listing, 'temporary_segment_index', create_autospec(
        listing.temporary_segment_index, side_effect=temporary))
    return index, rows, closed


async def test_frozen_members_cross_128_as_values_and_replay_without_database(monkeypatch):
    port, owned = database()
    index, rows, closed = index_double(monkeypatch)
    repository = create_autospec(SegmentRepository, instance=True, spec_set=True)
    ref = object()
    expected = tuple(StoredSegmentMember(i, i+1000, 5, 4, member(i)) for i in range(1, 130))
    repository.read_members.side_effect = [Page(expected[:128], 'next'), Page(expected[128:], None)]

    async with listing.materialized_segment_members(port, repository, ref) as members:
        assert tuple(members()) == expected
        assert tuple(members()) == expected
        assert len(rows) == 129
        assert not closed
    assert closed == [True]
    assert [call.args for call in repository.read_members.call_args_list] == [
        (ref, None, 128, owned), (ref, 'next', 128, owned),
    ]
    assert port.read.await_count == 2


async def test_member_read_cancellation_releases_temporary_index(monkeypatch):
    port, _ = database()
    _, _, closed = index_double(monkeypatch)
    repository = create_autospec(SegmentRepository, instance=True, spec_set=True)
    port.read.side_effect = asyncio.CancelledError('source canceled')
    with pytest.raises(asyncio.CancelledError, match='source canceled'):
        async with listing.materialized_segment_members(port, repository, object()):
            pytest.fail('未读完的临时索引不能发布')
    assert closed == [True]


async def test_current_page_with_no_eligible_members_still_advances_cursor(monkeypatch):
    from camctl.persistence.repositories import segment_current
    port, owned = database()
    _, _, closed = index_double(monkeypatch)
    read = create_autospec(segment_current.read_current_member_page,
        side_effect=[Page((), 'next'), Page((member(129),), None)])
    monkeypatch.setattr(segment_current, 'read_current_member_page', read)
    row = object()
    async with listing.materialized_current_members(port, row) as members:
        assert tuple(members()) == (member(129),)
    assert [call.args for call in read.call_args_list] == [
        (owned.connection, row, None, 128), (owned.connection, row, 'next', 128),
    ]
    assert closed == [True]


async def test_listing_materializes_one_saved_page_at_a_time(monkeypatch):
    port, owned = database()
    _, rows, closed = index_double(monkeypatch)
    pages = (object(), object())
    async def saved_pages():
        yield pages[0]
        assert len(rows) == 128
        yield pages[1]
    # pages() is the saved-page async stream contract; no Owned travels in it.
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    entries = SavedResultEntries(repository, port, object())
    entries.pages = create_autospec(entries.pages, side_effect=saved_pages)
    source = ListingRound(ListingPhase.LISTED, entries=entries, ticket=object())
    context = create_autospec(listing._listing_context, return_value=('boundary', 'attempt'))
    page = create_autospec(listing._listing_page, side_effect=[
        tuple((i, member(i)) for i in range(1, 129)), ((129, member(129)),),
    ])
    monkeypatch.setattr(listing, '_listing_context', context)
    monkeypatch.setattr(listing, '_listing_page', page)
    async with listing.ordered_listing_members(port, source) as members:
        assert len(tuple(members())) == 129
    assert context.call_args.args == (owned, source.ticket)
    assert [call.args for call in page.call_args_list] == [
        (owned, saved, 'attempt', entries.last_ref, 'boundary') for saved in pages
    ]
    assert closed == [True]


async def test_atomic_conclusion_classification_keeps_synchronous_member_source(monkeypatch):
    from camctl.capture import action6_completion
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories import segment_current, segments
    _, owned = database()
    expected = tuple(StoredSegmentMember(i, i+1000, 5, 4, member(i)) for i in range(1, 130))
    read = create_autospec(segments.read_segment_members,
        side_effect=[Page(expected[:128], 'next'), Page(expected[128:], None)])
    monkeypatch.setattr(segments, 'read_segment_members', read)
    action, ref, result = object(), object(), object()
    def classify(actual_action, actual_ref, members, **kwargs):
        assert actual_action is action and actual_ref is ref
        assert tuple(members) == tuple(stored.member for stored in expected)
        assert kwargs == {'complete': True}
        return result
    monkeypatch.setattr(action6_completion, 'classify_action6_members', create_autospec(
        action6_completion.classify_action6_members, side_effect=classify))
    row = {'source_kind': int(enum_for('segment_sets.source_kind').COMPLETE_SCAN),
           'publication_kind': int(enum_for('segment_sets.publication_kind').RESULT)}
    assert segment_current.classify_action6_current_set(owned.connection, action, ref, row) is result
    assert [(call.args[1], call.args[2]) for call in read.call_args_list] == [(None,128),('next',128)]
