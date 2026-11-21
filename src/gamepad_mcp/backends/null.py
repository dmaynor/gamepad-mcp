"""Null backend: records events instead of posting them.

Used for tests and for dry-running the server on any platform.
"""

from __future__ import annotations

import time
from typing import Optional

from .base import InputBackend


class NullBackend(InputBackend):
    name = "null"

    def __init__(self):
        self.events = []  # (monotonic_ts, kind, detail)

    def _record(self, kind, detail):
        self.events.append((time.monotonic(), kind, detail))

    def key_down(self, key: str) -> None:
        self._record("key_down", key)

    def key_up(self, key: str) -> None:
        self._record("key_up", key)

    def mouse_move(self, x: int, y: int) -> None:
        self._record("mouse_move", (x, y))

    def mouse_down(self, button: str = "left") -> None:
        self._record("mouse_down", button)

    def mouse_up(self, button: str = "left") -> None:
        self._record("mouse_up", button)

    def trusted(self) -> Optional[bool]:
        return True
