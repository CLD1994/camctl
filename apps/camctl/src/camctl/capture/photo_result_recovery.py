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


class PhotoBindingListing:
    """只在下一次实际角色调用前核绑定，绑定失败形成可靠未派发结果。"""

    def __init__(self, port, check_binding, next_role):
        self.port, self.check_binding, self.next_role = port, check_binding, next_role
        self._dispatched = False

    def close_scan(self, ticket):
        if self.port is not None and self._dispatched:
            self.port.close_scan(ticket)

    async def list_page(self, ticket, *, cursor, **kwargs):
        error = await self.check_binding(ticket)
        if error is None:
            if self.port is None:
                raise ConsistencyError("原照片绑定匹配但没有来源读取端口")
            self._dispatched = True
            return await self.port.list_page(ticket, cursor=cursor, **kwargs)
        from camctl.capture.result_inputs import page_from_outcome
        from camctl.devices.evidence import DeviceObservation
        from camctl.operations.models import (
            AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
        )

        role = await self.next_role(ticket, cursor)
        outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT, error=error,
            settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                  EvidenceValue("photo_result_not_dispatched", 1, {})),
            observations=(DeviceObservation("photo_result_attempt", 2, {
                "activity_id": ticket.target_id, "role": role, "steps": [], "comparison": None}),))
        return page_from_outcome(ticket, outcome, cursor=cursor)
