"""同一冻结输入，使用现有阶段计量，不开启cProfile或tracemalloc。"""
from pathlib import Path
from dataclasses import asdict
import hashlib,json,shutil,time
from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
from camctl.contracts.values import OperationKey
from camctl.history.decoding import decode_event_row
from integration.persistence import test_segment_transaction_batches as test
from camctl.logging_runtime import transaction_timing as timing

run=Path(__file__).resolve().parent
meta=json.loads((run/'metadata.json').read_text());path=Path(meta['database_path'])
owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
row=owned.connection.execute('SELECT operation_key,first_event_id FROM history_transactions ORDER BY id DESC LIMIT 1').fetchone()
key=OperationKey(row[0])
raw=owned.connection.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(row[1],)).fetchone()
request=test.transactions._header_request(decode_event_row(raw).evidence['segment_finish_request'])
owned.connection.close()
assert not Path(str(path)+'-wal').exists()
shutil.copy2(run/'frozen-state.db',path)
assert hashlib.sha256(path.read_bytes()).hexdigest()==meta['frozen_sha256']
owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
recorded=[];old=timing.record_performance
timing.record_performance=lambda stage,elapsed,**fields:recorded.append({'stage':stage,'wall_ns':elapsed,**fields})
measurement=timing._Measurement();token=timing._current.set(measurement)
try:
    saved=test.transactions.finish_segment_recording(request,key,owned,batch_size=meta['B'])
    assert saved.kind=='completed',saved.error
    measurement.emit()
finally:
    timing._current.reset(token);timing.record_performance=old
after=hashlib.sha256('\n'.join(owned.connection.iterdump()).encode()).hexdigest()
assert after==meta['result_logical_sha256']
started=time.perf_counter();verified=test.transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=meta['B']);verify_elapsed=time.perf_counter()-started
assert verified==saved.result
owned.connection.close()
(run/'write-stages-no-profile.json').write_text(json.dumps({'source_head':meta['source_head'],'N':meta['N'],'B':meta['B'],'stages':recorded,'result':asdict(saved.result),'after_logical_sha256':after,'verify_wall_s_without_stage_observation':verify_elapsed,'note':'只有现有事务阶段与SQL计量，无cProfile、无tracemalloc、无原测试MeasuredConnection。该观察本身仍有开销。'},ensure_ascii=False,indent=2,default=str)+'\n')
print(json.dumps(recorded,ensure_ascii=False))
