"""发现页的责任与继续位置不依赖候选详情缓存。"""

from dataclasses import replace

import pytest

from camctl.contracts.pages import Page
from camctl.outputs.read_discovery import (
    ReadDiscoveryCursor, ReadDiscoveryOwner, ReadDiscoveryScope, check_cursor,
)


def test_empty_page_with_cursor_continues():
    owner = ReadDiscoveryOwner(1)
    scope = owner.scope("reads", "db", 123)
    owner.accept(scope, Page((), ReadDiscoveryCursor(scope, (4,))))
    assert owner.pending
    assert owner.scope("reads", "db", 999) is scope
    owner.accept(scope, Page((), None))
    assert not owner.pending


@pytest.mark.parametrize("field,value", [
    ("database", "other"), ("kind", "local"), ("now_us", 124),
    ("generation", 1), ("device_id", "camera"),
])
def test_cursor_is_bound_to_scope_and_generation(field, value):
    scope = ReadDiscoveryScope("db", "reads", 123, 0)
    with pytest.raises(ValueError):
        check_cursor(replace(scope, **{field: value}), ReadDiscoveryCursor(scope, (1,)))


def test_eviction_keeps_discovery_responsibility():
    owner = ReadDiscoveryOwner(2)
    scope = owner.scope("local", "db", 123)
    page = Page(({"id": 1},), ReadDiscoveryCursor(scope, (2,)))
    owner.accept(scope, page)
    del page
    assert owner.pending and owner.cursor_count == 1
    assert not any(isinstance(value, dict) for value in owner._positions.values())


def test_new_eligibility_invalidates_old_frontier():
    owner = ReadDiscoveryOwner(1)
    owner.observe(("db", 5), 123)
    scope = owner.scope("reads", "db", 123)
    owner.accept(scope, Page((), ReadDiscoveryCursor(scope, (100,))))
    owner.observe(("db", 6), 124)
    assert not owner.pending
    with pytest.raises(ValueError):
        owner.accept(scope, Page((), None))
