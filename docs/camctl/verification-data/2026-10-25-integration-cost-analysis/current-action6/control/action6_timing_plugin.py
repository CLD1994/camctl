from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import time

_ROOT = Path(__file__).resolve().parents[1]
_LOG = _ROOT / "helper-events.jsonl"
_SERIAL = 0
_PARENT = []
_CASES = []


def _emit(record):
    with _LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _wrap(owner, name, label=None):
    original = getattr(owner, name)

    @functools.wraps(original)
    def timed(*args, **kwargs):
        global _SERIAL
        _SERIAL += 1
        identity = _SERIAL
        parent = _PARENT[-1] if _PARENT else None
        start = time.perf_counter_ns()
        details = {}
        if name == "run_client_driver":
            details["command"] = [str(arg) for arg in args[1:]]
        elif name == "_run":
            details["command"] = [str(arg) for arg in args[0]]
        elif name == "wait_for":
            details["message"] = str(kwargs.get("message", args[2] if len(args) > 2 else ""))
            details["budget_seconds"] = kwargs.get("timeout_s", args[1] if len(args) > 1 else None)
        _emit({"event": "start", "id": identity, "parent": parent, "helper": label or name,
               "monotonic_ns": start, "utc_ns": time.time_ns(), **details})
        _PARENT.append(identity)
        outcome = "passed"
        try:
            return original(*args, **kwargs)
        except BaseException as error:
            outcome = type(error).__name__
            raise
        finally:
            end = time.perf_counter_ns()
            _PARENT.pop()
            _emit({"event": "finish", "id": identity, "parent": parent, "helper": label or name,
                   "monotonic_ns": end, "utc_ns": time.time_ns(),
                   "elapsed_seconds": (end - start) / 1e9, "outcome": outcome, **details})

    setattr(owner, name, timed)


def pytest_collection_modifyitems(session, config, items):
    import test_action6_segmented_recovery_roundtrip as module
    import test_action6_builtin_demo_roundtrip as package_module
    from camctl_fixtures import Deployment
    from _wsl_host_demo import HostDemo
    for name in ("_prepare_installed_action6", "create_segmented_media_manifest", "_run",
                 "_start_installed_capture", "_submit_obtain", "_wait_for_waiting_obtains",
                 "_release_clock", "wait_for", "_save_report_snapshot", "_assert_client_delivery_files",
                 "_cleanup_inspection_inputs", "_assert_ack"):
        _wrap(module, name)
    for name in ("run_client_driver", "export_plan_with_client", "import_reports_with_client"):
        _wrap(Deployment, name, "Deployment." + name)
    for name in ("start", "submit", "claim", "stop"):
        _wrap(HostDemo, name, "HostDemo." + name)
    _wrap(package_module, "_run", "action6_package._run")
    _emit({"event": "collection", "nodeids": [item.nodeid for item in items],
           "pid": os.getpid(), "utc_ns": time.time_ns()})


def pytest_runtest_logreport(report):
    _CASES.append({"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
                   "duration_seconds": report.duration})
    (_ROOT / "pytest-phases.json").write_text(json.dumps(_CASES, indent=2) + "\n")


def pytest_sessionfinish(session, exitstatus):
    _emit({"event": "sessionfinish", "exit_code": int(exitstatus), "utc_ns": time.time_ns()})
