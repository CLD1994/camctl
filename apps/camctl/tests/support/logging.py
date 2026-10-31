"""按真实日志通道接口观察同步提交，不启动写入线程。"""

from unittest.mock import create_autospec

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult, LogChannel


def recording_log_channel(records, *, before_record=None):
    """记录公共入口的实际顺序，并返回已接纳、已写入的真实回执。"""
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    def admit(record):
        if before_record is not None:
            before_record(record)
        records.append(record)
        return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)

    def submit(record, *, receipt=None):
        result = admit(record)
        if receipt is None:
            receipt = LogReceipt()
        receipt.complete_admission(result)
        receipt.complete_write(AdmissionResult(result.decision, result.queue_depth, delivered=True))
        return receipt

    channel.submit.side_effect = submit
    channel.slog.side_effect = admit
    return channel
