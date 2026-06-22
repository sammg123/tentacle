"""Auto-discovering source registry.

Imports every module in the ``tentacle.sources`` package, collects every concrete
``Source`` subclass, and exposes the enabled ones. New tentacles need no manual
wiring: drop a file in this package that defines a ``Source`` subclass and it is
picked up automatically. Key-gated sources stay out of the enabled list until
their env vars are set.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Optional

from ..storage import Storage
from ..util import get_logger
from . import base as _base
from .base import Source

log = get_logger("tentacle.registry")

# Modules to skip when scanning the package.
_SKIP = {"base", "registry", "__init__"}


def _discover_classes() -> list[type[Source]]:
    import tentacle.sources as pkg

    classes: list[type[Source]] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        name = mod_info.name
        if name in _SKIP:
            continue
        try:
            module = importlib.import_module(f"tentacle.sources.{name}")
        except Exception as exc:  # noqa: BLE001 (a broken tentacle must not kill discovery)
            log.warning("could not import source module %s: %s", name, exc)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Source)
                and obj is not Source
                and obj.__module__ == module.__name__
                and not inspect.isabstract(obj)
                and not obj.__name__.startswith("_")   # skip helper base classes
                and getattr(obj, "id", "base") not in ("", "base")  # must set a real id
            ):
                classes.append(obj)
    # stable order: tier, then id
    classes.sort(key=lambda c: (getattr(c, "tier", 9), getattr(c, "id", "")))
    return classes


def all_source_classes() -> list[type[Source]]:
    return _discover_classes()


def enabled_source_classes() -> list[type[Source]]:
    return [c for c in _discover_classes() if c.is_enabled()]


def build_sources(storage: Optional[Storage] = None) -> list[Source]:
    """Instantiate every ENABLED source."""
    out: list[Source] = []
    for cls in enabled_source_classes():
        try:
            out.append(cls(storage=storage))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not instantiate %s: %s", cls.__name__, exc)
    return out


def doctor() -> list[dict]:
    """Report which tentacles are live vs dormant and why."""
    report: list[dict] = []
    for cls in _discover_classes():
        missing = [k for k in cls.requires_keys if not __import__("os").environ.get(k)]
        report.append(
            {
                "id": cls.id,
                "family": cls.family,
                "tier": cls.tier,
                "enabled": cls.is_enabled(),
                "needs_keys": list(cls.requires_keys),
                "missing_keys": missing,
            }
        )
    report.sort(key=lambda r: (not r["enabled"], r["tier"], r["id"]))
    return report
