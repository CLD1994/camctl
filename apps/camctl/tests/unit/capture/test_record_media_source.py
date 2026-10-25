"""异常录像只能选择唯一完整原片，预览和多个原片不成为修复输入。"""
import pytest
from camctl.capture.result_inputs import ObservedFile
from camctl.capture.results import CaptureFile, FileKind
from camctl.contracts.values import ConsistencyError


def _entry(name, fmt='osv', paired=None, complete=True):
    return ObservedFile(name, {'path': name}, {}, complete, 10,
        FileKind.VIDEO, paired_identity=paired, format_id=fmt)


def _registered(entries):
    return ((CaptureFile(entry.identity, FileKind.VIDEO, entry.complete, True), index)
        for index, entry in enumerate(entries, 1))


def test_lrf_before_original_selects_original_and_retains_its_format():
    from camctl.capture.handlers import _record_media_source
    entries = (_entry('preview', 'lrf', 'original'), _entry('original'))
    assert _record_media_source(_registered(entries), iter(entries)) == (2, 'osv')


@pytest.mark.parametrize('entries', [(_entry('a'), _entry('b')),
    (_entry('lrf', 'lrf'),), (_entry('a', complete=False),)])
def test_ambiguous_or_unusable_original_has_no_media_input(entries):
    from camctl.capture.handlers import _record_media_source
    assert _record_media_source(_registered(entries), iter(entries)) is None


def test_recovered_media_input_cannot_change_original_identity():
    from camctl.capture.handlers import _record_media_source
    entries = (_entry('a'),)
    with pytest.raises(ConsistencyError):
        _record_media_source(_registered(entries), iter(entries), source_file_id=2)


def test_recovered_media_input_retains_original_format():
    from camctl.capture.handlers import _record_media_source
    entries = (_entry('a'),)
    assert _record_media_source(_registered(entries), iter(entries), source_file_id=1) == (1, 'osv')


@pytest.mark.asyncio
async def test_minimum_size_controls_publication_without_discarding_other_complete_originals():
    from camctl.capture.handlers import _catalog_drafts
    from camctl.capture.results import ProductRule
    from dataclasses import replace
    original = _entry('a')
    entries = (replace(original, size_bytes=0), replace(original, identity='b', size_bytes=2))
    rule = ProductRule(FileKind.VIDEO, format_id='osv', exact_count=1, min_size_bytes=1)
    drafts = (await _catalog_drafts(tuple(_registered(entries)), entries, product_rules=(rule,)))
    assert [draft.file.device_file_id for draft in drafts] == [2]


@pytest.mark.asyncio
async def test_preview_of_zero_size_original_is_not_published():
    from camctl.capture.handlers import _catalog_drafts
    from camctl.capture.results import ProductRule
    from dataclasses import replace
    entries = (replace(_entry('a'), size_bytes=0), _entry('preview', 'lrf', 'a'))
    rule = ProductRule(FileKind.VIDEO, format_id='osv', min_size_bytes=1)
    assert (await _catalog_drafts(tuple(_registered(entries)), entries, product_rules=(rule,))) == ()


def test_empty_original_is_not_used_as_repair_input():
    from dataclasses import replace
    from camctl.capture.handlers import _record_media_source
    entries = (replace(_entry('a'), size_bytes=0),)
    assert _record_media_source(_registered(entries), iter(entries)) is None
