from .base import InputBackend
from .null import NullBackend

__all__ = ["InputBackend", "NullBackend", "make_backend"]


def make_backend(kind: str = "auto") -> InputBackend:
    """kind: 'auto' (macOS on darwin, null elsewhere), 'macos', 'null',
    'stub' (native state backend over the in-memory StubClient —
    for tests and dry runs), or 'ps' (native state backend over the
    real PlayStation client, PsClient on libchiaki). Real console
    backends register their clients the same way, selected by
    configuration, PRD §4.7.

    'ps' is constructed WITHOUT connecting: session ownership is
    lazy (see backends/native.py) — the first state hand-off
    connects, so a server on this backend starts and serves while
    the console refuses sessions, and the refusal surfaces typed on
    the first tool call."""
    import sys

    if kind == "null":
        return NullBackend()
    if kind == "stub":
        from ..client.stub import StubClient
        from .native import NativeBackend

        client = StubClient()
        client.connect()
        return NativeBackend(client)
    if kind == "ps":
        from ..client.ps import PsClient
        from .native import NativeBackend

        return NativeBackend(PsClient())
    if kind == "macos" or (kind == "auto" and sys.platform == "darwin"):
        from .macos import MacOSBackend

        return MacOSBackend()
    if kind == "auto":
        return NullBackend()
    raise ValueError(f"unknown backend: {kind!r}")
