"""取消服务使用真实数据库线程，存储契约的准备连接独立保留。"""

import pytest_asyncio

from ..support.database import business_database, database_path
from ..support.capture import capture_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    async with capture_test_session():
        yield


@pytest_asyncio.fixture
async def cancellation_database(pipeline):
    async with business_database(database_path(pipeline)) as database:
        yield database
