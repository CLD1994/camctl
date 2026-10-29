"""取消来源恢复的公开输入、仓储准备和真实数据库资源。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.cancellation.models import (ApplyCancelTarget, CancelApplyMode, CancellationEffect,
    FixedCancelSet, FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction)
from camctl.contracts.values import new_operation_key, to_utc_micros
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .acceptance import accept_document
from .database import business_database, create_test_database
from ..acceptance.test_acceptance import Catalog, _plan_body

NOW = int(to_utc_micros("2026-01-15 09:00:00"))


@asynccontextmanager
async def origin_world(home, *, fixed=False):
    home.mkdir(exist_ok=True)
    path = home / "state.db"
    create_test_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    clock = SimpleNamespace(utc_micros=lambda: NOW)

    class PreviewCatalog(Catalog):
        def parameter_definition(self, *args):
            return replace(super().parameter_definition(*args), preview_supported=True)

    context = AcceptanceContext(CommandMode.RUN, PreviewCatalog(), AcceptanceRepository(), clock)
    photo = _plan_body()["actions"][0]
    preview = {"name": "预览", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {"source": {"action_name": "shoot"},
            "filter": "preview", "purpose": "auto_preview"}}
    cancel = {"name": "C1", "type": "cancel_task", "params": {"target": {"action_instance_id": "1"}}}
    repository = CancellationRepository()

    def completed(method, request):
        receipt = method(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return receipt.value

    try:
        accepted = await accept_document(_plan_body(actions=[photo, preview, cancel]), context, path)
        assert accepted.plan_id == 1
        completed(SchedulingRepository().start_action, StartActionRequest(1, NOW, NOW))
        completed(repository.start_cancel_action, StartCancelAction(3, NOW))
        if fixed:
            # 已固定来源的目标来自正式公开命令，尚未执行目标设备工作。
            fixed_targets = completed(repository.fix_cancel_targets, FixCancelTargets(3, FixedCancelSet((
                FixedTarget(1, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), NOW))
            completed(repository.apply_cancel_target, ApplyCancelTarget(
                fixed_targets.item_ids[0], CancelApplyMode.WITH_STOP, NOW))
        accepted = await accept_document(_plan_body(request_id="2", actions=[{
            "name": "C2", "type": "cancel_task", "params": {"target": {"action_instance_id": "3"}},
        }]), context, path)
        assert accepted.plan_id == 2
        completed(repository.start_cancel_action, StartCancelAction(4, NOW))
        selected = completed(repository.fix_cancel_targets, FixCancelTargets(4, FixedCancelSet((
            FixedTarget(3, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), NOW))
        completed(repository.apply_cancel_target,
            ApplyCancelTarget(selected.item_ids[0], CancelApplyMode.WITH_STOP, NOW))
        async with business_database(path) as database:
            yield SimpleNamespace(path=path, owned=owned, database=database, clock=clock)
    finally:
        owned.connection.close()
