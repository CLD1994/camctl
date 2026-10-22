"""从来源动作的冻结合同解析分段文件规则。"""

from camctl.devices.segment_profiles import profile_for_policy
from camctl.persistence.transaction import row_facts


def profile_for_action(connection, action_id):
    action = row_facts(connection, "actions", action_id)
    return profile_for_policy(action["execution_spec_json"]["result_policy"])
