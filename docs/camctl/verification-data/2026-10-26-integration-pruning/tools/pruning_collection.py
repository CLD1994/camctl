import json
import os
from pathlib import Path


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['CAMCTL_PRUNING_COLLECTION_RECEIPT']).write_text(json.dumps({
        'exit_code':int(exitstatus),
        'nodeids':[item.nodeid for item in session.items],
        'collection_failures':session.testsfailed,
    },ensure_ascii=False,indent=2)+'\n')
