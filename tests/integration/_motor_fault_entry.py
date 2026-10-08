"""仅测试使用的进程中断桥：生产事务和管道调用前后设置确定断点。"""
import os
from pathlib import Path


def main():
    from camctl.cli import main as cli
    from camctl.motor.notification import NotificationWriter
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.motor import MotorRepository

    phase = os.environ.get('CAMCTL_MOTOR_FAULT', '')
    marker = Path(os.environ['CAMCTL_MOTOR_FAULT_MARKER'])
    trace = Path(os.environ['CAMCTL_MOTOR_WRITE_TRACE'])

    def hit(point):
        if point != phase or marker.exists():
            return False
        marker.write_text(point)
        if point.endswith('_unknown'):
            return True
        os._exit(91)

    prepare = MotorRepository.prepare_send
    finish = MotorRepository.finish_send
    observe = MotorRepository.observe_window
    send = NotificationWriter.send

    def prepare_once(self, *args, **kwargs):
        hit('before_intent')
        if hit('intent_absent_unknown'):
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('意图未提交且返回丢失'))
        result = prepare(self, *args, **kwargs)
        if result.kind is DbOutcomeKind.COMPLETED:
            hit('after_intent')
            if hit('intent_commit_unknown'):
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('意图提交返回丢失'))
        return result

    def send_once(self, message):
        hit('before_write')
        result = send(self, message)
        with trace.open('ab') as stream:
            stream.write(message[:result.written_bytes])
        hit('after_write')
        return result

    def finish_once(self, *args, **kwargs):
        hit('before_result')
        if hit('result_absent_unknown'):
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('结果未提交且返回丢失'))
        result = finish(self, *args, **kwargs)
        if result.kind is DbOutcomeKind.COMPLETED:
            hit('after_result')
            if hit('result_commit_unknown'):
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('结果提交返回丢失'))
        return result

    def observe_once(self, *args, **kwargs):
        result = observe(self, *args, **kwargs)
        if result.kind is DbOutcomeKind.COMPLETED and hit('observe_commit_unknown'):
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('窗口观察提交返回丢失'))
        return result

    MotorRepository.observe_window = observe_once
    MotorRepository.prepare_send = prepare_once
    MotorRepository.finish_send = finish_once
    NotificationWriter.send = send_once
    return cli()


if __name__ == '__main__':
    raise SystemExit(main())
