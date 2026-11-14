"""Adapter registry. Adding a tool = one module + one line in `_registry.py`.

The registry is built on first use, not on import. The hook commands (`auto`, `nudge`) run on every
prompt and need only a path from one adapter (`claude_paths`); they must not load all nine.
"""
__all__ = ["REGISTRY", "implemented_check_ids"]


def __getattr__(name: str):
    if name in __all__:
        from . import _registry
        value = getattr(_registry, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
