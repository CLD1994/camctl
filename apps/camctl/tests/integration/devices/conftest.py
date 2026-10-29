"""本目录按测试管理正式拍摄装配的实际资源。"""

import pytest_asyncio

from ..support.capture import capture_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    async with capture_test_session() as session:
        yield session
