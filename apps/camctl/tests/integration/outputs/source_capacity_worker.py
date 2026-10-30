"""预算组内只执行公开来源仓储；准备、完整数据库断言和资源回收属于组外。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from integration.support.source_capacity import STAGES, result_document


def main(input_path, result_path):
    import camctl
    from camctl.contracts.values import OperationKey
    from camctl.outputs.sources import SourceSpec
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import register_acceptance_guards
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.outputs import (
        OutputsRepository, ResolveSources, register_outputs_guards,
    )
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    manifest = json.loads(input_path.read_text())
    assert manifest["version"] == 1
    for register in (register_acceptance_guards, register_capture_guards,
                     register_operation_guards, register_outputs_guards):
        register()
    raw = manifest["request"]
    request = ResolveSources(raw["action_id"], SourceSpec(**raw["spec"]), raw["occurred_at"])
    key = OperationKey(manifest["key"])
    repository = OutputsRepository(manifest["batch_size"])
    owned = open_existing(Path(manifest["database"]), DbOpenMode.EXISTING_RW, DbConfig())
    messages = []
    try:
        for stage in STAGES:
            started = time.perf_counter()
            result = repository.resolve_sources(request, key, owned)
            seconds = time.perf_counter() - started
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            message = {name: manifest[name] for name in
                       ("version", "database", "key", "request", "batch_size")}
            message.update(phase="result", stage=stage, kind=result.kind.value,
                           value=result_document(result.value), seconds=seconds)
            messages.append(message)
            print(json.dumps(message), flush=True)
            assert sys.stdin.readline().strip() == "continue", "缺少组外核对确认"
    finally:
        owned.connection.close()
    tracer = sys.modules.get("tracemalloc")
    result = {"phase": "closed", "messages": messages, "runtime": {
        "pid": os.getpid(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
        "camctl_file": str(Path(camctl.__file__).resolve()),
        "pytest_imported": "pytest" in sys.modules,
        "tracemalloc_active": tracer is not None and tracer.is_tracing(),
        "profile_active": sys.getprofile() is not None,
        "cgroup": Path("/proc/self/cgroup").read_text(),
    }}
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
