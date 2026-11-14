"""Provider lookup. Adapters register a factory; instances are built on first use."""
from __future__ import annotations

import importlib
import os
from typing import Callable

from .base import Provider

ORDER = ["abliteration", "anthropic", "openai", "google", "xai", "ollama", "codex", "claude_code"]
# name -> (module, factory attribute). A module that does not exist yet is skipped.
_SPECS: dict[str, tuple[str, str]] = {
    "abliteration": ("openai_compat", "abliteration"),
    "openai": ("openai_compat", "openai"),
    "xai": ("openai_compat", "xai"),
    "anthropic": ("anthropic", "make"),
    "google": ("google", "make"),
    "ollama": ("ollama", "make"),
    "codex": ("codex", "make"),
    "claude_code": ("claude_code", "make"),
    "mock": ("mock", "MockProvider"),
}
_instances: dict[str, Provider] = {}
_overrides: dict[str, Provider] = {}


def _build(name: str) -> Provider | None:
    if name in _overrides:
        return _overrides[name]
    if name not in _instances:
        mod_name, attr = _SPECS[name]
        try:
            mod = importlib.import_module(f"{__package__}.{mod_name}")
        except ModuleNotFoundError as e:
            if e.name == f"{__package__}.{mod_name}":
                return None
            raise
        factory: Callable[[], Provider] = getattr(mod, attr)
        _instances[name] = factory()
    return _instances[name]


def _names() -> list[str]:
    return ORDER + (["mock"] if os.environ.get("HUNTERAI_MOCK") == "1" else [])


def all() -> list[Provider]:  # noqa: A001 - registry API name
    return [p for p in (_build(n) for n in _names()) if p is not None]


def get(name: str) -> Provider:
    if name in _overrides:
        return _overrides[name]
    if name not in _names():
        raise KeyError(name)
    p = _build(name)
    if p is None:
        raise KeyError(name)
    return p


def override(name: str, provider: Provider | None) -> None:
    """Swap a provider instance (tests). Pass None to remove the override."""
    if provider is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = provider
