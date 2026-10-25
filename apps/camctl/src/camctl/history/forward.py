"""以创建事件的原值和统一派生登记建立正向恢复行。"""
from camctl.history.changes import EntityEventLink


def created_values(event, change, ref, count):
    from camctl.persistence.transaction import _derived_map
    link = EntityEventLink(ref[0], ref[1], event.event_id, count)
    derived = _derived_map((event,), (link,), {ref: count})
    return {'id': change.row_id, **change.after.values,
            **derived.get((change.table, change.row_id), {})}
