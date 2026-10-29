"""结果页存储测试通过公开操作仓储建立原核实尝试。"""

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository


def begin_result_check(owned, runtime, action_id):
    """存储测试只建立核实事务，不通过业务处理器提前消费结果页。"""
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    result = OperationRepository().begin_attempt(
        AttemptIntent(operation="result", action_id=action_id,
            kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(activity_id=activity_id), query_purpose=None,
            config=runtime.check_config, occurred_at=runtime.wall_us()),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value
