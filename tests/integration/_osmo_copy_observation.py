"""在恢复读取仍未转发新字节时核对原副本与续传位置。"""

import shlex


def assert_copy_resumed_before_data(
    *,
    interrupted_read,
    resumed_read,
    resumed_gate,
    saved_copy,
    resumed_copies,
    saved_deliveries,
    resumed_deliveries,
):
    progress = saved_copy[2]
    assert progress > 0 and progress % 65536 == 0
    assert interrupted_read["kind"] == "read_blocked"
    assert interrupted_read["forwarded_bytes"] >= progress
    original_command = shlex.split(interrupted_read["script"])
    assert len(original_command) == 4
    assert original_command[0] == "dd" and original_command[1].startswith("if=")
    assert original_command[2:] == ["bs=65536", "skip=0"]
    assert resumed_read["kind"] == "read_started"
    assert resumed_read["started_ns"] > interrupted_read["started_ns"]
    assert shlex.split(resumed_read["script"]) == [
        *original_command[:3], f"skip={progress // 65536}"
    ]
    assert resumed_gate["kind"] == "read_blocked"
    assert resumed_gate["started_ns"] == resumed_read["started_ns"]
    assert resumed_gate["forwarded_bytes"] == 0
    assert resumed_copies == [saved_copy]
    assert resumed_deliveries == saved_deliveries
