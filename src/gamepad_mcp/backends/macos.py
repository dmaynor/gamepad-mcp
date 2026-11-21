"""macOS backend: posts CGEvents at the HID event tap.

These are the same events a physical keyboard/mouse produce, so they reach
windows that have no Accessibility keyboard recipient. Posting events
requires the process to be Accessibility-trusted (System Settings ->
Privacy & Security -> Accessibility); trusted() reports that state.
"""

from __future__ import annotations

from typing import Optional

from ..keycodes import keycode
from .base import InputBackend


class MacOSBackend(InputBackend):
    name = "macos"

    def __init__(self):
        try:
            import Quartz
        except ImportError as exc:  # pragma: no cover - macOS only
            raise RuntimeError(
                "macOS backend needs pyobjc-framework-Quartz; "
                "install with: pip install 'gamepad-mcp[macos]'"
            ) from exc
        self._q = Quartz
        # An explicit HID-system event source is load-bearing: with a
        # None source, events created on a non-main thread (the MCP
        # server runs tool calls on worker threads) are silently
        # dropped by the window server, while the identical call from
        # a process's main thread works. Verified live 2026-11-21:
        # main-thread snippet typed, server-posted keys vanished.
        try:
            self._source = Quartz.CGEventSourceCreate(
                Quartz.kCGEventSourceStateHIDSystemState
            )
        except Exception:
            self._source = None

    def _post_key(self, key: str, down: bool) -> None:
        q = self._q
        event = q.CGEventCreateKeyboardEvent(self._source, keycode(key), down)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def key_down(self, key: str) -> None:
        self._post_key(key, True)

    def key_up(self, key: str) -> None:
        self._post_key(key, False)

    def _mouse_event(self, event_type, x: int, y: int, button) -> None:
        q = self._q
        event = q.CGEventCreateMouseEvent(self._source, event_type, (x, y), button)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def mouse_move(self, x: int, y: int) -> None:
        q = self._q
        self._mouse_event(q.kCGEventMouseMoved, x, y, q.kCGMouseButtonLeft)

    def mouse_down(self, button: str = "left") -> None:
        q = self._q
        if button == "right":
            self._mouse_event(q.kCGEventRightMouseDown, 0, 0, q.kCGMouseButtonRight)
        else:
            self._mouse_event(q.kCGEventLeftMouseDown, 0, 0, q.kCGMouseButtonLeft)

    def mouse_up(self, button: str = "left") -> None:
        q = self._q
        if button == "right":
            self._mouse_event(q.kCGEventRightMouseUp, 0, 0, q.kCGMouseButtonRight)
        else:
            self._mouse_event(q.kCGEventLeftMouseUp, 0, 0, q.kCGMouseButtonLeft)

    def trusted(self) -> Optional[bool]:
        try:
            import ApplicationServices

            return bool(ApplicationServices.AXIsProcessTrusted())
        except Exception:
            return None
