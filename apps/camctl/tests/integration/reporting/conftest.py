"""每个报告场景分别关闭正式拍摄装配所拥有的真实资源。"""

import pytest_asyncio

from ..support.capture import capture_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    async with capture_test_session() as session:
        yield session
