"""Native backend: an InputBackend over a RemotePlayClient.

This is where the old chain's layers 3–6 (HID posting, stream window,
plugin, re-sign) cease to exist (SCOPING §2): tool calls become fields
of a ControllerState handed directly to the streaming library by a
client we own, and every hand-off returns an InputAck or raises a
typed error.

key_down/key_up are supported for compatibility with the existing
Controller: keys are treated as DualSense button names and folded
into the current state. The Controller's keymap profiles remain a
translation layer above this; a native deployment maps tool buttons
to button names 1:1. Mouse methods raise UnsupportedStateError —
a console has no mouse, and a typed refusal beats a silent no-op.
"""

from __future__ import annotations

from typing import Optional

from ..client.base import RemotePlayClient
from ..client.errors import UnsupportedStateError
from ..client.state import ControllerState, InputAck
from .base import InputBackend


class NativeBackend(InputBackend):
    name = "native"

    def __init__(self, client: RemotePlayClient):
        self.client = client
        self.name = f"native-{client.name}"
        self._state = ControllerState.neutral()

    # -- state API ------------------------------------------------------
    def supports_state(self) -> bool:
        return True

    def set_state(self, state: ControllerState, why: str = "") -> InputAck:
        ack = self.client.send_state(state, why=why)
        self._state = state
        return ack

    def release_all(self) -> InputAck:
        return self.set_state(ControllerState.neutral(), why="release_all")

    # -- key compatibility ----------------------------------------------
    def key_down(self, key: str) -> None:
        self.set_state(
            self._state.with_buttons(add={key}), why=f"key_down {key}"
        )

    def key_up(self, key: str) -> None:
        self.set_state(
            self._state.with_buttons(remove={key}), why=f"key_up {key}"
        )

    # -- no mouse on a console -------------------------------------------
    def mouse_move(self, x: int, y: int) -> None:
        raise UnsupportedStateError("native console backend has no mouse")

    def mouse_down(self, button: str = "left") -> None:
        raise UnsupportedStateError("native console backend has no mouse")

    def mouse_up(self, button: str = "left") -> None:
        raise UnsupportedStateError("native console backend has no mouse")

    def trusted(self) -> Optional[bool]:
        """No OS input permission is involved; the session is the trust."""
        return True if self.client.connected else None
