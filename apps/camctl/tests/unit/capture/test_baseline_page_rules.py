"""原目录调用范围内的基准页只沿原游标推进。"""

import pytest

from camctl.capture.baseline_page_rules import validate_baseline_page_progress
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.file_identity import FileIdentity


_BINDING = DeviceBinding('camera', 'driver')
_ROOTS = ('/DCIM', '/OTHER')


def item(path):
    return FileIdentity(_BINDING, path)


def cursor(index, after=None, *, binding=_BINDING, directories=_ROOTS):
    return DirectoryCursor(binding, directories, index, after)


def check(page, previous=None):
    validate_baseline_page_progress(binding=_BINDING, directories=_ROOTS,
                                    cursor=previous, page=page)


def test_page_can_advance_within_directory_or_to_the_next_directory():
    check(Page((item('/DCIM/a'), item('/DCIM/b')), cursor(0, '/DCIM/b')))
    check(Page((), cursor(1)))
    check(Page((item('/OTHER/a'),), None), previous=cursor(1))


@pytest.mark.parametrize('page,previous', [
    (Page((item('/OTHER/a'),), cursor(1)), None),
    (Page((item('/DCIM/b'), item('/DCIM/a')), cursor(1)), None),
    (Page((item('/DCIM/a'),), cursor(1)), cursor(0, '/DCIM/a')),
])
def test_page_rejects_wrong_directory_repeated_or_reversed_items(page, previous):
    with pytest.raises(ConsistencyError, match='有序推进'):
        check(page, previous)


def test_page_rejects_changed_binding_or_directory_range():
    other = DeviceBinding('other', 'driver')
    with pytest.raises(ConsistencyError, match='改变原绑定或完整目录'):
        check(Page((), cursor(1, binding=other)))


def test_page_rejects_cursor_that_does_not_cover_last_item():
    with pytest.raises(ConsistencyError, match='不是本目录进度或下一目录'):
        check(Page((item('/DCIM/a'), item('/DCIM/b')), cursor(0, '/DCIM/a')))


def test_page_cannot_finish_before_last_directory():
    with pytest.raises(ConsistencyError, match='尚有目录'):
        check(Page((item('/DCIM/a'),), None))
