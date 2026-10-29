#!/usr/bin/env bash
set -u
cd /tmp/camctl-failure-repair-20261026-4f5dfpqh/action6
export PYTHONPATH="$PWD/apps/camctl/src:$PWD/apps/camctl/tests:$PWD/scripts"
export TMPDIR=/tmp/camctl-failure-repair-evidence/action6/private/tmp
export PYTHONPYCACHEPREFIX=/tmp/camctl-failure-repair-evidence/action6/private/pycache
/workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/capture/test_preparation.py -q -k 'running_preparation_failure_publishes_real_no_result_conclusion or cold_saved_preparation_conclusion_finishes_before_new_settings' --basetemp=/tmp/camctl-failure-repair-evidence/action6/await-red/data --junitxml=/tmp/camctl-failure-repair-evidence/action6/await-red/result.xml > /tmp/camctl-failure-repair-evidence/action6/await-red/pytest.log 2>&1
result=$?
printf '%s\n' "$result" > /tmp/camctl-failure-repair-evidence/action6/await-red/exit-code
exit "$result"
