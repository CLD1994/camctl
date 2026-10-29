import json, os, time
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def client_timing(monkeypatch):
    from camctl_fixtures import Deployment
    original = Deployment.run_client_driver
    def measured(self, *args, output):
        started = time.monotonic()
        record = {'operation': args[0], 'world': str(self.root), 'output': str(output)}
        try:
            result = original(self, *args, output=output)
            record['outcome'] = 'passed'
            return result
        except BaseException:
            record['outcome'] = 'failed'
            raise
        finally:
            record['elapsed_seconds'] = time.monotonic() - started
            with Path(os.environ['ACTION6_TIMING_LOG']).open('a') as stream:
                stream.write(json.dumps(record) + '\n')
    monkeypatch.setattr(Deployment, 'run_client_driver', measured)
