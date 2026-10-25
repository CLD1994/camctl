"""清理持久责任的有界发现；动作、成员及流程终态独立判断。"""

# 此条件由执行发现、needs_run 与取消等待共用。
_ABSENCE = """EXISTS(SELECT 1 FROM outputs o WHERE o.id=c.output_id
 AND (NOT(o.availability=3 AND o.cleanup_status=4 AND o.cleanup_error_json IS NULL)
 OR EXISTS(SELECT 1 FROM device_files f WHERE f.id=o.device_file_id AND f.presence_state<>3)
 OR EXISTS(SELECT 1 FROM intermediate_files f WHERE f.id=o.intermediate_file_id AND f.cleanup_state<>4))
 AND EXISTS(
 SELECT 1 FROM operation_runs ar JOIN operation_attempts aa ON aa.run_id=ar.id,
 json_each(aa.result_json,'$.observations') obs
 WHERE ar.cleanup_item_id=c.id AND ar.kind IN (4,5) AND aa.status<>1
 AND aa.result_event_id IS NOT NULL AND (
 json_extract(obs.value,'$.type')='file_absent' OR
 (json_extract(obs.value,'$.type') IN ('file_presence','file_delete_presence')
 AND json_extract(obs.value,'$.data.present')=0))))"""
_RESIDUAL = (
    """EXISTS(
 SELECT 1 FROM operation_runs r WHERE r.cleanup_item_id=c.id AND r.kind IN (4,5)
 AND (r.status IN (1,2) OR EXISTS(
 SELECT 1 FROM operation_attempts a WHERE a.run_id=r.id AND (a.status=1
 OR (c.status NOT IN (4,5,6) AND a.result_event_id IS NOT NULL))))) OR """
    + _ABSENCE
)


async def cleanup_item_batches(database, *, batch_size=100):
    """只保留当前成员页和稳定 id 续读位置，不一次保留全部责任。"""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("清理发现批量必须是正整数")
    after = 0
    while True:
        rows = await database.read(lambda owned: owned.connection.execute(
            "SELECT c.id FROM cleanup_items c WHERE c.id>? AND (c.status NOT IN (4,5,6) OR "
            + _RESIDUAL
            + ") ORDER BY c.id LIMIT ?",
            (after, batch_size),
        ).fetchall(), description="分页发现清理责任")
        if not rows:
            return
        after = rows[-1][0]
        yield tuple(row[0] for row in rows)


def cleanup_action_has_responsibility(connection, action_id):
    """成员终态仍须等原调用和开放伴随流程可靠结束。"""
    return (
        connection.execute(
            "SELECT 1 FROM cleanup_items c WHERE c.action_id=? AND (c.status NOT IN (4,5,6) OR "
            + _RESIDUAL
            + ") LIMIT 1",
            (action_id,),
        ).fetchone()
        is not None
    )


def extra_cleanup_settlements(connection):
    """补齐通用 open-run 计数未覆盖的成员及终态流程在途责任。"""
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM cleanup_items c WHERE (c.status NOT IN (4,5,6) OR "
            + _RESIDUAL
            + ") AND NOT EXISTS(SELECT 1 FROM operation_runs r"
            " WHERE r.cleanup_item_id=c.id AND r.status IN (1,2))"
        ).fetchone()[0]
    )


def has_cleanup_handoff(connection):
    """当前端口检查前，核对是否存在须先接手的原持久责任。"""
    return (
        connection.execute(
            "SELECT 1 FROM cleanup_items c WHERE " + _RESIDUAL + " LIMIT 1"
        ).fetchone()
        is not None
    )
