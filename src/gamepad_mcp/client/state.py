"""Value types for the remote-play client layer.

``ControllerState`` is the complete controller state the streaming
library takes per cycle (SCOPING §2): buttons, stick axes, triggers.
It is a value, not an event — a hold is a state that persists until
changed, guarded by the caller's TTL/watchdog, exactly as libchiaki
expects.

``InputAck`` is PRD §4.4/§4.9 made concrete: every state hand-off
returns when it was sent and when the library acknowledged it, so the
latency distribution is computed from these records and a failed
hand-off is a typed error instead of silence.

``InputLogRecord`` is the structured form of one MOVELOG entry
(timestamp, input, why, verified result, outcome) — the markdown log
is a rendering of these records, and the §4.10 diagnostic panel reads
the records directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# DualSense-shaped button vocabulary (the MCP tool surface's names).
BUTTONS = frozenset(
    {
        "cross", "circle", "square", "triangle",
        "l1", "r1", "l2", "r2", "l3", "r3",
        "dpad_up", "dpad_down", "dpad_left", "dpad_right",
        "ps", "touchpad", "create", "options",
    }
)

AXES = ("left_x", "left_y", "right_x", "right_y")


@dataclass(frozen=True)
class ControllerState:
    """One complete controller state.

    buttons: names from BUTTONS currently held.
    axes: stick positions in [-1.0, 1.0], keys from AXES.
    triggers: analog L2/R2 in [0.0, 1.0] (also mirrored as buttons
    when fully digital in the button set — the state carries both,
    as the hardware does).
    """

    buttons: frozenset = frozenset()
    axes: dict = field(default_factory=dict)
    triggers: dict = field(default_factory=dict)

    def __post_init__(self):
        unknown = set(self.buttons) - BUTTONS
        if unknown:
            raise ValueError(f"unknown buttons: {sorted(unknown)}")
        for name, value in self.axes.items():
            if name not in AXES:
                raise ValueError(f"unknown axis: {name!r}")
            if not -1.0 <= value <= 1.0:
                raise ValueError(f"axis {name} out of range: {value}")
        for name, value in self.triggers.items():
            if name not in ("l2", "r2"):
                raise ValueError(f"unknown trigger: {name!r}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"trigger {name} out of range: {value}")

    @classmethod
    def neutral(cls) -> "ControllerState":
        """Nothing held, sticks centred, triggers released."""
        return cls()

    def with_buttons(self, *, add=frozenset(), remove=frozenset()) -> "ControllerState":
        return ControllerState(
            buttons=(set(self.buttons) | set(add)) - set(remove),
            axes=dict(self.axes),
            triggers=dict(self.triggers),
        )

    def is_neutral(self) -> bool:
        return (
            not self.buttons
            and all(v == 0.0 for v in self.axes.values())
            and all(v == 0.0 for v in self.triggers.values())
        )


@dataclass(frozen=True)
class InputAck:
    """Acknowledgement of one state hand-off (PRD §4.4, §4.9)."""

    sent_ts: float  # time.monotonic() at hand-off
    ack_ts: float  # time.monotonic() when the library confirmed
    ok: bool = True
    detail: str = ""

    @property
    def latency_ms(self) -> float:
        return (self.ack_ts - self.sent_ts) * 1000.0


@dataclass(frozen=True)
class FrameResult:
    """One captured frame: a file plus when it was captured."""

    path: str
    captured_ts: float  # time.time() wall clock, matches file naming
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class InputLogRecord:
    """Structured form of one MOVELOG entry.

    outcome vocabulary matches dataset v1: moved | no_effect | failed |
    interacted (plus "sent" for a state hand-off whose game effect has
    not been verified from a frame yet — verification is the caller's
    next step, and the record is updated in place when it happens).
    """

    ts: float  # time.time() wall clock
    input: dict  # {"tool": ..., "state": {...}} or a key description
    why: str
    result: str = ""
    outcome: str = "sent"
    ack: Optional[InputAck] = None

    @classmethod
    def now(cls, input: dict, why: str, **kw) -> "InputLogRecord":
        return cls(ts=time.time(), input=input, why=why, **kw)
