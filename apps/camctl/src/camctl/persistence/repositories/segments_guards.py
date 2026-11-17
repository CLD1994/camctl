"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.contracts.values import ConsistencyError
from camctl.history.validators import EventValidationError


def register_segment_guards(register):
    def guard(event,context):
        try:
            if context.current_reads is None:raise ConsistencyError('分段守卫缺少原事务有限读端口')
            context.current_reads.verify_segment_event(event,context)
        except (ValueError,KeyError,TypeError) as error:
            raise EventValidationError(str(error)) from error
    register('segments',guard)
