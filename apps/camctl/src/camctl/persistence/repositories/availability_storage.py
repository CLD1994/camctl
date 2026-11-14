"""设备检查的事务与事实读取端口。"""

from camctl.persistence.repositories.availability_create import CreateDisposition, ensure_check
from camctl.persistence.repositories.availability_failure import (
    FailureDisposition, fail_photo_availability, fail_read_availability,
)
from camctl.persistence.repositories.availability_reads import (
    read_action_facts, read_binding_checks_page, read_dispatch_decision,
    read_open_check, read_run_facts,
)
from camctl.persistence.repositories.availability_release import (
    ResolveDisposition, finish_availability, finish_if_resolved,
)
from camctl.persistence.repositories.availability_retry import complete_availability_retry_wait
from camctl.persistence.repositories.availability_wait_finish import (
    WaitingBindingResult, WaitingFinishDisposition, finish_waiting_availability,
)
from camctl.persistence.repositories.operations import OperationRepository


class AvailabilityStorage:
    """同一会话借用数据库执行器，只暴露检查所需事务与有界事实。"""

    CreateDisposition = CreateDisposition
    FailureDisposition = FailureDisposition
    ResolveDisposition = ResolveDisposition
    WaitingBindingResult = WaitingBindingResult
    WaitingFinishDisposition = WaitingFinishDisposition
    read_action_facts = staticmethod(read_action_facts)
    read_binding_checks_page = staticmethod(read_binding_checks_page)
    read_dispatch_decision = staticmethod(read_dispatch_decision)
    read_open_check = staticmethod(read_open_check)
    read_run_facts = staticmethod(read_run_facts)
    ensure_check = staticmethod(ensure_check)
    fail_photo_availability = staticmethod(fail_photo_availability)
    fail_read_availability = staticmethod(fail_read_availability)
    finish_availability = staticmethod(finish_availability)
    finish_if_resolved = staticmethod(finish_if_resolved)
    finish_waiting_availability = staticmethod(finish_waiting_availability)
    complete_availability_retry_wait = staticmethod(complete_availability_retry_wait)

    def __init__(self):
        self.operations = OperationRepository()

    def begin_availability_attempt(self, *args):
        return self.operations.begin_availability_attempt(*args)

    def finish_attempt(self, *args):
        return self.operations.finish_attempt(*args)
