"""Input backend interface.

A backend turns abstract key/mouse actions into events on the host.
The macOS backend posts real HID events (the path a physical keyboard
takes), which is what makes game windows that expose no Accessibility
keyboard recipient — Chiaki's stream view is one — still playable.

State-based API (SCOPING §5, the one refactor of the remote-client
rebuild): native console backends (PlayStation via libchiaki, Xbox
via sidecar) take a complete ControllerState, not key events. The
state methods below are OPTIONAL — keymap backends inherit defaults
that answer honestly: supports_state() is False and set_state()
raises UnsupportedStateError (typed, never silent). The keymap path
stays for keyboard-driven games.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ..client.state import ControllerState, InputAck


class InputBackend:
    name = "base"

    def key_down(self, key: str) -> None:
        raise NotImplementedError

    def key_up(self, key: str) -> None:
        raise NotImplementedError

    def mouse_move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def mouse_down(self, button: str = "left") -> None:
        raise NotImplementedError

    def mouse_up(self, button: str = "left") -> None:
        raise NotImplementedError

    def trusted(self) -> Optional[bool]:
        """Whether the OS has granted this process input-posting permission.

        True/False when the platform can answer, None when it cannot.
        """
        return None

    # -- optional state-based API (native console backends) -----------
    def supports_state(self) -> bool:
        """Whether this backend takes complete controller states."""
        return False

    def set_state(self, state: "ControllerState", why: str = "") -> "InputAck":
        """Hand one complete controller state to the console.

        Returns the client's InputAck (send/ack timestamps, PRD §4.9).
        Keymap-only backends raise UnsupportedStateError — a typed,
        logged refusal, never a silent no-op (PRD §4.4).
        """
        from ..client.errors import UnsupportedStateError

        raise UnsupportedStateError(
            f"backend {self.name!r} does not implement state-based input"
        )
