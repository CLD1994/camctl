import sys,sqlite3,traceback,json
from pathlib import Path
source=Path('/tmp/camctl-itopt-final-20261025/tasks/040-root-test_camctl_media_roundtrip-shard-1/source')
sys.path.insert(0,str(source/'apps/camctl/src'))
from camctl.capture.models import ActivityObservationSave
from camctl.operations.attempts import AttemptFinish
from camctl.operations.models import AttemptStatus,AttemptTicket,CallOutcome,EffectState,ErrorValue,EvidenceValue,Settlement,SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.devices.evidence import EvidenceRegistry,EvidenceContract
from camctl.persistence.repositories.capture import _FinishStartResultCommand
from camctl.persistence.repositories.scheduling import ExpireActionRequest
from camctl.persistence.transaction import TransactionScope
from camctl.contracts.values import OperationKey
p=source.parent/'tmp/pytest/test_excess_recording_repairs_0/deployment/state.db'
c=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True)
ticket=AttemptTicket(attempt_id=1,operation='control',target_id='1',responsibility_key='start/1',run_id=1)
outcome=CallOutcome(status=AttemptStatus.FAILED,effect=EffectState.NO_EFFECT,error=ErrorValue('window_ended','dispatch'),settlement=Settlement(SettlementBasis.NOT_DISPATCHED,EvidenceValue('dispatch_prevented',1,{})))
validated=validate_outcome(ticket,outcome,EvidenceRegistry((EvidenceContract('dispatch_prevented',1,'control',frozenset()),)))
now=1792886764017084
finish=AttemptFinish(ticket,validated,now)
observation=ActivityObservationSave(1,now,dispatch_state=1)
command=_FinishStartResultCommand(finish,observation,None,None,ExpireActionRequest(1,now,now),OperationKey('77777777777777777777777777777777'))
scope=TransactionScope(c,c.execute('SELECT MAX(id) FROM history_transactions').fetchone()[0],c.execute('SELECT MAX(id) FROM history_events').fetchone()[0])
try:
 p=command.plan(scope)
 print('PLAN_OK',len(p.events))
except Exception:
 traceback.print_exc()
finally:
 print('connection_in_transaction',c.in_transaction)
 c.close()
# 原始数据库只读，以下事务只作用于独立副本。
from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
from camctl.persistence.repositories.capture import CaptureRepository
original=source.parent/'tmp/pytest/test_excess_recording_repairs_0/deployment/state.db'
copy=Path('/tmp/camctl-diag-clock-original30-copy.db')
if copy.exists():copy.unlink()
ro=sqlite3.connect('file:'+str(original)+'?mode=ro',uri=True)
dst=sqlite3.connect(copy);ro.backup(dst);dst.close();ro.close()
owned=open_existing(copy,DbOpenMode.EXISTING_RW,DbConfig())
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.scheduling import register_window_guard
register_operation_guards();register_capture_guards();register_window_guard()
receipt=CaptureRepository().finish_start_result(finish,observation,OperationKey('77777777777777777777777777777777'),owned,expiration=ExpireActionRequest(1,now,now))
print('COPY_RECEIPT',receipt.kind,repr(receipt.error))
if receipt.error:
 traceback.print_exception(receipt.error)
print('COPY_ACTIONS',owned.connection.execute('SELECT status FROM actions ORDER BY id').fetchall())
owned.connection.close()
