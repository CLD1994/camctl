"""已审阅单文件脚本的冻结识别与本地树模型；不执行远端 shell。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import time


# 人工核对 file_presence_script 的完整静态尾部。
# 只解码外层 sh -c 和前三项 manifest 赋值，不实现 shell 语法。
_TAIL_SHA = {
    "query": "f297419ea3dfdf9460fe23fa361e6fee90f367bee59f58c6bf6f5ad7ea47fd12",
    "delete": "5da5ae210cf6fb9fba68b2bd1b9fb179ef43acb1588d1e185acce62aeb8894dd",
}


def _process():
    stat = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(),
            "starttime": int(stat[19]), "utc_ns": time.time_ns()}


def _append(path, record):
    record = {**record, **_process()}
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _tree(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("case 远端树不接受符号链接")
        if path.is_file():
            content = path.read_bytes()
            result["/" + path.relative_to(root).as_posix()] = {
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return result


def file_call(script, *, root, calls_path):
    manifest = json.loads(Path(os.environ["CAMCTL_CLEANUP_MANIFEST"]).read_text())
    first, second, third, tail = script.split("\n", 3)
    operation = next((op for op, digest in _TAIL_SHA.items()
                      if hashlib.sha256(tail.encode()).hexdigest() == digest), None)
    if operation is None:
        raise ValueError("正式文件脚本静态尾部与已审阅冻结值不同")
    targets = [target for target in manifest["targets"] if first == "target=" + shlex.quote(target["path"])]
    if len(targets) != 1:
        raise ValueError("清理脚本没有精确匹配已登记 FileIdentity")
    target = targets[0]
    if (second != "parent=" + shlex.quote(target["parent"])
            or third != "scan_root=" + shlex.quote(target["scan_root"])
            or operation not in target["operations"]):
        raise ValueError("清理脚本改变 manifest 的范围或操作")
    root = root.resolve(strict=True)
    if str(root) != manifest["root"]:
        raise ValueError("清理模型的 case 根发生变化")
    local = root / target["path"].lstrip("/")
    if root not in local.resolve().parents:
        raise ValueError("清理目标越过 case 根")
    before = _tree(root)
    previous = []
    if calls_path.exists():
        previous = [json.loads(line) for line in calls_path.read_text().splitlines()]
    prior_deletes = sum(row.get("kind") == "cleanup" and row.get("operation") == "delete"
                        and row.get("stage") == "entered" for row in previous)
    fault = manifest.get("fault") if operation == "delete" and prior_deletes == 0 else None
    import sqlite3
    with sqlite3.connect("file:" + manifest["database"] + "?mode=ro", uri=True) as connection:
        item, = connection.execute("SELECT c.id,a.id FROM cleanup_items c JOIN actions a ON a.id=c.action_id JOIN plans p ON p.id=a.plan_id WHERE p.request_id=? AND c.output_id=?", (manifest["request_id"], target["output_id"])).fetchall()
        connection.row_factory = sqlite3.Row
        ticket, = connection.execute(
            "SELECT t.*,r.responsibility_key,h.operation_key FROM operation_attempts t "
            "JOIN operation_runs r ON r.id=t.run_id JOIN history_events e ON e.id=t.intent_event_id "
            "JOIN history_transactions h ON h.id=e.transaction_id WHERE r.cleanup_item_id=? "
            "AND r.responsibility_key=? ORDER BY t.attempt_no DESC LIMIT 1",
            (item[0], ("delete/" if operation == "delete" else "exists/") + str(item[0]))).fetchall()
    record = {"kind": "cleanup", "case": manifest["case"],
              "request_id": manifest["request_id"], "action_id": item[1],
              "item_id": item[0], "operation": operation,
              "path": target["path"], "argv": list(__import__("sys").argv),
              "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
              "ticket": dict(ticket),
              "before": before, "stage": "entered",
              "modeled_unlink_count": 0, "modeled_observation_count": 0}
    _append(calls_path, record)
    if fault == "before":
        Path(manifest["marker"]).write_text(json.dumps({**record, **_process()}))
        while True:
            time.sleep(0.05)
    unlink_count = 0
    if operation == "delete":
        local.unlink()
        unlink_count = 1
    after = _tree(root)
    unknown = operation == "query" and manifest.get("query_unknown", False)
    state = "unknown" if unknown else ("1" if target["path"] in after else "0")
    frame = b"\0".join((b"CAMCTL-FILE/1", operation.encode(), target["path"].encode(),
        state.encode(), b"0" if operation == "delete" else b"-",
        b"1" if unknown else b"0", b"END", b""))
    record.update(stage="observed", after=after, modeled_unlink_count=unlink_count,
                  modeled_observation_count=1, frame_b64=base64.b64encode(frame).decode(),
                  frame_sha256=hashlib.sha256(frame).hexdigest(), frame_size=len(frame))
    _append(calls_path, record)
    if fault == "after":
        Path(manifest["marker"]).write_text(json.dumps({**record, **_process()}))
        while True:
            time.sleep(0.05)
    __import__("sys").stdout.buffer.write(frame)
    return 1 if unknown else 0
