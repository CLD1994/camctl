"""软件恢复责任共用的阶段类型。"""

from enum import StrEnum

from camctl.contracts.enums import enum_for


#: 普通 phase 列的编号由组件枚举登记维护，内存只派生成员名的小写文本。
#: 保留原类型的序列化模块路径；运行模块导入同一类型。
RecoveryPhase = StrEnum("RecoveryPhase", {member.name: member.name.lower()
    for member in enum_for("software_recoveries.phase")},
    module="camctl.capture.software_recovery")


def recovery_trigger_active(trigger):
    """触发动作必须仍在执行范围内且取消未生效。"""
    return trigger['status'] in (1, 2) and not trigger['cancel_requested']
