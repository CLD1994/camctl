"""安装版仓储的只读同步点：实际提交后暂停，供真实旧进程组退出。"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import time


def install():
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories import segment_selections
    from camctl.reporting import policy

    stage = os.environ["CAMCTL_OSMO_TEST_SEGMENT_FAULT"]
    marker = Path(os.environ["CAMCTL_OSMO_TEST_SEGMENT_MARKER"])

    def hit(owned, request, key):
        if marker.exists():
            return
        db = owned.connection
        assert not db.in_transaction
        record = {
            "stage": stage,
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "utc_ns": time.time_ns(),
            "key": str(key),
            "request": asdict(request)
            if hasattr(request, "__dataclass_fields__")
            else request,
            "outputs": db.execute(
                "SELECT id,source_action_id,device_file_id,kind FROM outputs ORDER BY id"
            ).fetchall(),
            "scope": db.execute(
                "SELECT id,set_id,ordinal,output_id FROM segment_scope_members ORDER BY id"
            ).fetchall(),
            "items": db.execute(
                "SELECT id,selection_id,requested_output_id,output_id FROM obtain_items ORDER BY id"
            ).fetchall(),
            "reports": db.execute(
                "SELECT id,frozen_event_id,size_bytes,sha256 FROM reports ORDER BY id"
            ).fetchall(),
        }
        temporary = marker.with_suffix(".new")
        temporary.write_text(json.dumps(record, default=str), encoding="utf-8")
        temporary.replace(marker)
        while True:
            time.sleep(0.05)

    if stage in ("scan", "finish"):
        method = "save_result_page" if stage == "scan" else "finish_segment_recording"
        original = getattr(CaptureRepository, method)

        def observe(self, request, key, owned, **kwargs):
            result = original(self, request, key, owned, **kwargs)
            if result.kind is DbOutcomeKind.COMPLETED:
                hit(owned, request, key)
            return result

        setattr(CaptureRepository, method, observe)
    elif stage == "selection":
        original = segment_selections.fix_segment_selection

        def observe(request, key, owned):
            result = original(request, key, owned)
            if result.kind is DbOutcomeKind.COMPLETED:
                hit(owned, request, key)
            return result

        segment_selections.fix_segment_selection = observe
    elif stage == "report":
        original = policy.record_report_bytes

        def observe(key, owned, report_id, contents, **kwargs):
            result = original(key, owned, report_id, contents, **kwargs)
            terminal = owned.connection.execute(
                "SELECT 1 FROM actions WHERE name='capture' AND status IN (3,4,6)"
            ).fetchone()
            if result.kind is DbOutcomeKind.COMPLETED and terminal:
                hit(owned, {"report_id": report_id, "contents": asdict(contents)}, key)
            return result

        policy.record_report_bytes = observe
    else:
        raise ValueError(f"未登记的分段同步点: {stage}")
