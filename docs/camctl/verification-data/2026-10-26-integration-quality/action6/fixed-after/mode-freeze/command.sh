#!/bin/bash
set -o pipefail
export PYTHONPATH=/tmp/camctl-quality-action6-20261026/apps/camctl/src:/tmp/camctl-quality-action6-20261026/apps/camctl/tests:/tmp/camctl-quality-action6-20261026/scripts:/tmp/camctl-quality-action6-20261026/tests/integration:/tmp/camctl-quality-evidence/action6
export TMPDIR=/tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/tmp
export PYTHONPYCACHEPREFIX=/tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/pycache
export UV_PYTHON=/workspaces/camctl/apps/camctl/.venv311/bin/python
export ACTION6_TIMING_LOG=/tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/client-commands.jsonl
/workspaces/camctl/apps/camctl/.venv311/bin/python scripts/run-memory-budget.py --evidence-dir /tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/budget -- /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p camctl_memory_plugin -p action6_timing tests/integration/test_action6_segmented_recovery_roundtrip.py::test_installed_video_modes_freeze_for_both_cameras_and_both_video_actions -vv --durations=10 --basetemp=/tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/data --junitxml=/tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/result.xml 2>&1 | tee /tmp/camctl-quality-evidence/action6/fixed-after/mode-freeze/pytest.log
