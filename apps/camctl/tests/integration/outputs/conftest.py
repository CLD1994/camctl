"""涉及真实拍摄来源的产物测试统一拥有数据库与文件任务资源。"""

from contextlib import ExitStack

import pytest
import pytest_asyncio

from ..support.capture import capture_test_session
from ..support.outputs import output_test_session


@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    with output_test_session():
        async with capture_test_session() as session:
            yield session


@pytest.fixture
def port_stack():
    with ExitStack() as stack:
        yield stack
