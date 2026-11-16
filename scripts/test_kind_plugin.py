"""统一 pytest 测试类别选择。"""
import pytest


TEST_KINDS = ('functional', 'performance', 'all')
DEFAULT_TEST_KIND = 'functional'


def item_test_kind(item):
    return 'performance' if item.get_closest_marker('performance') else 'functional'


def pytest_addoption(parser):
    parser.addoption('--test-kind', choices=TEST_KINDS, default=DEFAULT_TEST_KIND,
                     help='选择功能测试、性能专项或全部测试')


def pytest_configure(config):
    config.addinivalue_line('markers', 'performance: 性能专项测试，须显式选择执行')


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    kind = config.getoption('--test-kind')
    if kind == 'all':
        return
    selected, deselected = [], []
    for item in items:
        (selected if item_test_kind(item) == kind else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
