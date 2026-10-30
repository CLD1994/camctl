"""在公开仓储与事务内核之间装饰正式 AtomicCommand 协议。"""
from unittest.mock import patch


def decorate_transaction_command(monkeypatch, repository_module, transform):
    """替换本次命令提案后仍交给真实内核，不访问命令实现类型。"""
    commit = repository_module.commit_operation

    def decorated(command, key, owned):
        class Command:
            def plan(self, scope):
                return transform(command, scope)
        return commit(Command(), key, owned)

    monkeypatch.setattr(repository_module, 'commit_operation', decorated)


def plan_operation(scope, repository_module, operation):
    """从公开仓储的命令交付边界取得正式 plan，供组合事务反例使用。"""
    class Planned(BaseException):
        def __init__(self, plan):
            self.plan = plan
    def planned(command, key, owned):
        raise Planned(command.plan(scope))
    with patch.object(repository_module, 'commit_operation', planned):
        try:
            operation()
        except Planned as captured:
            return captured.plan
    raise AssertionError('公开仓储没有交付本次完整事务命令')


def capture_operation_command(repository_module, operation):
    """取得仓储交付的不透明命令；不执行计划、不写库，也不返回虚假结果。"""
    class Delivered(BaseException):
        def __init__(self, command):
            self.command = command

    def receive(command, key, owned):
        raise Delivered(command)

    with patch.object(repository_module, 'commit_operation', receive):
        try:
            operation()
        except Delivered as delivered:
            return delivered.command
    raise AssertionError('仓储没有交付事务命令')
