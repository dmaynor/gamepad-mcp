"""NativeTools: the MCP tool surface over a native console backend.

The legacy server dispatches tools through the keymap ``Controller``:
tool calls become keyboard events aimed at a stream window. A native
backend (PsClient on libchiaki, the StubClient in tests) speaks
complete controller states instead, so when the server's backend is
a ``NativeBackend`` its tools dispatch HERE (M3, 2026-11-22 — the
wiring David ordered with "i want the new stuff").

The contract is PRD §4.4 applied through MCP: **no silent inputs**.
Every result carries the ``InputAck`` of the send it reports (with
its measured latency), or the typed error — verbatim class name and
message — when the send failed or never happened. A tool never
reports success for a send that did not occur.

Translation rules (the names David knows, native semantics):

- ``press`` -> a state with the button added, held for ``hold_ms``
  (capped exactly as the legacy Controller caps), then a state with
  it removed. Both sends' acks ride in the result. The MCP name
  ``share`` is the DualSense ``create`` button in the client
  vocabulary and is mapped here.
- ``hold_stick`` -> a state with the stick's axes set for the
  duration, then the exact prior state restored. Axis convention is
  the client's (live-verified in M2): -1.0 is up/left, +1.0
  down/right; a diagonal sets both axes fully, as holding two
  direction keys did on the keymap path.
- ``release_all`` -> one neutral state send.
- ``key_tap``/``key_down``/``key_up``/``combo``/``mouse_*`` and the
  keymap-profile tools have NO native meaning. They return a typed
  ``UnsupportedStateError`` naming the backend — never a keystroke
  emulation, which would rebuild the chain this client replaces.

New tools the old surface never had: ``frame`` (the current decoded
frame, saved to a file whose path + sha256 + capture timestamp are
returned), ``client_status``, ``wake`` (wake + connect), and
``disconnect`` (clean session end; the client's disconnect sends
neutral first — the dead-man rule).

SESSION OWNERSHIP: this server and the §4.10 diagnostic panel are
ALTERNATIVE owners of the console's single Remote Play session —
never connected simultaneously. The backend connects lazily on
first use, so this server starts and serves while the console
refuses sessions; the refusal (e.g. the panel holds the session)
surfaces typed on the first input call and is kept as ``last_error``
in the status tools.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

from .backends.native import NativeBackend
from .client.errors import ClientError, UnsupportedStateError
from .client.log import MoveLogger
from .client.state import BUTTONS, ControllerState, InputAck

# The MCP surface's name for the DualSense Create button (the PS4-era
# name the tool vocabulary grew up with) -> the client vocabulary.
BUTTON_ALIASES = {"share": "create"}

# Stick direction -> (x, y) in the client's axis convention:
# -1.0 is up/left, +1.0 is down/right (ps.py, live-verified in M2).
DIRECTIONS = {
    "up": (0.0, -1.0),
    "down": (0.0, 1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up-left": (-1.0, -1.0),
    "up-right": (1.0, -1.0),
    "down-left": (-1.0, 1.0),
    "down-right": (1.0, 1.0),
}

# Tools that exist only as keystrokes/mouse/keymap machinery. Under a
# native backend each returns unsupported_result() — typed, named,
# and with nothing sent.
KEYMAP_ONLY_TOOLS = (
    "key_tap", "key_down", "key_up", "combo",
    "mouse_move", "mouse_click", "profiles", "set_profile",
)

SESSION_OWNERSHIP_NOTE = (
    "session ownership: this server and the §4.10 panel are "
    "alternative owners of the console's single Remote Play "
    "session — never connected simultaneously; a connect refused "
    "as already-in-use means the other surface holds it"
)

# How old the newest grabbed frame may be before status calls it
# stale — the panel's bound (defect D1's disclosure rule).
FRAME_STALE_S = 2.0


def _ack_dict(ack: InputAck) -> dict:
    return {
        "ok": ack.ok,
        "latency_ms": round(ack.latency_ms, 3),
        "detail": ack.detail,
    }


def _error_dict(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


def unsupported_result(tool: str, backend_name: str) -> dict:
    """The typed refusal for a tool a backend cannot mean.

    Returned (not raised) so the refusal is IN the tool result, with
    the error's class and message verbatim — the same shape every
    native result uses for a failed send.
    """
    error = UnsupportedStateError(
        f"tool {tool!r} is not supported by backend {backend_name!r}: "
        "it has no native meaning there and nothing was sent"
    )
    return {"ok": False, "tool": tool, "backend": backend_name,
            "error": _error_dict(error)}


class NativeTools:
    """Tool dispatch over one NativeBackend (one client session)."""

    def __init__(
        self,
        backend: NativeBackend,
        frame_dir: Optional[str] = None,
        max_hold_ms: int = 10_000,
        hard_cap_ms: int = 30_000,
    ):
        self.backend = backend
        self.client = backend.client
        self.max_hold_ms = max_hold_ms
        self.hard_cap_ms = hard_cap_ms
        self.frame_dir = Path(
            frame_dir
            or os.environ.get("GAMEPAD_FRAME_DIR")
            or Path.home() / "gamepad-frames"
        )
        # The client's structured input records (PRD §4.5) go to a
        # MoveLogger — a file when GAMEPAD_MOVELOG names one, else
        # in-memory only. The acks in tool results are the live
        # surface; the logger is the dataset-shaped record.
        self.logger = MoveLogger(path=os.environ.get("GAMEPAD_MOVELOG") or None)
        self.client.set_log_sink(self.logger)
        self._last_frame_ts: Optional[float] = None
        self._last_error: Optional[dict] = None

    # -- plumbing -------------------------------------------------------
    def _cap(self, hold_ms: int) -> int:
        return max(0, min(int(hold_ms), self.max_hold_ms, self.hard_cap_ms))

    def _send(self, state: ControllerState, why: str) -> InputAck:
        """One state hand-off through the backend (which lazily
        connects). Success clears last_error; a typed failure is
        recorded for the status tools and re-raised."""
        try:
            ack = self.backend.set_state(state, why=why)
        except ClientError as exc:
            self._last_error = _error_dict(exc)
            raise
        self._last_error = None
        return ack

    def _ensure_connected(self) -> None:
        try:
            self.backend.ensure_connected()
        except ClientError as exc:
            self._last_error = _error_dict(exc)
            raise
        self._last_error = None

    # -- input tools ------------------------------------------------------
    def press(self, button: str, hold_ms: int = 120) -> dict:
        name = BUTTON_ALIASES.get(button, button)
        if name not in BUTTONS:
            error = ValueError(
                f"unknown button {button!r}: the native vocabulary is "
                f"{sorted(BUTTONS | set(BUTTON_ALIASES))}"
            )
            return {"ok": False, "button": button, "error": _error_dict(error)}
        hold = self._cap(hold_ms)
        pressed = self.backend.current_state.with_buttons(add={name})
        try:
            ack = self._send(pressed, why=f"press {name}")
        except ClientError as exc:
            return {"ok": False, "button": button, "error": _error_dict(exc)}
        if hold:
            time.sleep(hold / 1000.0)
        released = pressed.with_buttons(remove={name})
        result = {"ok": True, "button": button, "hold_ms": hold,
                  "ack": _ack_dict(ack)}
        if name != button:
            result["client_button"] = name
        try:
            result["release_ack"] = _ack_dict(
                self._send(released, why=f"release {name}")
            )
        except ClientError as exc:
            # The press DID happen (its ack is in this result); the
            # release failing is reported as itself, never smoothed.
            result["ok"] = False
            result["phase"] = "release"
            result["error"] = _error_dict(exc)
        return result

    def hold_stick(self, stick: str, direction: str, hold_ms: int = 1000) -> dict:
        if stick not in ("left", "right"):
            error = ValueError(f"stick must be 'left' or 'right', got {stick!r}")
            return {"ok": False, "stick": stick, "error": _error_dict(error)}
        key = direction.replace("_", "-")
        if key not in DIRECTIONS:
            error = ValueError(
                f"unknown direction {direction!r}: {sorted(DIRECTIONS)}"
            )
            return {"ok": False, "direction": direction,
                    "error": _error_dict(error)}
        dx, dy = DIRECTIONS[key]
        hold = self._cap(hold_ms)
        prior = self.backend.current_state
        axes = dict(prior.axes)
        axes[f"{stick}_x"] = dx
        axes[f"{stick}_y"] = dy
        held = ControllerState(buttons=prior.buttons, axes=axes,
                               triggers=prior.triggers)
        try:
            ack = self._send(held, why=f"hold_stick {stick} {key}")
        except ClientError as exc:
            return {"ok": False, "stick": stick, "direction": direction,
                    "error": _error_dict(exc)}
        if hold:
            time.sleep(hold / 1000.0)
        result = {"ok": True, "stick": stick, "direction": key,
                  "axes": {f"{stick}_x": dx, f"{stick}_y": dy},
                  "hold_ms": hold, "ack": _ack_dict(ack)}
        try:
            result["release_ack"] = _ack_dict(
                self._send(prior, why=f"release_stick {stick}")
            )
        except ClientError as exc:
            result["ok"] = False
            result["phase"] = "release"
            result["error"] = _error_dict(exc)
        return result

    def release_all(self) -> dict:
        try:
            ack = self._send(ControllerState.neutral(), why="release_all")
        except ClientError as exc:
            return {"ok": False, "error": _error_dict(exc)}
        return {"ok": True, "released": "all", "ack": _ack_dict(ack)}

    def unsupported(self, tool: str) -> dict:
        error = UnsupportedStateError(
            f"tool {tool!r} has no native meaning on backend "
            f"{self.backend.name!r}: the native client speaks "
            "controller state, not keystrokes or mouse events — "
            "nothing was emulated and nothing was sent"
        )
        return {"ok": False, "tool": tool, "backend": self.backend.name,
                "error": _error_dict(error)}

    # -- the new tools ----------------------------------------------------
    def frame(self) -> dict:
        """Grab the current frame: saved to a file; the result
        carries its path, sha256, capture timestamp and dimensions."""
        try:
            self._ensure_connected()
            self.frame_dir.mkdir(parents=True, exist_ok=True)
            path = self.frame_dir / f"frame-{int(time.time() * 1000)}.png"
            result = self.client.grab_frame(str(path))
        except ClientError as exc:
            self._last_error = _error_dict(exc)
            return {"ok": False, "error": _error_dict(exc)}
        except OSError as exc:
            return {"ok": False, "error": _error_dict(exc)}
        self._last_frame_ts = result.captured_ts
        digest = hashlib.sha256(Path(result.path).read_bytes()).hexdigest()
        return {
            "ok": True,
            "path": result.path,
            "sha256": digest,
            "frame_ts": result.captured_ts,
            "width": result.width,
            "height": result.height,
        }

    def client_status(self) -> dict:
        """Session state without side effects: reporting never
        connects (a status call must stay safe while the console
        refuses sessions — that refusal is what last_error holds)."""
        negotiated = getattr(self.client, "negotiated_profile", None)
        profile = negotiated() if callable(negotiated) else None
        age = (
            round(max(0.0, time.time() - self._last_frame_ts), 2)
            if self._last_frame_ts is not None
            else None
        )
        return {
            "ok": True,
            "backend": self.backend.name,
            "client": self.client.name,
            "console": self.client.console,
            "connected": bool(self.client.connected),
            "profile": profile,
            "frame_ts": self._last_frame_ts,
            "frame_age_s": age,
            "frame_stale": (age > FRAME_STALE_S) if age is not None else None,
            "last_error": self._last_error,
            "session_ownership": SESSION_OWNERSHIP_NOTE,
        }

    def status(self) -> dict:
        """The legacy `status` tool's answer on a native backend:
        the client status plus the held-state fields it carried."""
        result = self.client_status()
        result["held"] = sorted(self.backend.current_state.buttons)
        result["max_hold_ms"] = self.max_hold_ms
        result["hard_cap_ms"] = self.hard_cap_ms
        return result

    def wake(self) -> dict:
        """Wake the console, then connect (PRD §4.1)."""
        try:
            self.client.wake()
            self._ensure_connected()
        except ClientError as exc:
            self._last_error = _error_dict(exc)
            return {"ok": False, "connected": False,
                    "error": _error_dict(exc),
                    "session_ownership": SESSION_OWNERSHIP_NOTE}
        status = self.client_status()
        return {"ok": True, "woke": True, "connected": status["connected"],
                "profile": status["profile"]}

    def disconnect(self) -> dict:
        """End the session cleanly. The client's disconnect sends
        the neutral state first (dead-man rule). Disconnecting with
        no live session reports exactly that — disconnected: false —
        rather than claiming an end that never happened."""
        was_connected = bool(self.client.connected)
        if was_connected:
            try:
                self.client.disconnect()
            except ClientError as exc:
                self._last_error = _error_dict(exc)
                return {"ok": False, "connected": True,
                        "error": _error_dict(exc)}
            self.backend.note_disconnected()
        return {"ok": True, "disconnected": was_connected, "connected": False}

    def shutdown(self) -> None:
        """atexit: leave no session — and no held input — behind."""
        try:
            if self.client.connected:
                self.client.disconnect()
        except Exception:
            pass
