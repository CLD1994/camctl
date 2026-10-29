import asyncio
import json
from types import SimpleNamespace

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbJob, DbJobKind, DbOutcome, DbOutcomeKind, DbPriority


async def main():
    executor = DbExecutor(lambda: None)
    executor._ensure_thread_locked = lambda: None
    key = OperationKey('a' * 32)
    jobs = [DbJob(key, f'save-{value}', DbJobKind.WRITE, DbPriority.BUSINESS,
                  lambda owned, value=value: DbOutcome(DbOutcomeKind.COMPLETED, value=value))
            for value in ('first', 'second')]
    first, second = [executor.submit_write(job) for job in jobs]
    for _ in jobs:
        job = executor._core.pop_next()
        executor._execute(job, SimpleNamespace())
        executor._core.complete(job.key)
    await asyncio.sleep(0)
    print(json.dumps({
        'first_waiter_completed': first.done(),
        'second_waiter_completed': second.done(),
        'second_waiter_value': second.result().value if second.done() else None,
        'submitted_values': ['first', 'second'],
        'actual_worker_threads': 0,
    }))


asyncio.run(main())
