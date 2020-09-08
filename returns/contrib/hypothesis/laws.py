import inspect
import sys
from contextlib import contextmanager
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Type,
    TypeVar,
)

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.strategies._internal import types
from typing_extensions import final

from returns.interfaces.applicative import ApplicativeN
from returns.interfaces.specific import maybe, result
from returns.primitives.laws import Law, Lawful

_pyversion = sys.version_info[:2]


@final
class _Settings(NamedTuple):
    """Settings that we provide to an end user."""

    settings_kwargs: Dict[str, Any]
    use_init: bool


def check_all_laws(
    container_type: Type[Lawful],
    *,
    settings_kwargs: Optional[Dict[str, Any]] = None,
    use_init: bool = False,
) -> None:
    """
    Function to check all definied mathematical laws in a specified container.

    Should be used like so:

    .. code:: python

      from returns.contrib.hypothesis.laws import check_all_laws
      from returns.io import IO

      check_all_laws(IO)

    You can also pass different ``hypothesis`` settings inside:

    .. code:: python

      check_all_laws(IO, {'max_examples': 100})

    Note:
        Cannot be used inside doctests because of the magic we use inside.

    See: https://mmhaskell.com/blog/2017/3/13/obey-the-type-laws
    """
    settings = _Settings(
        settings_kwargs if settings_kwargs is not None else {},
        use_init,
    )

    for interface, laws in container_type.laws().items():
        for law in laws:
            _create_law_test_case(
                container_type,
                interface,
                law,
                settings=settings,
            )


@contextmanager
def container_strategies(
    container_type: Type[Lawful],
    *,
    settings: _Settings,
) -> Iterator[None]:
    """
    Registers all types inside a container to resolve to a correct strategy.

    For example, let's say we have ``Result`` type.
    It is a subtype of ``ContainerN``, ``MappableN``, ``BindableN``, etc.
    When we check this type, we need ``MappableN`` to resolve to ``Result``.

    Can be used independently from other functions.
    """
    our_interfaces = {
        base_type
        for base_type in container_type.__mro__
        if getattr(base_type, '__module__', '').startswith('returns.')
    }
    for interface in our_interfaces:
        st.register_type_strategy(
            interface,
            _create_container_factory(
                container_type,
                use_init=settings.use_init,
            ),
        )

    with maybe_register_container(container_type, use_init=settings.use_init):
        yield

    for interface in our_interfaces:
        types._global_type_lookup.pop(interface)  # noqa: WPS441


@contextmanager
def maybe_register_container(
    container_type: Type[Lawful],
    *,
    use_init: bool,
) -> Iterator[None]:
    """Temporary registeres a container if it is not registered yet."""
    unknown_container = container_type not in types._global_type_lookup
    if unknown_container:
        st.register_type_strategy(
            container_type,
            _create_container_factory(container_type, use_init=use_init),
        )

    yield

    if unknown_container:
        types._global_type_lookup.pop(container_type)  # noqa: WPS441


@contextmanager
def pure_functions() -> Iterator[None]:
    """
    Context manager to resolve all ``Callable`` as pure functions.

    It is not a default in ``hypothesis``.
    """
    def factory(thing) -> st.SearchStrategy:
        like = (lambda: None) if len(
            thing.__args__,
        ) == 1 else (lambda *args, **kwargs: None)

        return st.functions(
            like=like,
            returns=st.from_type(thing.__args__[-1]),
            pure=True,
        )

    used = types._global_type_lookup[Callable]  # type: ignore
    st.register_type_strategy(Callable, factory)  # type: ignore

    yield

    types._global_type_lookup.pop(Callable)  # type: ignore
    st.register_type_strategy(Callable, used)  # type: ignore


@contextmanager
def type_vars() -> Iterator[None]:
    """
    Our custom ``TypeVar`` handling.

    There are several noticable differences:

    1. We add mutable types to the tests: like ``list`` and ``dict``
    2. We ensure that values inside strategies are self-equal,
       for example, ``nan`` does not work for us

    """
    used = types._global_type_lookup[TypeVar]  # type: ignore

    def factory(thing):
        type_strategies = [
            types.resolve_TypeVar(thing),
            # TODO: add mutable strategies
        ]
        return st.one_of(type_strategies).filter(
            lambda inner: inner == inner,  # noqa: WPS312
        )

    st.register_type_strategy(TypeVar, factory)  # type: ignore

    yield

    types._global_type_lookup.pop(TypeVar)  # type: ignore
    st.register_type_strategy(TypeVar, used)  # type: ignore


def _create_container_factory(
    container_type: Type[Lawful],
    *,
    use_init: bool,
) -> Callable[[type], st.SearchStrategy]:
    """
    Creates a strategy from a container type.

    Basically, containers should not support ``__init__``
    even when they have one.
    Because, that can be very complex: for example ``FutureResult`` requires
    ``Awaitable[Result[a, b]]`` as an ``__init__`` value.

    But, custom containers pass ``use_init``
    if they are not an instance of ``ApplicativeN``
    and do not have a working ``.from_value`` method.

    For example, pure ``MappableN`` can do that.
    """
    def factory(type_: type) -> st.SearchStrategy:
        strategies: List[st.SearchStrategy[Any]] = []
        if use_init and getattr(container_type, '__init__', None):
            strategies.append(st.builds(container_type))
        if issubclass(container_type, ApplicativeN):
            strategies.append(st.builds(container_type.from_value))
        if issubclass(container_type, result.ResultLikeN):
            strategies.append(st.builds(container_type.from_failure))
        if issubclass(container_type, maybe.MaybeLikeN):
            strategies.append(st.builds(container_type.from_optional))
        return st.one_of(*strategies)
    return factory


def _run_law(
    container_type: Type[Lawful],
    law: Law,
    *,
    settings: _Settings,
) -> Callable[[st.DataObject], None]:
    def factory(source: st.DataObject) -> None:
        if _pyversion < (3, 7):  # pragma: no cover
            raise RuntimeError(
                'Hypothesis does not support several important ' +
                'typing features on python3.6, and earlier versions. ' +
                'Please update to at least python3.7',
            )

        with type_vars():
            with pure_functions():
                with container_strategies(container_type, settings=settings):
                    source.draw(st.builds(law.definition))
    return factory


def _create_law_test_case(
    container_type: Type[Lawful],
    interface: Type[Lawful],
    law: Law,
    *,
    settings: _Settings,
) -> None:
    test_function = given(st.data())(
        hypothesis_settings(**settings.settings_kwargs)(
            _run_law(container_type, law, settings=settings),
        ),
    )

    called_from = inspect.stack()[2]
    module = inspect.getmodule(called_from[0])

    template = 'test_{container}_{interface}_{name}'
    test_function.__name__ = template.format(  # noqa: WPS125
        container=container_type.__qualname__.lower(),
        interface=interface.__qualname__.lower(),
        name=law.name,
    )

    setattr(
        module,
        test_function.__name__,
        # We mark all tests with `returns_lawful` marker,
        # so users can easily skip them if needed.
        pytest.mark.returns_lawful(test_function),
    )