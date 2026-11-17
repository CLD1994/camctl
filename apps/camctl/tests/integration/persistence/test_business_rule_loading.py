"""完整业务规则装配不导入用于处理命令的大型仓储。"""

import json
import subprocess
import sys


def test_business_storage_loads_complete_rules_without_command_repositories():
    code = '''
import json
import sys
from camctl.history.events import load_event_registry
from camctl.persistence.repositories.validation import BusinessStorage, build_business_rules

BusinessStorage()
rules = build_business_rules()
registry = load_event_registry()
expected = {name for event in registry["events"].values()
            for branch in event["branches"].values() for name in branch["guards"]}
repositories = (
    "acceptance", "cancellation", "capture", "file_batches", "motor",
    "operations", "outputs", "scheduling", "segments", "timelapse",
)
loaded = [name for name in repositories
          if "camctl.persistence.repositories." + name in sys.modules]
print(json.dumps({"complete": set(rules) == expected,
                  "callable": all(callable(rule) for rule in rules.values()),
                  "loaded": loaded}))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"complete": True, "callable": True, "loaded": []}
