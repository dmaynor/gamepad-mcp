"""Controller: stateful key holds with safety caps, over any backend.

Every key-down is paired with a scheduled key-up. Holds are capped
(default 10 s, hard cap 30 s) so a crashed client or a lost connection
can never leave a key — and a game character — stuck down forever.
release_all() is the panic button and also runs on shutdown.
"""

from __future__ import annotations

import threading
import time

from .backends.base import InputBackend
from .keymaps import Profile


class Controller:
    def __init__(
        self,
        backend: InputBackend,
        profile: Profile,
        max_hold_ms: int = 10_000,
        hard_cap_ms: int = 30_000,
    ):
        self.backend = backend
        self.profile = profile
        self.max_hold_ms = max_hold_ms
        self.hard_cap_ms = hard_cap_ms
        self._held: dict = {}  # key name -> threading.Timer
        self._lock = threading.Lock()

    # -- low level ------------------------------------------------------
    def _cap(self, hold_ms: int) -> int:
        return max(0, min(int(hold_ms), self.max_hold_ms, self.hard_cap_ms))

    def key_down(self, key: str, auto_release_ms: int | None = None) -> None:
        with self._lock:
            if key in self._held:
                self._held[key].cancel()
            else:
                self.backend.key_down(key)
            cap = self._cap(
                auto_release_ms if auto_release_ms is not None else self.hard_cap_ms
            )
            timer = threading.Timer(cap / 1000.0, self._release, args=(key,))
            timer.daemon = True
            self._held[key] = timer
            timer.start()

    def _release(self, key: str) -> None:
        with self._lock:
            timer = self._held.pop(key, None)
            if timer is not None:
                timer.cancel()
                self.backend.key_up(key)

    def key_up(self, key: str) -> None:
        self._release(key)

    def press_key(self, key: str, hold_ms: int = 120) -> dict:
        hold = self._cap(hold_ms)
        self.key_down(key, auto_release_ms=hold)
        return {"key": key, "hold_ms": hold}

    def combo(self, keys: list, hold_ms: int = 120) -> dict:
        hold = self._cap(hold_ms)
        for key in keys:
            self.key_down(key, auto_release_ms=hold)
        return {"keys": list(keys), "hold_ms": hold}

    def release_all(self) -> dict:
        with self._lock:
            keys = list(self._held)
            for key in keys:
                self._held.pop(key).cancel()
                self.backend.key_up(key)
        return {"released": keys}

    def held(self) -> list:
        with self._lock:
            return sorted(self._held)

    # -- gamepad layer --------------------------------------------------
    def press_button(self, button: str, hold_ms: int = 120) -> dict:
        key = self.profile.button_key(button)
        result = self.press_key(key, hold_ms)
        return {"button": button, **result}

    def hold_stick(self, stick: str, direction: str, hold_ms: int = 1000) -> dict:
        if stick not in ("left", "right"):
            raise ValueError(f"stick must be 'left' or 'right', got {stick!r}")
        keys = self.profile.stick_keys(stick, direction)
        hold = self._cap(hold_ms)
        for key in keys:
            self.key_down(key, auto_release_ms=hold)
        return {"stick": stick, "direction": direction, "keys": keys, "hold_ms": hold}

    # -- mouse ----------------------------------------------------------
    def mouse_move(self, x: int, y: int) -> dict:
        self.backend.mouse_move(int(x), int(y))
        return {"x": int(x), "y": int(y)}

    def mouse_click(self, button: str = "left", hold_ms: int = 80) -> dict:
        hold = self._cap(hold_ms)
        self.backend.mouse_down(button)
        if hold:
            time.sleep(hold / 1000.0)
        self.backend.mouse_up(button)
        return {"button": button, "hold_ms": hold}

    def status(self) -> dict:
        return {
            "backend": self.backend.name,
            "trusted": self.backend.trusted(),
            "profile": self.profile.name,
            "held": self.held(),
            "max_hold_ms": self.max_hold_ms,
            "hard_cap_ms": self.hard_cap_ms,
        }
