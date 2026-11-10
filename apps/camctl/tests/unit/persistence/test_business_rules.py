"""业务存储规则装配只读取正式定义，每份装配独立且不可变。"""

import pytest

from camctl.history import validators
from camctl.persistence.repositories.validation import build_business_rules


def test_formal_rules_ignore_shared_registration_state(monkeypatch):
    poisoned = lambda event, context: None
    monkeypatch.setitem(validators.NAMED_GUARDS, "window", poisoned)
    before = dict(validators.NAMED_GUARDS)
    rules = build_business_rules()
    assert rules["window"] is not poisoned
    assert rules["window"].__module__ == "camctl.persistence.repositories.scheduling"
    assert validators.NAMED_GUARDS == before


def test_rule_sets_have_independent_replacements():
    first = lambda event, context: None
    second = lambda event, context: None
    original = build_business_rules()
    left = build_business_rules({"window": first})
    right = build_business_rules({"window": second})
    assert left["window"] is first
    assert right["window"] is second
    assert original["window"] is not first and original["window"] is not second
    with pytest.raises(TypeError):
        left["window"] = second


def test_unknown_rule_override_is_rejected():
    with pytest.raises(ValueError, match="未登记"):
        build_business_rules({"not_registered": lambda event, context: None})
