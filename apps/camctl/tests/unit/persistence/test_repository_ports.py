"""验证故障注入边界的委派、身份限定和作用域恢复。"""

import pytest

from support.repository_ports import repository_port


class Repository:
    def save(self, request, *, key):
        return ("saved", request, key)


class Observer(Repository):
    def __init__(self, label="observed"):
        self.label, self.calls = label, []

    def save(self, request, *, key):
        self.calls.append((request, key))
        return (self.label, super().save(request, key=key))


def test_subclass_delegates_once_to_original_repository():
    observer = Observer()
    with repository_port(Repository, observer, ("save",)):
        assert Repository().save(7, key="original") == (
            "observed", ("saved", 7, "original"))
    assert observer.calls == [(7, "original")]
    assert Repository().save(8, key="later") == ("saved", 8, "later")


def test_only_the_selected_request_is_intercepted():
    observer = Observer()
    with repository_port(Repository, observer, ("save",),
                         matches=lambda _name, args, _kwargs: args == (7,)):
        assert Repository().save(8, key="other") == ("saved", 8, "other")
        assert Repository().save(7, key="target")[0] == "observed"
    assert observer.calls == [(7, "target")]


def test_nested_scopes_restore_the_previous_observer():
    first, second = Observer("first"), Observer("second")
    with repository_port(Repository, first, ("save",)):
        with repository_port(Repository, second, ("save",)):
            assert Repository().save(1, key="inner")[0] == "second"
        assert Repository().save(2, key="outer")[0] == "first"
    assert first.calls == [(2, "outer")]
    assert second.calls == [(1, "inner")]


def test_unmatched_inner_scope_can_use_the_outer_observer():
    first, second = Observer("first"), Observer("second")
    with repository_port(Repository, first, ("save",)):
        with repository_port(Repository, second, ("save",),
                             matches=lambda _name, args, _kwargs: args == (2,)):
            assert Repository().save(1, key="outer")[0] == "first"
            assert Repository().save(2, key="inner")[0] == "second"
    assert first.calls == [(1, "outer")]
    assert second.calls == [(2, "inner")]


def test_explicit_delegate_uses_the_original_method():
    class Delegating:
        def __init__(self):
            self.delegate = Repository()

        def save(self, request, *, key):
            return ("delegated", self.delegate.save(request, key=key))

    with repository_port(Repository, Delegating(), ("save",)):
        assert Repository().save(3, key="same") == (
            "delegated", ("saved", 3, "same"))


def test_original_exception_and_method_are_preserved_after_exit():
    original_method = Repository.save
    error = RuntimeError("controlled failure")

    class Failing:
        def save(self, request, *, key):
            raise error

    with pytest.raises(RuntimeError) as caught:
        with repository_port(Repository, Failing(), ("save",)):
            Repository().save(1, key="first")
    assert caught.value is error
    assert Repository.save is original_method
    with repository_port(Repository, Observer(), ("save",)):
        assert Repository().save(2, key="second")[0] == "observed"


@pytest.mark.parametrize("methods", [(), ("save", "save"), ("_private",)])
def test_invalid_method_selection_does_not_change_the_repository(methods):
    original = Repository.save
    with pytest.raises(ValueError):
        with repository_port(Repository, Observer(), methods):
            raise AssertionError("非法选择不能进入作用域")
    assert Repository.save is original
