"""A read-only, instance-dictionary-backed cached descriptor."""

from collections.abc import Callable
from typing import Self, overload, override


class CachedProperty[T, R](property):
    """Cache a computed value once per instance without allowing reassignment."""

    def __init__(self, function: Callable[[T], R]) -> None:
        super().__init__(function)
        self.function = function
        self.name = function.__name__

    def __set_name__(self, owner: type[T], name: str) -> None:
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type[T]) -> Self: ...

    @overload
    def __get__(self, instance: T, owner: type[T] | None = None) -> R: ...

    @override
    def __get__(self, instance: T | None, owner: type[T] | None = None) -> R | Self:
        if instance is None:
            return self
        try:
            cache = instance.__dict__
        except AttributeError as error:
            raise TypeError("cached_property requires a mutable __dict__") from error
        try:
            return cache[self.name]
        except KeyError:
            result = self.function(instance)
            try:
                cache[self.name] = result
            except (AttributeError, TypeError) as error:
                raise TypeError("cached_property requires a mutable __dict__") from error
            return result

    @override
    def __set__(self, instance: T, value: R) -> None:
        raise AttributeError(f"{self.name} is read-only")

    @override
    def __delete__(self, instance: T) -> None:
        raise AttributeError(f"{self.name} is read-only")


cached_property = CachedProperty
