import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(repo / "apps/camctl/src"), str(repo / "apps/camctl/scripts"), str(repo)]
from sync_resources import sync_package_resources
sync_package_resources(repo / "apps/camctl")
from apps.camctl.tests.integration.history.test_record_segment_history import _large_fixture, _rows


async def main():
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="camctl-history-small-") as directory:
        owned, runtime, repository, ref, history = await _large_fixture(Path(directory), 4)
        prepared = time.perf_counter()
        try:
            worker = runtime.database.executor._thread
            assert worker is not None and not worker.is_alive()
            boundary = history.current_boundary()
            with history.open_segment_history(ref.action_id, boundary, batch_size=2) as handle:
                assert len(tuple(_rows(handle, "segment_members", ref))) == 4
                assert len(tuple(_rows(handle, "segment_gaps", ref))) == 2
                assert len(tuple(_rows(handle, "segment_scope_members", ref))) == 1
            print(json.dumps({"members": 4, "gaps": 2,
                              "preparation_seconds": prepared-started,
                              "checks_seconds": time.perf_counter()-prepared,
                              "worker_threads_closed": True}))
        finally:
            owned.connection.close()


asyncio.run(main())
