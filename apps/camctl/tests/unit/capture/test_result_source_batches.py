"""来源文件的当前资格由仓储交付的同批观察决定。"""

import pytest

from camctl.capture.result_inputs import ObservedFile
from camctl.capture.result_scans import QualifiedResultEntries, SavedResultEntries, SourceResultEntries
from camctl.capture.results import CaptureFile, FileKind
from camctl.contracts.pages import Page


class _Database:
    async def read(self, callback, **_kwargs):
        return callback(object())


class _Repository:
    def __init__(self, page, current):
        self.page, self.current = page, current

    def read_result_source_batch(self, ticket, cursor, batch, current_ref, owned):
        return self.page, self.current


@pytest.mark.asyncio
@pytest.mark.parametrize("original_time,expected", [(10, True), (9, False), (None, False)])
async def test_current_preview_requires_qualified_original_from_same_batch(original_time, expected):
    preview = ObservedFile("preview", {"path": "preview"}, {}, True, 7,
        FileKind.PHOTO, paired_identity="original")
    original = ObservedFile("original", {"path": "original"}, {}, True, 10, FileKind.VIDEO)
    file = CaptureFile("preview", FileKind.PHOTO, True, True, size_bytes=7)
    current = {"preview": (preview, 1, 10)}
    if original_time is not None:
        current["original"] = (original, 2, original_time)
    repository = _Repository(Page(((preview, file, 1),), None), current)
    database = _Database()
    entries = SavedResultEntries(repository, database, object())
    qualified = QualifiedResultEntries(entries, 10)

    actual = [item async for item in SourceResultEntries(
        repository, database, object(), current_entries=qualified).records()]

    assert bool(actual) is expected
    if expected:
        assert actual[0][0] == preview
        assert actual[0][1].pairing_confirmed is True
        assert actual[0][2] == 1
