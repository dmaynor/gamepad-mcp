"""RemotePlayClient: the interface the first-party client implements.

One implementation per console family (PlayStation via libchiaki,
Xbox via the sidecar — SCOPING §2/§3), selected by configuration.
Everything above this layer — the MCP server, the driver, the
§4.10 diagnostic panel — is a thin client of these five operations:

    discover/wake/connect -> grab_frame -> send_state -> (verify)

The panel (§4.10) needs nothing beyond this surface: it polls
``grab_frame`` for the live view, calls ``send_state`` per press and
renders the returned ``InputAck`` (latency included) beside the log
records the backend emits. If the panel ever needs a method that is
not here, that is an interface bug, not a panel feature.
"""

from __future__ import annotations

from typing import Callable, Optional

from .errors import NotConnectedError
from .state import ControllerState, FrameResult, InputAck, InputLogRecord


class RemotePlayClient:
    """Base class; real clients subclass and override every method."""

    name = "base"
    console = "unknown"  # "ps5" | "xbox" in real clients

    def __init__(self):
        self._log_sink: Optional[Callable[[InputLogRecord], None]] = None

    # -- logging hook -------------------------------------------------
    def set_log_sink(self, sink: Optional[Callable[[InputLogRecord], None]]) -> None:
        """Register where structured input records go (PRD §4.5).

        The backend boundary owns logging: every send_state outcome —
        acked or typed-error — produces one record through this sink.
        """
        self._log_sink = sink

    def _emit(self, record: InputLogRecord) -> None:
        if self._log_sink is not None:
            self._log_sink(record)

    # -- session ------------------------------------------------------
    def wake(self) -> None:
        """Wake the console from rest mode. Raises ConsoleAsleepError
        or ConsoleUnreachableError (typed) if it does not wake."""
        raise NotImplementedError

    def connect(self) -> None:
        """Establish the streaming session. Raises typed errors."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """End the session, releasing all input (dead-man rule)."""
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def _require_connected(self) -> None:
        if not self.connected:
            raise NotConnectedError(f"{self.name}: no live session")

    # -- frames -------------------------------------------------------
    def grab_frame(self, path: str) -> FrameResult:
        """Write the current decoded frame to ``path`` and describe it.

        Raises FrameGrabError / SessionDownError / NotConnectedError.
        """
        raise NotImplementedError

    # -- input --------------------------------------------------------
    def send_state(self, state: ControllerState, why: str = "") -> InputAck:
        """Hand one complete controller state to the streaming library.

        Returns an InputAck with send/ack timestamps. Raises a typed
        error (InputChannelError, SessionDownError, NotConnectedError)
        if the state did not reach the library — never returns None,
        never fails silently (PRD §4.4).
        """
        raise NotImplementedError

    def release_all(self) -> InputAck:
        """Send the neutral state (dead-man: nothing held)."""
        return self.send_state(ControllerState.neutral(), why="release_all")
