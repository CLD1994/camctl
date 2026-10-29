"""共用测试环境保留真实数据库线程及完整关闭。"""

from threading import get_ident

import pytest

from camctl.persistence.models import DbExecutorError
from ..support.database import business_database, create_test_database


@pytest.mark.asyncio
async def test_business_scope_uses_a_real_database_worker_and_closes_it(tmp_path):
    path = tmp_path / "state.db"
    create_test_database(path)
    caller = get_ident()
    async with business_database(path) as database:
        assert await database.read(lambda owned: get_ident()) != caller
        assert await database.read(lambda owned: owned.connection.execute(
            "SELECT acknowledged_wm FROM runtime_state WHERE id=1").fetchone()[0]) == 0
    with pytest.raises(DbExecutorError):
        await database.read(lambda owned: owned.metadata)


@pytest.mark.asyncio
async def test_business_scope_closes_after_the_original_test_error(tmp_path):
    path = tmp_path / "state.db"
    create_test_database(path)
    failure = LookupError("受控业务失败")
    with pytest.raises(LookupError) as caught:
        async with business_database(path) as database:
            await database.read(lambda owned: owned.metadata)
            raise failure
    assert caught.value is failure
    with pytest.raises(DbExecutorError):
        await database.read(lambda owned: owned.metadata)
