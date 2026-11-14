"""事务回执属性分类。"""

import camctl.persistence.repositories.transactions as transaction_module

def test_write_receipt_accessor_rejects_untyped_attribute() -> None:
    error = RuntimeError("ordinary business failure")
    setattr(error, "_camctl_write_receipt", object())
    accessor = getattr(transaction_module, "write_receipt_for", None)
    assert callable(accessor)
    assert accessor(error) is None
