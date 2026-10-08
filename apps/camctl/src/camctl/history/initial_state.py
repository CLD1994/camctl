"""格式 1 在部署初始化时已经存在的业务事实。"""

from camctl.contracts.enums import load_registry
from camctl.history.events import business_columns


def runtime_state_values() -> dict:
    """返回全局单例的完整初始行，供实际建库与初始历史恢复使用。

    可空业务列在初始化时没有事实；累计 ACK 水位从 0 开始。
    列集合和单例身份分别来自事件登记与固定列登记。
    """
    values = {column: None for column in sorted(business_columns("runtime_state"))}
    values["acknowledged_wm"] = 0
    values["id"] = load_registry()["fixed_columns"]["runtime_state.id"]["value"]
    return values
