"""bootstrap 每个测试独立拥有正式拍摄装配产生的数据库和文件责任。"""

import pytest
import pytest_asyncio

from ..support.capture import capture_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    async with capture_test_session() as session:
        yield session


@pytest.fixture(scope="session")
def cleanup_source_cache(tmp_path_factory):
    """同一次运行中按驱动保存来源，活动世界仍由单个测试拥有。"""
    return {"root": tmp_path_factory.mktemp("cleanup-sources"), "worlds": {}}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if report.when == "call":
        item.cleanup_call_passed = report.passed
