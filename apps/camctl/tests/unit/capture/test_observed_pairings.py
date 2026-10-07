"""结果列举配对校验的单元测试。

预览条目按驱动声明的配对关联归属预览角色；配对目标缺失、自指或
指向另一个预览时无法证明配对可靠，整批拒绝，不猜测关联。
"""

from __future__ import annotations

import pytest

from camctl.capture.handlers import (
    ObservedFile,
    validate_observed_pairings,
)
from camctl.capture.results import FileKind
from camctl.contracts.values import ConsistencyError


def _entry(identity: str, *, paired: str | None = None) -> ObservedFile:
    return ObservedFile(
        identity=identity,
        locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": identity},
        complete=True,
        size_bytes=4096,
        kind=FileKind.PHOTO,
        original_name=f"{identity}.jpg",
        media_type="image/jpeg",
        paired_identity=paired,
    )


def test_unpaired_entries_pass() -> None:
    validate_observed_pairings((_entry("shot-1"), _entry("shot-2")))
    validate_observed_pairings(())


def test_preview_paired_to_original_passes() -> None:
    validate_observed_pairings((
        _entry("shot-1"), _entry("shot-1-preview", paired="shot-1")))


@pytest.mark.parametrize(
    "entries",
    [
        # 配对目标不在同批条目中。
        (_entry("shot-1-preview", paired="shot-1"),),
        # 预览配对指向自身。
        (_entry("shot-1", paired="shot-1"),),
        # 配对目标本身是预览，不是原片。
        (
            _entry("shot-1"),
            _entry("shot-1-preview", paired="shot-1"),
            _entry("shot-1-tiny", paired="shot-1-preview"),
        ),
    ],
    ids=["missing-target", "self-paired", "paired-to-preview"],
)
def test_invalid_pairing_rejects_batch(entries) -> None:
    with pytest.raises(ConsistencyError, match="预览配对"):
        validate_observed_pairings(entries)
