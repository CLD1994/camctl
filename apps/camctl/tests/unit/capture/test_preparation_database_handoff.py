"""准备恢复在队列内保存原申请，闭包不改变仓储身份。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.preparation import RuntimePreparationCache, resume_preparation_saves
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from support.capture_database import capture_database

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("explicit", [False, True])
async def test_empty_preparation_cache_needs_no_database_operation(explicit):
    database = create_autospec(type(capture_database()), instance=True)
    repositories = ({"operations": create_autospec(OperationRepository, instance=True),
        "scheduling": create_autospec(SchedulingRepository, instance=True),
        "capture": create_autospec(CaptureRepository, instance=True)} if explicit else {})

    await resume_preparation_saves(database, RuntimePreparationCache(), **repositories)

    database.read.assert_not_awaited()
    database.write.assert_not_awaited()


async def test_original_reservation_and_endings_use_their_supplied_repositories():
    cache = RuntimePreparationCache()
    reservation, release, failure = object(), object(), object()
    keys = tuple(new_operation_key() for _ in range(3))
    cache.reservations[1] = (reservation, keys[0])
    cache.endings.update({2: ("release", release, keys[1]), 3: ("fail_preparation", failure, keys[2])})
    scheduling = create_autospec(SchedulingRepository, instance=True)
    capture = create_autospec(CaptureRepository, instance=True)
    scheduling.reserve_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    scheduling.fail_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        SimpleNamespace(outcome=SimpleNamespace(value="released")))

    await resume_preparation_saves(capture_database(), cache,
        operations=create_autospec(OperationRepository, instance=True),
        scheduling=scheduling, capture=capture)

    assert not cache.reservations and not cache.endings
    assert scheduling.reserve_preparation.call_args.args[:2] == (reservation, keys[0])
    assert capture.release_occupancy.call_args.args[:2] == (release, keys[1])
    assert scheduling.fail_preparation.call_args.args[:2] == (failure, keys[2])
