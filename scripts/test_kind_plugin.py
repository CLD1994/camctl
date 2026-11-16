"""统一 pytest 测试类别选择。"""
import pytest


TEST_KINDS = ('functional', 'performance', 'acceptance', 'all')
DEFAULT_TEST_KIND = 'functional'


def classify_test_item(item):
    for marker in item.iter_markers():
        if marker.name in TEST_KINDS[1:-1]:
            return marker.name
    return 'functional'


def pytest_addoption(parser):
    parser.addoption('--test-kind', choices=TEST_KINDS, default=DEFAULT_TEST_KIND,
                     help='选择功能测试、性能专项、按需验收或全部测试')


def pytest_configure(config):
    config.addinivalue_line('markers', 'performance: 性能专项测试，须显式选择执行')
    config.addinivalue_line('markers', 'acceptance: 按需验收测试，须显式选择执行')


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    kind = config.getoption('--test-kind')
    if kind == 'all':
        return
    selected, deselected = [], []
    for item in items:
        (selected if classify_test_item(item) == kind else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
