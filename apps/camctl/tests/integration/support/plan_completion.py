"""按已提交历史核对动作终态及所属计划的共同事务。"""


def plan_before(connection, plan_id):
    row = connection.execute(
        "SELECT status,last_event_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert row is not None
    return row


def assert_action_plan(connection, action_id, plan_id, key, *,
                       action_status, previous_plan, completed):
    assert previous_plan[0] == 2
    action = connection.execute(
        "SELECT status,last_event_id FROM actions WHERE id=? AND plan_id=?",
        (action_id, plan_id)).fetchone()
    assert action is not None and action[0] == action_status
    transaction = connection.execute(
        "SELECT id FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone()
    assert transaction is not None
    assert connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?", (action[1],)).fetchone() == transaction
    current_plan = plan_before(connection, plan_id)
    if completed:
        assert current_plan[0] == 3
        assert connection.execute(
            "SELECT transaction_id FROM history_events WHERE id=?", (current_plan[1],)).fetchone() == transaction
    else:
        assert current_plan == previous_plan
    assert not connection.in_transaction
