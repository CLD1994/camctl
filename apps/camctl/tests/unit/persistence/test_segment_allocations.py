"""增长完成组必须在SQLite绑定前核对完整整数范围。"""
import pytest
from camctl.contracts.values import MAX_OBJECT_ID
from camctl.persistence.transaction import TransactionScope,TransactionError

@pytest.mark.parametrize('tx,event,count',[(MAX_OBJECT_ID,0,1),(0,MAX_OBJECT_ID,1),(0,MAX_OBJECT_ID-1,2)])
def test_complete_history_allocation_rejects_integer_exhaustion(tx,event,count):
    scope=TransactionScope(None,tx,event)
    with pytest.raises(TransactionError):scope.allocate(count)
    assert scope.allocation is None

def test_complete_history_allocation_preserves_exact_last_valid_integer():
    allocation=TransactionScope(None,MAX_OBJECT_ID-1,MAX_OBJECT_ID-2).allocate(2)
    assert allocation.txn_id==allocation.last_event_id==MAX_OBJECT_ID
