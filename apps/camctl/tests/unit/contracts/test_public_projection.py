"""公开片段比较只依赖已产生的结构化值。"""
from decimal import Decimal
from camctl.contracts.public_projection import public_changed


def test_identical_public_fragment_has_no_change():
    assert public_changed({"status":"pending"}, {"status":"pending"}) is False


def test_field_value_change_is_visible():
    assert public_changed({"status":"pending"}, {"status":"running"}) is True


def test_field_appearance_change_is_visible():
    assert public_changed({"status":"pending"}, {"status":"pending","extra":True}) is True


def test_decimal_comparison_is_exact():
    assert public_changed({"v":Decimal("1.0")}, {"v":Decimal("1.00")}) is False
