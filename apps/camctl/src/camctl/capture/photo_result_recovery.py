"""按原角色页恢复照片文件子责任。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError


def saved_photo_page_refs(connection, *, action_id, activity_id, run_id):
    """读取同一原 RESULTS 流程中至多两份可靠角色页。"""
    from camctl.persistence.repositories.result_pages import _at

    with closing(connection.execute(
        "SELECT p.page_event_id FROM result_page_files p"
        " JOIN operation_runs r ON r.id=p.run_id"
        " WHERE p.action_id=? AND r.activity_id=? AND r.id=?"
        " AND CASE WHEN json_valid(p.identity) THEN json_extract(p.identity,'$[0]') END='hb-download-v3'"
        " ORDER BY p.page_event_id LIMIT 3", (action_id, activity_id, run_id))) as cursor:
        rows = cursor.fetchall()
    refs, roles = [], set()
    for event_id, in rows:
        saved, _ = _at(connection, event_id)
        observations = [item for item in saved.page.outcome.observations
                        if item.type == "result_files_listed" and item.version == 3]
        if (saved.ref.ticket.run_id != run_id or saved.ref.ticket.target_id != str(activity_id)
                or len(saved.page.entries) != 1 or len(observations) != 1):
            raise ConsistencyError("照片角色索引与原可靠结果页不一致")
        role = observations[0].data.get("role")
        if role not in ("original", "thumbnail") or role in roles:
            raise ConsistencyError("同一照片角色已有多个原来源页")
        roles.add(role)
        refs.append(saved.ref)
    return tuple(refs)
