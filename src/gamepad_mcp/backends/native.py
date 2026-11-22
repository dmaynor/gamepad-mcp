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

Session ownership is LAZY (M3): constructing this backend does not
connect the client. The first state hand-off connects, and a refusal
(the console's one Remote Play session is held by the §4.10 panel,
or the console is unreachable) raises the client's typed error from
that call — so an MCP server on this backend starts and serves
cleanly while the console refuses sessions, and the refusal surfaces
on the first tool call, never as a hang or a fake success. This
server and the panel are ALTERNATIVE session owners: never
connected simultaneously.
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

    @property
    def current_state(self) -> ControllerState:
        """The last state handed to the client (neutral until then)."""
        return self._state

    # -- state API ------------------------------------------------------
    def supports_state(self) -> bool:
        return True

    def note_disconnected(self) -> None:
        """Forget the tracked state: after a disconnect the console
        holds nothing of ours (the client's disconnect sends neutral
        before the session goes down)."""
        self._state = ControllerState.neutral()

    def ensure_connected(self) -> None:
        """Connect the client if it has no live session (lazy session
        ownership, see the module docstring). A refusal raises the
        client's typed error verbatim — SessionDownError when the
        console's session is held elsewhere, ConsoleUnreachableError
        when it does not answer at all."""
        if not self.client.connected:
            self.client.connect()

    def set_state(self, state: ControllerState, why: str = "") -> InputAck:
        self.ensure_connected()
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
