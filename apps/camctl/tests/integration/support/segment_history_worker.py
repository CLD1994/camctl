"""在独立受限进程完整读取分页与快照，逐块交给组外核对。"""
import json
import sys
from pathlib import Path

from camctl.capture.segment_models import SegmentSetRef
from camctl.history.snapshots import prepare_snapshot
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import encode_json_value
from camctl.contracts.history_values import HistoryBoundary


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    directory = Path(sys.argv[2])
    ref = SegmentSetRef(*(manifest[name] for name in ('action_id','scan_id','final_page_event_id','version')))
    boundary = HistoryBoundary(**manifest['boundary'])
    history = HistoryRepository(directory / 'state.db')
    diagnostics = len(sys.argv)>3 and sys.argv[3]=='--diagnose'
    measurements = {}
    if diagnostics:
        import tracemalloc
        tracemalloc.start()
    def sample(label):
        if diagnostics:
            current, peak = tracemalloc.get_traced_memory()
            measurements[label] = {'current':current,'peak':peak}
    counts = {}
    with history.open_segment_history(ref.action_id,boundary,batch_size=7) as handle:
        sample('opened')
        with (directory / 'rows.jsonl').open('w') as output:
            for table in ('segment_members','segment_gaps'):
                cursor = None
                count = 0
                while True:
                    page = handle.read_page(table,ref=ref,cursor=cursor,limit=7)
                    assert len(page.items) <= 7
                    for row in page.items:
                        output.write(encode_json_value({'table':table,'row':dict(row.values)})+'\n')
                        count += 1
                        if table=='segment_members' and count in (256,2048):
                            sample(f'members_{count}')
                    cursor=page.next_cursor
                    if cursor is None:break
                counts[table]=count
        prepared=prepare_snapshot(handle)
        sample('snapshot_prepared')
        try:
            length=0
            with (directory/'snapshot.jsonl').open('wb') as output:
                for chunk in prepared.chunks:
                    assert len(chunk)<=65536
                    output.write(chunk)
                    length+=len(chunk)
            assert length==prepared.length
            sample('snapshot_consumed')
        finally:prepared.close()
    sample('closed')
    (directory/'result.json').write_text(json.dumps({'counts':counts,'snapshot_length':length,
        'diagnostics_enabled':diagnostics,'diagnostics':measurements}))


if __name__ == '__main__':
    main()
