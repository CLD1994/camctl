"""正式副本、交付和文件清理入口保留数据库原异常及控制对象。"""

import asyncio

import pytest

from camctl.outputs import copy, handoff, work_files
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories.outputs import OutputsRepository

from ..support.outputs import output_operation
from ..support.database import business_database, database_path
from .test_copy_complete import complete_env
from .test_work_files import storage_work_env as work_env, _cancel_delivery
from .test_read_associations import read_targets


@pytest.fixture
def file_world(request, boundary):
    return request.getfixturevalue('complete_env' if boundary is copy else 'work_env')


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [copy, handoff, work_files])
@pytest.mark.parametrize("controlled", [False, True])
async def test_file_stage_wrapper_keeps_original_database_error(boundary, controlled, file_world, tmp_path):
    owned, roots, prepared = file_world
    original = asyncio.CancelledError("original control") if controlled else OSError(5, "original DB error")
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=original)
    class SavePort(OutputsRepository):
        calls = []
        def rejected(self, request, key, worker):
            self.calls.append((request, key))
            return outcome
        save_verification = rejected
        save_publication_intent = rejected
        save_retention_release = rejected
    port = SavePort()
    if boundary is work_files:
        _cancel_delivery(owned, prepared.delivery_id)
    expected = (copy.CopyCompletionError if boundary is copy else
                handoff.DeliveryHandoffError if boundary is handoff else work_files.WorkFileCleanupError)
    with pytest.raises(asyncio.CancelledError if controlled else expected) as caught:
        if boundary is copy:
            await output_operation(owned, copy.complete_copy, prepared.copy_id, copy.CompletionContext,
                repository=port, roots=roots, occurred_at=1_800_000_000_000_000)
        elif boundary is handoff:
            await output_operation(owned, handoff.publish_delivery, prepared.delivery_id, handoff.DeliveryContext,
                repository=port, directories=handoff.DeliveryDirectories(
                    roots.staging, tmp_path / 'ready', tmp_path / 'processing'),
                occurred_at=1_800_000_000_000_000)
        else:
            async with business_database(database_path(owned)) as database:
                await work_files.clean_one_work_file(prepared.target_file_id, work_files.WorkFileContext(
                    repository=port, database=database, staging=roots.staging,
                    limits=work_files.WorkFileLimits(1, 1), occurred_at=1_800_000_000_000_000))
    assert len(port.calls) == 1
    if controlled:
        assert caught.value is original
    else:
        assert caught.value.__cause__ is original
        assert caught.value.stage == {
            copy: 'verification_save_unknown', handoff: 'intent_save_unknown',
            work_files: 'retention_release_failed',
        }[boundary]
    assert db_outcome_for(caught.value) is outcome
    assert (roots.staging / ('deliveries/' + str(prepared.target_file_id) + '.part')).exists()
