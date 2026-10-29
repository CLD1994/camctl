"""有界候选缓存的纯内存容量与淘汰行为。"""

import pytest

from camctl.scheduling.discovery import BoundedCandidateCache


def test_cache_is_bounded_and_keeps_latest() -> None:
    """缓存淘汰最旧候选，保留近期批次；容量有界。"""
    cache = BoundedCandidateCache(capacity=3)
    cache.extend([{"id": 1}, {"id": 2}])
    cache.extend([{"id": 3}, {"id": 4}])
    assert [item["id"] for item in cache.snapshot()] == [2, 3, 4]
    cache.extend([{"id": 5}, {"id": 6}])
    assert [item["id"] for item in cache.snapshot()] == [4, 5, 6]
    with pytest.raises(ValueError):
        BoundedCandidateCache(capacity=0)
