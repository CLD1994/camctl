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


class ConfiguredRepository:
    def __init__(self, limit):
        self.limit, self.saved = limit, []

    def save(self, value):
        self.saved.append(value)
        return self.read(value)

    def read(self, value):
        return (self.limit, value)


class ConfiguredObserver(ConfiguredRepository):
    def __init__(self):
        super().__init__(999)
        self.calls = []

    def save(self, value):
        self.calls.append(("save", value))
        return super().save(value)

    def read(self, value):
        self.calls.append(("read", value))
        return super().read(value)


def test_delegation_preserves_each_actual_instance_configuration_and_state():
    first, second, observer = ConfiguredRepository(4), ConfiguredRepository(32), ConfiguredObserver()
    with repository_port(ConfiguredRepository, observer, ("save",)):
        assert first.save("first") == (4, "first")
        assert second.save("second") == (32, "second")
    assert first.saved == ["first"]
    assert second.saved == ["second"]
    assert observer.saved == []
    assert observer.calls == [("save", "first"), ("save", "second")]


def test_explicit_delegate_also_preserves_the_intercepted_instance():
    class Delegating:
        delegate = ConfiguredRepository(999)

        def save(self, value):
            return self.delegate.save(value)

    actual, observer = ConfiguredRepository(4), Delegating()
    with repository_port(ConfiguredRepository, observer, ("save",)):
        assert actual.save("original") == (4, "original")
    assert actual.saved == ["original"]
    assert observer.delegate.saved == []


def test_original_method_calling_another_selected_method_is_observed():
    actual, observer = ConfiguredRepository(4), ConfiguredObserver()
    with repository_port(ConfiguredRepository, observer, ("save", "read")):
        assert actual.save("original") == (4, "original")
    assert observer.calls == [("save", "original"), ("read", "original")]


def test_observer_calling_another_selected_method_uses_original_instance():
    class ReadingObserver(ConfiguredObserver):
        def save(self, value):
            return ConfiguredRepository.read(self, value)

    actual, observer = ConfiguredRepository(4), ReadingObserver()
    with repository_port(ConfiguredRepository, observer, ("save", "read")):
        assert actual.save("original") == (4, "original")
    assert observer.calls == [("read", "original")]
    assert actual.saved == []


def test_other_selected_method_still_checks_its_match():
    actual, observer, checked = ConfiguredRepository(4), ConfiguredObserver(), []

    def match(name, args, kwargs):
        checked.append((name, args, kwargs))
        return name == "save"

    with repository_port(ConfiguredRepository, observer, ("save", "read"), matches=match):
        assert actual.save("original") == (4, "original")
    assert checked == [("save", ("original",), {}), ("read", ("original",), {})]
    assert observer.calls == [("save", "original")]


def test_nested_observers_preserve_actual_state_and_configuration():
    actual = ConfiguredRepository(4)
    outer, inner = ConfiguredObserver(), ConfiguredObserver()
    with repository_port(ConfiguredRepository, outer, ("save", "read")):
        with repository_port(ConfiguredRepository, inner, ("save", "read")):
            assert actual.save("inner") == (4, "inner")
        assert actual.save("outer") == (4, "outer")
    assert inner.calls == [("save", "inner"), ("read", "inner")]
    assert outer.calls == [("save", "outer"), ("read", "outer")]
    assert actual.saved == ["inner", "outer"]
    assert outer.saved == inner.saved == []


def test_delegated_original_error_uses_actual_instance_and_restores_context():
    error = RuntimeError("actual repository error")

    class RejectingRepository:
        def __init__(self, result):
            self.result = result

        def save(self, value):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class Observing(RejectingRepository):
        def save(self, value):
            return super().save(value)

    with repository_port(RejectingRepository, Observing("observer"), ("save",)):
        with pytest.raises(RuntimeError) as caught:
            RejectingRepository(error).save("first")
        assert caught.value is error
        assert RejectingRepository("actual").save("second") == "actual"


def test_async_methods_are_rejected_before_any_method_is_patched():
    class AsyncRepository(Repository):
        async def read(self):
            pass

    class AsyncObserver(Observer):
        async def read(self):
            pass

    original = AsyncRepository.save
    with pytest.raises(TypeError, match="同步"):
        with repository_port(AsyncRepository, AsyncObserver(), ("save", "read")):
            raise AssertionError("异步方法不能进入作用域")
    assert AsyncRepository.save is original
