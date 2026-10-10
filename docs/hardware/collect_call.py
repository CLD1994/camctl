import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

directory = Path(sys.argv[1])
argv = sys.argv[2:]
if not argv:
    raise SystemExit("需要提供本次调用的完整 argv")
directory.mkdir(parents=True, exist_ok=False)
started_at = datetime.now(timezone.utc).isoformat()
started_ns = time.monotonic_ns()
with (directory / "stdout.bin").open("wb") as stdout, \
        (directory / "stderr.bin").open("wb") as stderr:
    result = subprocess.run(argv, stdout=stdout, stderr=stderr, check=False)
metadata = {
    "argv": argv,
    "started_at": started_at,
    "returned_at": datetime.now(timezone.utc).isoformat(),
    "elapsed_ns": time.monotonic_ns() - started_ns,
    "returncode": result.returncode,
}
(directory / "call.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(metadata, ensure_ascii=False))
