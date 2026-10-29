import json
import os
from pathlib import Path
import time

_PATH = Path(os.environ['BOOTSTRAP_PROGRESS_PATH'])


def _write(**entry):
    with _PATH.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'monotonic': time.monotonic(), **entry}, ensure_ascii=False) + '\n')


def pytest_runtest_logstart(nodeid, location):
    _write(event='start', nodeid=nodeid)


def pytest_runtest_logreport(report):
    _write(event='report', nodeid=report.nodeid, phase=report.when, outcome=report.outcome, duration=report.duration)
