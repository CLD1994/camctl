"""记录 pytest 的实际开始、阶段完成和结束事件；不包裹被测函数。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

_OUT = Path(os.environ['CAMCTL_FIXED_PROGRESS'])

def _record(event, **fields):
    item = {'utc': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}
    with _OUT.open('a') as stream:
        stream.write(json.dumps(item, ensure_ascii=False)+'\n')

def pytest_runtest_logstart(nodeid, location):
    _record('node_started', nodeid=nodeid)

def pytest_runtest_logreport(report):
    _record('phase_completed', nodeid=report.nodeid, phase=report.when, outcome=report.outcome)

def pytest_runtest_logfinish(nodeid, location):
    _record('node_finished', nodeid=nodeid)

def pytest_sessionfinish(session, exitstatus):
    _record('session_finished', exit_code=int(exitstatus))
