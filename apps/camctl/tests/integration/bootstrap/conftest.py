"""bootstrap 每个测试独立拥有正式拍摄装配产生的数据库和文件责任。"""

import pytest_asyncio

from ..support.capture import capture_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    async with capture_test_session() as session:
        yield session
