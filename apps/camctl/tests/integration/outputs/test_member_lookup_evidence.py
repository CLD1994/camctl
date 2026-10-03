"""成员错误的结论由可靠当前查询决定，事件提案不能证明自身。"""

from dataclasses import replace

import pytest

from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError, validate_event
from camctl.persistence.transaction import event_envelope, update_change

from .test_member_guard import member_context, read_targets
from .test_qualification import _NOW


@pytest.mark.parametrize("code,location,queried,valid", [
    (1, "absent", False, False), (1, "absent", True, True),
    (1, "same", True, False), (1, "other", True, False),
    (1, "future", True, True), (1, "future", False, False),
    (2, "absent", False, False), (2, "absent", True, False),
    (2, "same", True, False), (2, "other", False, True),
    (2, "other", True, True), (2, "future", True, False),
])
def test_member_failure_requires_the_corresponding_current_evidence(member_context, code, location, queried, valid):
    context = member_context
    item = context.state_rows["obtain_items"][101]
    item.update(status=1, basis=5, requested_output_id=999, output_id=None)
    target = {**context.state_rows["outputs"][701], "id": 999,
              "source_action_id": 11 if location == "same" else 12}
    if location in ("same", "other"):
        context.state_rows["outputs"][999] = target
    future = ({**context.state_rows, "outputs": {**context.state_rows["outputs"], 999: target}}
              if location == "future" else None)
    context = replace(context, transaction_rows=future,
        read_coverage=ReadCoverage({("outputs", "id"): frozenset({999})}) if queried else ReadCoverage())
    after = {"status": 4, "error_code": code, "error_details_json": {"requested_output_id": "999"}}
    event = event_envelope(2, 2, 21, 2, (update_change("obtain_items", 101,
        {name: item[name] for name in after}, after),), _NOW)
    if valid:
        validate_event(event, context)
    else:
        with pytest.raises(EventValidationError):
            validate_event(event, context)
