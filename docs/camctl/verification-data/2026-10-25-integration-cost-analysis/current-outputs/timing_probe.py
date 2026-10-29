import functools, gc, json, os, sqlite3, sys, time
from pathlib import Path
import pytest

_STATE = {"phase": "outside", "calls": {}, "pytest_reports": []}
_REAL_TIME = time.perf_counter


def _wrap(label, function):
    @functools.wraps(function)
    def measured(*args, **kwargs):
        started = _REAL_TIME()
        try:
            return function(*args, **kwargs)
        finally:
            duration = _REAL_TIME() - started
            phase = _STATE["phase"]
            bucket = _STATE["calls"].setdefault(label, {}).setdefault(phase, {"count": 0, "seconds": 0.0, "max_seconds": 0.0, "durations": []})
            bucket["count"] += 1
            bucket["seconds"] += duration
            bucket["max_seconds"] = max(bucket["max_seconds"], duration)
            bucket["durations"].append(duration)
    return measured


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from camctl.persistence.repositories.outputs import OutputsRepository
    originals = [(gc, "collect", gc.collect), (ReadDiscoveryOwner, "reads", ReadDiscoveryOwner.reads), (OutputsRepository, "grant_file", OutputsRepository.grant_file)]
    for owner, name, original in originals:
        setattr(owner, name, _wrap(owner.__name__ + "." + name, original))
    try:
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    _STATE["phase"] = "setup"
    yield
    _STATE["phase"] = "outside"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    _STATE["phase"] = "call"
    yield
    _STATE["phase"] = "outside"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    _STATE["phase"] = "teardown"
    yield
    _STATE["phase"] = "outside"


def pytest_runtest_logreport(report):
    _STATE["pytest_reports"].append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome, "seconds": report.duration})


def pytest_sessionfinish(session, exitstatus):
    import camctl
    _STATE.update({"exitstatus": int(exitstatus), "python": sys.version, "executable": sys.executable, "sqlite": sqlite3.sqlite_version, "camctl_file": camctl.__file__, "cwd": os.getcwd(), "pytest_version": pytest.__version__, "notes": ["Wrapper durations are inclusive; do not sum nested work.", "Only one existing test node was executed; no production or test sources were edited."]})
    Path(os.environ["CAMCTL_EXTERNAL_PROBE_RESULT"]).write_text(json.dumps(_STATE, indent=2))
