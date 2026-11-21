from .base import InputBackend
from .null import NullBackend

__all__ = ["InputBackend", "NullBackend", "make_backend"]


def make_backend(kind: str = "auto") -> InputBackend:
    """kind: 'auto' (macOS on darwin, null elsewhere), 'macos', or 'null'."""
    import sys

    if kind == "null":
        return NullBackend()
    if kind == "macos" or (kind == "auto" and sys.platform == "darwin"):
        from .macos import MacOSBackend

        return MacOSBackend()
    if kind == "auto":
        return NullBackend()
    raise ValueError(f"unknown backend: {kind!r}")
