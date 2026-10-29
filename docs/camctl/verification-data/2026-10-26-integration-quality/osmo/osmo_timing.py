import json
import os
from pathlib import Path
import sys
import time

_RECORDS = []
_PHASES = []
_PATH = Path(os.environ['OSMO_TIMING_FILE'])

def _save():
    _PATH.write_text(json.dumps({'commands': _RECORDS, 'pytest_phases': _PHASES}, indent=2) + '\n')

def pytest_collection_modifyitems(session, config, items):
    module = sys.modules['test_osmo_builtin_timelapse_roundtrip']
    original = module._run
    def measured(command, **kwargs):
        record = {'argv': [str(x) for x in command], 'cwd': str(kwargs['cwd']), 'started_ns': time.monotonic_ns()}
        _RECORDS.append(record)
        try:
            result = original(command, **kwargs)
            record['outcome'] = 'passed'
            return result
        except BaseException as error:
            record['outcome'] = type(error).__name__
            raise
        finally:
            record['elapsed_seconds'] = (time.monotonic_ns() - record['started_ns']) / 1e9
            _save()
    module._run = measured
    sys.modules['test_osmo_builtin_record_segmentation_roundtrip']._run = measured

def pytest_runtest_logreport(report):
    _PHASES.append({'nodeid': report.nodeid, 'when': report.when, 'duration': report.duration, 'outcome': report.outcome})
    _save()
