"""Typed errors for the remote-play client layer.

PRD §4.4 is the product: *no silent inputs*. Every failure in this layer
is one of these types, raised — never swallowed, never returned as a
bare ``None``. A caller (the loop, the diagnostic panel, a test) can
always distinguish *why* an input or a frame did not happen.
"""

from __future__ import annotations


class ClientError(Exception):
    """Base class for every error raised by a RemotePlayClient."""


class ConsoleUnreachableError(ClientError):
    """The console cannot be reached at all (discovery/connect failed)."""


class ConsoleAsleepError(ClientError):
    """The console is reachable but in rest mode and did not wake."""


class NotConnectedError(ClientError):
    """An operation was attempted with no live session."""


class SessionDownError(ClientError):
    """The streaming session dropped or was never established."""


class InputChannelError(ClientError):
    """Controller state could not be handed to the streaming library.

    This is the typed replacement for tonight's failure mode: in the old
    chain this condition was indistinguishable from success.
    """


class FrameGrabError(ClientError):
    """No decoded frame was available to capture."""


class UnsupportedStateError(ClientError):
    """A backend was asked for state-based input it does not implement.

    Raised by the default ``InputBackend.set_state`` so the keymap-only
    backends fail loudly and typed, never silently.
    """


class RegistrationError(ClientError):
    """Pairing/registration material is missing or was rejected."""


class PadUnavailableError(ClientError):
    """No supported physical gamepad is available to a pad source.

    Raised typed (PRD §4.4's spirit applied to sources): an unpaired
    or unplugged controller is a named condition, not a silent
    neutral state that would look like a player choosing to idle.
    """
