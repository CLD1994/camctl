#!/bin/bash
set -u
cd /tmp/camctl-quality-history-20261026
export PYTHONPATH=/tmp/camctl-quality-evidence/history/diagnostics:/tmp/camctl-quality-history-20261026/apps/camctl/src:/tmp/camctl-quality-history-20261026/apps/camctl/tests:/tmp/camctl-quality-history-20261026/scripts
export TMPDIR=/tmp/camctl-quality-evidence/history/tmp
export PYTHONPYCACHEPREFIX=/tmp/camctl-quality-evidence/history/pycache
export QUALITY_PHASE_FILE=/tmp/camctl-quality-evidence/history/bootstrap-phases.jsonl
/workspaces/camctl/apps/camctl/.venv311/bin/python3.11 -m pytest -p quality_phase_timer apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py::test_submit_during_initial_discovery_is_not_lost -q -s --durations=10 --basetemp=/tmp/camctl-quality-evidence/history/baseline-bootstrap --junitxml=/tmp/camctl-quality-evidence/history/baseline-bootstrap.xml > /tmp/camctl-quality-evidence/history/baseline-bootstrap.log 2>&1
bootstrap_status=$?
printf '%s\n' "$bootstrap_status" > /tmp/camctl-quality-evidence/history/baseline-bootstrap.exit
if [ "$bootstrap_status" -ne 0 ]; then
    exit "$bootstrap_status"
fi
export QUALITY_PHASE_FILE=/tmp/camctl-quality-evidence/history/history-phases.jsonl
/workspaces/camctl/apps/camctl/.venv311/bin/python3.11 -m pytest -p quality_phase_timer apps/camctl/tests/integration/history/test_report_selection.py::test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window apps/camctl/tests/integration/history/test_record_segment_history.py::test_large_growth_crosses_page_and_disk_cache_capacity -q -s --durations=10 --basetemp=/tmp/camctl-quality-evidence/history/diagnostic-history --junitxml=/tmp/camctl-quality-evidence/history/diagnostic-history.xml > /tmp/camctl-quality-evidence/history/diagnostic-history.log 2>&1
history_status=$?
printf '%s\n' "$history_status" > /tmp/camctl-quality-evidence/history/diagnostic-history.exit
exit "$history_status"
