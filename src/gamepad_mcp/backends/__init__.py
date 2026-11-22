from .base import InputBackend
from .null import NullBackend

__all__ = ["InputBackend", "NullBackend", "make_backend"]


def make_backend(kind: str = "auto") -> InputBackend:
    """kind: 'auto' (macOS on darwin, null elsewhere), 'macos', 'null',
    or 'stub' (native state backend over the in-memory StubClient —
    for tests and dry runs; real console backends register their
    clients the same way, selected by configuration, PRD §4.7)."""
    import sys

    if kind == "null":
        return NullBackend()
    if kind == "stub":
        from ..client.stub import StubClient
        from .native import NativeBackend

        client = StubClient()
        client.connect()
        return NativeBackend(client)
    if kind == "macos" or (kind == "auto" and sys.platform == "darwin"):
        from .macos import MacOSBackend

        return MacOSBackend()
    if kind == "auto":
        return NullBackend()
    raise ValueError(f"unknown backend: {kind!r}")
