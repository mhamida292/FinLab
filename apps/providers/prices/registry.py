from typing import Type

from .base import PriceProvider

_REGISTRY: dict[str, Type[PriceProvider]] = {}


def register(provider_cls: Type[PriceProvider]) -> Type[PriceProvider]:
    _REGISTRY[provider_cls.name] = provider_cls
    return provider_cls


def get(name: str = "yahoo_chart") -> PriceProvider:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown price provider: {name!r}. Registered: {sorted(_REGISTRY)}") from exc
    return cls()
