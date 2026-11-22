"""Input sources: interchangeable front-ends that produce state.

PRD §4.10/§4.11: the diagnostic panel and a real physical gamepad
are the same thing to the client — an InputSource whose read_state()
returns the current ControllerState. Neither produces keystrokes;
both feed the StatePump, which hands changed states to the client's
send_state and so gets the same ack, latency, and logging whichever
front-end a human (or the loop) is driving.

Sources in M1: ManualSource (the panel's model — presses set state).
The physical-pad source (Bluetooth/USB HID reader) is M2 work; it
implements this same ABC and nothing above it changes.
"""

from __future__ import annotations

from typing import Optional

from .base import RemotePlayClient
from .state import ControllerState, InputAck


class InputSource:
    """A producer of controller state."""

    name = "base"

    def read_state(self) -> ControllerState:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ManualSource(InputSource):
    """State set explicitly by the caller — the §4.10 panel's model.

    A panel press is set_button(name, True); a release is
    set_button(name, False); sticks/triggers set their values. The
    pump turns the resulting state changes into send_state calls.
    """

    name = "manual"

    def __init__(self):
        self._state = ControllerState.neutral()

    def set_button(self, button: str, down: bool) -> None:
        if down:
            self._state = self._state.with_buttons(add={button})
        else:
            self._state = self._state.with_buttons(remove={button})

    def set_axis(self, axis: str, value: float) -> None:
        axes = dict(self._state.axes)
        axes[axis] = value
        self._state = ControllerState(
            buttons=self._state.buttons, axes=axes, triggers=dict(self._state.triggers)
        )

    def set_trigger(self, trigger: str, value: float) -> None:
        triggers = dict(self._state.triggers)
        triggers[trigger] = value
        self._state = ControllerState(
            buttons=self._state.buttons, axes=dict(self._state.axes), triggers=triggers
        )

    def set_state(self, state: ControllerState) -> None:
        self._state = state

    def read_state(self) -> ControllerState:
        return self._state


class StatePump:
    """Moves state changes from a source to a client.

    step() reads the source; if the state changed since the last
    send, it hands the new state to client.send_state (which acks,
    timestamps, and logs it — PRD §4.4/§4.5/§4.9) and returns the ack.
    Unchanged state sends nothing and returns None. A typed error
    from send_state propagates — the pump never swallows it.
    """

    def __init__(self, source: InputSource, client: RemotePlayClient):
        self.source = source
        self.client = client
        self._last_sent: Optional[ControllerState] = None

    def step(self, why: str = "") -> Optional[InputAck]:
        state = self.source.read_state()
        if state == self._last_sent:
            return None
        ack = self.client.send_state(state, why=why or f"source:{self.source.name}")
        self._last_sent = state
        return ack

    def release(self) -> Optional[InputAck]:
        """Dead-man: force the neutral state through, whatever the source says."""
        if self._last_sent is not None and self._last_sent.is_neutral():
            return None
        ack = self.client.send_state(
            ControllerState.neutral(), why=f"release:{self.source.name}"
        )
        self._last_sent = ControllerState.neutral()
        return ack
