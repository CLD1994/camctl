#!/bin/bash
set -o pipefail
cd /tmp/camctl-quality-repair-lidlmje7/action6
unset CAMCTL_TIMING_REPO CAMCTL_TIMING_DATA UV_PROJECT_ENVIRONMENT VIRTUAL_ENV PYTHONHOME
export PYTHONPATH=/tmp/camctl-quality-repair-lidlmje7/action6/apps/camctl/src:/tmp/camctl-quality-repair-lidlmje7/action6/apps/camctl/tests:/tmp/camctl-quality-repair-lidlmje7/action6/scripts:/tmp/camctl-quality-repair-lidlmje7/action6/tests/integration
export TMPDIR=/tmp/camctl-quality-repair-evidence/final-action6/native/tmp
export PYTHONPYCACHEPREFIX=/tmp/camctl-quality-repair-evidence/final-action6/native/pycache
export UV_PYTHON=/workspaces/camctl/apps/camctl/.venv311/bin/python
export UV_CACHE_DIR=/tmp/camctl-quality-repair-evidence/final-action6/uv-cache
export UV_LINK_MODE=copy
export npm_config_cache=/tmp/camctl-quality-repair-evidence/final-action6/npm-cache
export CAMCTL_INTEGRATION_PROGRESS=/tmp/camctl-quality-repair-evidence/final-action6/native
/workspaces/camctl/apps/camctl/.venv311/bin/python /tmp/camctl-quality-repair-evidence/final-action6/source_receipt.py native before
if [ "$?" -ne 0 ]; then exit 2; fi
/workspaces/camctl/apps/camctl/.venv311/bin/python scripts/run-memory-budget.py --evidence-dir /tmp/camctl-quality-repair-evidence/final-action6/native/budget --camctl-memory-limit 2GiB -- /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p camctl_memory_plugin -p integration_progress tests/integration/test_action6_segmented_recovery_roundtrip.py::test_installed_action6_native_required_photo_failure_keeps_reached_video_scope_and_waiters -vv -x --durations=10 -o cache_dir=/tmp/camctl-quality-repair-evidence/final-action6/native/pytest-cache --basetemp=/tmp/camctl-quality-repair-evidence/final-action6/native/data --junitxml=/tmp/camctl-quality-repair-evidence/final-action6/native/result.xml 2>&1 | tee /tmp/camctl-quality-repair-evidence/final-action6/native/pytest.log
result=${PIPESTATUS[0]}
printf '%s\n' "$result" > /tmp/camctl-quality-repair-evidence/final-action6/native/exit-code.txt
/workspaces/camctl/apps/camctl/.venv311/bin/python /tmp/camctl-quality-repair-evidence/final-action6/source_receipt.py native after
exit "$result"
