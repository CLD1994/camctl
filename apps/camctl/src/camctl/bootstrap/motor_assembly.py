"""按有界批次推进电机动作，发送许可只在当前会话内保留。"""
from contextlib import closing

from camctl.motor.service import MotorRuntime, advance_motor
from camctl.persistence.repositories.motor import MotorRepository


def motor_flow(writer, permits):
    async def flow(context):
        now = context.clock.utc_micros()

        def due_actions(owned):
            # 未决意图须进入恢复，即使当前时间已退回计划时间之前。
            with closing(owned.connection.execute(
                "SELECT id FROM actions WHERE type = 8 AND status IN (1, 2)"
                " AND (scheduled_at <= ? OR status = 2)"
                " ORDER BY plan_id, input_index LIMIT 64", (now,),
            )) as cursor:
                return tuple(row[0] for row in cursor)

        identities = await context.database.read(due_actions, description="电机候选读取")
        runtime = MotorRuntime(context.database, MotorRepository(), writer, context.clock,
                               context.clock_policy, permits)
        for identity in identities:
            await advance_motor(identity, runtime)
        # 取消事务可能已结束持有许可的动作，及时释放进程内记录。
        for identity in tuple(permits):
            facts = await context.database.read(
                lambda owned: runtime.repository.read_facts(identity, owned),
                description="电机许可终态读取")
            if facts.action["status"] in (3, 4, 5, 6):
                permits.pop(identity, None)
    return flow
