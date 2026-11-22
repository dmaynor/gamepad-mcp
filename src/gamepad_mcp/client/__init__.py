"""First-party remote-play client layer (PRD: remote-client).

The interface (base.RemotePlayClient), its value types (state), its
typed errors (errors), a MOVELOG-compatible logger (log), and an
in-memory stub (stub) for tests. Real clients (PlayStation via
libchiaki, Xbox via sidecar) implement the same interface in their
own modules and are selected by configuration.
"""

from .base import RemotePlayClient
from .errors import (
    ClientError,
    ConsoleAsleepError,
    ConsoleUnreachableError,
    FrameGrabError,
    InputChannelError,
    NotConnectedError,
    PadUnavailableError,
    RegistrationError,
    SessionDownError,
    UnsupportedStateError,
)
from .log import MoveLogger
from .panel import PanelService, serve as serve_panel
from .sources import (
    HidPadReader,
    InputSource,
    ManualSource,
    PadSource,
    StatePump,
    parse_pad_report,
)
from .state import (
    BUTTONS,
    ControllerState,
    FrameResult,
    InputAck,
    InputLogRecord,
)
from .stub import StubClient

__all__ = [
    "RemotePlayClient",
    "StubClient",
    "MoveLogger",
    "PanelService",
    "serve_panel",
    "InputSource",
    "ManualSource",
    "PadSource",
    "HidPadReader",
    "parse_pad_report",
    "StatePump",
    "ControllerState",
    "FrameResult",
    "InputAck",
    "InputLogRecord",
    "BUTTONS",
    "ClientError",
    "ConsoleAsleepError",
    "ConsoleUnreachableError",
    "FrameGrabError",
    "InputChannelError",
    "NotConnectedError",
    "PadUnavailableError",
    "RegistrationError",
    "SessionDownError",
    "UnsupportedStateError",
]
