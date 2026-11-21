"""Input backend interface.

A backend turns abstract key/mouse actions into events on the host.
The macOS backend posts real HID events (the path a physical keyboard
takes), which is what makes game windows that expose no Accessibility
keyboard recipient — Chiaki's stream view is one — still playable.
"""

from __future__ import annotations

from typing import Optional


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
