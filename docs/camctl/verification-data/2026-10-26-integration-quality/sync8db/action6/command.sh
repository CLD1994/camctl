#!/bin/bash
set -o pipefail
export PYTHONPATH=/tmp/camctl-sync8db-tests-ojv22ku3/action6/apps/camctl/src:/tmp/camctl-sync8db-tests-ojv22ku3/action6/apps/camctl/tests:/tmp/camctl-sync8db-tests-ojv22ku3/action6/scripts:/tmp/camctl-sync8db-tests-ojv22ku3/action6/tests/integration
export TMPDIR=/tmp/camctl-sync8db-evidence/action6/native/tmp
export PYTHONPYCACHEPREFIX=/tmp/camctl-sync8db-evidence/action6/native/pycache
export UV_PYTHON=/workspaces/camctl/apps/camctl/.venv311/bin/python
export UV_CACHE_DIR=/tmp/camctl-sync8db-evidence/action6/uv-cache
/workspaces/camctl/apps/camctl/.venv311/bin/python scripts/run-memory-budget.py --evidence-dir /tmp/camctl-sync8db-evidence/action6/native/budget -- /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p camctl_memory_plugin tests/integration/test_action6_segmented_recovery_roundtrip.py::test_installed_action6_native_required_photo_failure_keeps_reached_video_scope_and_waiters -vv --durations=10 --basetemp=/tmp/camctl-sync8db-evidence/action6/native/data --junitxml=/tmp/camctl-sync8db-evidence/action6/native/result.xml 2>&1 | tee /tmp/camctl-sync8db-evidence/action6/native/pytest.log
result=${PIPESTATUS[0]}
printf '%s\n' "$result" > /tmp/camctl-sync8db-evidence/action6/native/exit-code.txt
exit "$result"
