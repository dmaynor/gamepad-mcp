"""StubClient: an in-memory RemotePlayClient for tests and dry runs.

Records every state it is handed; grab_frame writes a minimal valid
PNG so file-based consumers work unchanged. Failure injection
(``fail_next``) exists to prove the layer's core contract: failures
surface as typed errors with log records, never as silence.
"""

from __future__ import annotations

import struct
import time
import zlib
from typing import List, Optional

from .base import RemotePlayClient
from .errors import ClientError
from .state import ControllerState, FrameResult, InputAck, InputLogRecord


def _tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        raw = kind + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


class StubClient(RemotePlayClient):
    name = "stub"
    console = "stub"

    def __init__(self):
        super().__init__()
        self._connected = False
        self.states: List[ControllerState] = []
        self.awake = True
        self._fail_next: Optional[ClientError] = None

    # -- test controls ------------------------------------------------
    def fail_next(self, error: ClientError) -> None:
        """Make the next send_state raise this typed error (and log it)."""
        self._fail_next = error

    # -- session ------------------------------------------------------
    def wake(self) -> None:
        self.awake = True

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            self.states.append(ControllerState.neutral())
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # -- frames -------------------------------------------------------
    def grab_frame(self, path: str) -> FrameResult:
        self._require_connected()
        with open(path, "wb") as fh:
            fh.write(_tiny_png())
        return FrameResult(path=path, captured_ts=time.time(), width=1, height=1)

    # -- input --------------------------------------------------------
    def send_state(self, state: ControllerState, why: str = "") -> InputAck:
        sent = time.monotonic()
        try:
            self._require_connected()
            if self._fail_next is not None:
                error, self._fail_next = self._fail_next, None
                raise error
            self.states.append(state)
            ack = InputAck(sent_ts=sent, ack_ts=time.monotonic(), ok=True)
            self._emit(
                InputLogRecord.now(
                    input={"tool": "send_state", "state": _state_dict(state)},
                    why=why,
                    result="state handed to stub client",
                    ack=ack,
                )
            )
            return ack
        except ClientError as exc:
            self._emit(
                InputLogRecord.now(
                    input={"tool": "send_state", "state": _state_dict(state)},
                    why=why,
                    result=f"FAILED: {type(exc).__name__}: {exc}",
                    outcome="failed",
                )
            )
            raise


def _state_dict(state: ControllerState) -> dict:
    return {
        "buttons": sorted(state.buttons),
        "axes": dict(state.axes),
        "triggers": dict(state.triggers),
    }
