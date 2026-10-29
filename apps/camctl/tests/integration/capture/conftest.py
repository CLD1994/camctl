"""每个拍摄测试独立拥有正式装配所创建的实际资源。"""

import pytest_asyncio



@pytest_asyncio.fixture(autouse=True)
async def capture_resources():
    from ..support.capture import capture_test_session
    from ..support.outputs import output_test_session

    with output_test_session():
        async with capture_test_session() as session:
            yield session
