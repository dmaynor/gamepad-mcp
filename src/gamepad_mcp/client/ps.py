"""PlayStation client over chiaki-py (the libchiaki bindings).

M1 (2026-11-21, BUILDLOG-M1) proved connect + decoded frames from
our own code on the mini. M2 adds the rest of the contract:

- ``send_state`` drives libchiaki's controller-state path natively
  (PRD §4.3): per-button press/release calls, stick axes scaled to
  the library's ±1023, triggers to 0..255, then
  ``send_feedback_state()`` flushes the accumulated state to the
  console. Every call returns an InputAck with send/ack timestamps
  and emits a MoveLogger record — on failure it emits a FAILED
  record and raises a typed error (PRD §4.4: no silent inputs).
- ``wake`` sends the discovery wakeup packet and polls until the
  console reports Ready (PRD §4.1).
- ``connect`` requests 1080p60 on both the library's local and
  remote profile presets (defect D4: this connection is governed
  by the *remote* profile — sessions negotiated its 720p default
  while only the already-1080p local preset was being set), and
  ``negotiated_profile()`` reports what the console actually agreed
  to — the verified number, not the request.
- ``grab_frame`` pulls decoded frames from the session's frame
  handler under a bounded deadline — never the ``session.frames()``
  event iterator, whose unbounded wait wedged the panel's frame
  pump permanently and silently (defect D1).

Axis convention (verified live in the M2 rehearsal, BUILDLOG-M2):
ControllerState axes are libchiaki's — -1.0 is stick fully up/left,
+1.0 fully down/right; in-game, left_y = -1.0 walks forward.

Registration values are credentials: they are read from the plist on
the host, held in memory, and never logged or printed.
"""

from __future__ import annotations

import plistlib
import time
from pathlib import Path
from typing import Optional

from .base import RemotePlayClient
from .errors import (
    ClientError,
    ConsoleAsleepError,
    ConsoleUnreachableError,
    FrameGrabError,
    InputChannelError,
    RegistrationError,
    SessionDownError,
)
from .state import ControllerState, FrameResult, InputAck, InputLogRecord

DEFAULT_PLIST = Path.home() / "Library/Preferences/com.chiaki.Chiaki.plist"

_AXIS_SCALE = 1023  # libchiaki stick range (chiaki-py controller reference)
_TRIGGER_SCALE = 255

# ControllerState button name -> (press method, release method) on the
# pybind session. dpad names map to the library's directional calls.
_BUTTON_METHODS = {
    "cross": ("press_cross", "release_cross"),
    "circle": ("press_circle", "release_circle"),
    "square": ("press_square", "release_square"),
    "triangle": ("press_triangle", "release_triangle"),
    "l1": ("press_l1", "release_l1"),
    "r1": ("press_r1", "release_r1"),
    "l3": ("press_l3", "release_l3"),
    "r3": ("press_r3", "release_r3"),
    "dpad_up": ("press_up", "release_up"),
    "dpad_down": ("press_down", "release_down"),
    "dpad_left": ("press_left", "release_left"),
    "dpad_right": ("press_right", "release_right"),
    "ps": ("press_ps", "release_ps"),
    "touchpad": ("press_touchpad", "release_touchpad"),
    "create": ("press_create", "release_create"),
    "options": ("press_options", "release_options"),
    # l2/r2 have no press/release calls in libchiaki — they are the
    # analog triggers (set_l2/set_r2), handled in apply_state.
}


def apply_state(cp, state: ControllerState) -> None:
    """Push one complete ControllerState into a ChiakiPySession.

    Pure function of (cp, state) so tests can drive it with a
    recording fake. Applies every field — buttons are pressed or
    released to match the state exactly, sticks and triggers are set
    absolutely — then flushes with send_feedback_state(). Any library
    exception propagates to the caller.
    """
    for name, (press, release) in _BUTTON_METHODS.items():
        getattr(cp, press if name in state.buttons else release)()
    cp.set_left(
        round(state.axes.get("left_x", 0.0) * _AXIS_SCALE),
        round(state.axes.get("left_y", 0.0) * _AXIS_SCALE),
    )
    cp.set_right(
        round(state.axes.get("right_x", 0.0) * _AXIS_SCALE),
        round(state.axes.get("right_y", 0.0) * _AXIS_SCALE),
    )
    # Analog triggers win; a bare l2/r2 button means fully pressed.
    l2 = state.triggers.get("l2", 1.0 if "l2" in state.buttons else 0.0)
    r2 = state.triggers.get("r2", 1.0 if "r2" in state.buttons else 0.0)
    cp.set_l2(round(l2 * _TRIGGER_SCALE))
    cp.set_r2(round(r2 * _TRIGGER_SCALE))
    cp.send_feedback_state()


def _state_dict(state: ControllerState) -> dict:
    return {
        "buttons": sorted(state.buttons),
        "axes": dict(state.axes),
        "triggers": dict(state.triggers),
    }


class PsClient(RemotePlayClient):
    name = "ps-chiaki-py"
    console = "ps5"

    def __init__(
        self,
        host_name: str = "PS5-582",
        plist_path: Path = DEFAULT_PLIST,
        host_index: int = 1,
    ):
        super().__init__()
        self.host_name = host_name
        self.plist_path = Path(plist_path)
        self.host_index = host_index
        self._session = None

    # -- internals ------------------------------------------------------
    @staticmethod
    def _lib():
        try:
            import chiaki_py
        except ImportError as exc:
            raise SessionDownError(
                "chiaki_py is not installed (TestPyPI: chiaki-py)"
            ) from exc
        return chiaki_py

    def _plist(self) -> dict:
        if not self.plist_path.exists():
            raise RegistrationError(f"no chiaki settings at {self.plist_path}")
        with open(self.plist_path, "rb") as fh:
            return plistlib.load(fh)

    def _plist_get(self, pl: dict, key: str):
        return pl.get(f"registered_hosts.{self.host_index}." + key)

    def _registration(self):
        pl = self._plist()  # missing material is a RegistrationError first
        chiaki_py = self._lib()
        get = lambda k: self._plist_get(pl, k)  # noqa: E731
        disc = chiaki_py.find_host(self.host_name, timeout=3.0)
        if disc is None:
            raise ConsoleUnreachableError(
                f"{self.host_name} not answering discovery on the LAN"
            )
        try:
            return chiaki_py.HostRegistration(
                host=disc.host_addr,
                target=get("target"),
                regist_key=get("rp_regist_key"),
                nickname=get("server_nickname"),
                morning=get("rp_key"),
                initial_login_pin=get("console_pin") or "",
                ps5=True,
            )
        except Exception as exc:
            raise RegistrationError(
                f"stored registration did not validate ({type(exc).__name__})"
            ) from exc

    # -- session --------------------------------------------------------
    def wake(self, timeout: float = 30.0) -> None:
        """Wake the console from rest mode (PRD §4.1).

        Sends the libchiaki discovery wakeup packet (registered host
        address + the stored regist key, which never leaves the host),
        then polls discovery until the console reports Ready. Raises
        ConsoleUnreachableError if it never answers, ConsoleAsleepError
        if it answers but does not wake within ``timeout``.
        """
        chiaki_py = self._lib()
        disc = chiaki_py.find_host(self.host_name, timeout=3.0)
        if disc is None:
            raise ConsoleUnreachableError(
                f"{self.host_name} not answering discovery on the LAN"
            )
        if self._is_ready(disc):
            return
        pl = self._plist()
        regist_key = self._plist_get(pl, "rp_regist_key")
        if not regist_key:
            raise RegistrationError("no stored regist key to wake with")
        manager = chiaki_py.lib.DiscoveryManager()
        try:
            manager.send_wakeup(disc.host_addr, regist_key, True)
        except Exception as exc:
            raise ConsoleAsleepError(
                f"wakeup packet failed ({type(exc).__name__}): {exc}"
            ) from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            disc = chiaki_py.find_host(self.host_name, timeout=2.0)
            if disc is not None and self._is_ready(disc):
                return
        raise ConsoleAsleepError(
            f"{self.host_name} did not report Ready within {timeout:.0f} s of wakeup"
        )

    @staticmethod
    def _is_ready(disc) -> bool:
        state = str(getattr(disc, "state", ""))
        return "Ready" in state or "Running" in state

    def connect(self) -> None:
        chiaki_py = self._lib()
        reg = self._registration()
        settings = chiaki_py.lib.Settings()
        # D4 (BUILDLOG-M2R/M2F): the library governs this connection
        # with its REMOTE video profile — every session negotiated
        # the remote default (1280x720@60, bitrate 10000, verified
        # against Settings.get_video_profile_remote_ps5()) while the
        # code set only the local preset, which already defaults to
        # 1080p and changed nothing. Set both presets so the request
        # is identical whichever the library applies;
        # negotiated_profile() remains the truth about the answer.
        settings.set_resolution_local_ps5(
            chiaki_py.lib.VideoResolutionPreset.Resolution1080p
        )
        settings.set_resolution_remote_ps5(
            chiaki_py.lib.VideoResolutionPreset.Resolution1080p
        )
        settings.set_fpslocal_ps5(chiaki_py.lib.VideoFPSPreset.FPS60)
        settings.set_fpsremote_ps5(chiaki_py.lib.VideoFPSPreset.FPS60)
        session = chiaki_py.Session(reg, settings=settings)
        try:
            session.connect()
        except Exception as exc:
            raise SessionDownError(
                f"session connect failed ({type(exc).__name__}): {exc}"
            ) from exc
        self._session = session

    def disconnect(self) -> None:
        if self._session is not None:
            try:
                # Dead-man rule (PRD §6): release everything before the
                # session goes down, best effort — a dying session must
                # not leave a button held on the console.
                try:
                    apply_state(self._session.cp_session, ControllerState.neutral())
                except Exception:
                    pass
                self._session.disconnect()
            finally:
                self._session = None

    @property
    def connected(self) -> bool:
        return self._session is not None and bool(self._session.is_active)

    def negotiated_profile(self) -> Optional[dict]:
        """The video profile the console actually negotiated.

        This reading is the record, never the request (D4): through
        the M2 rehearsal it reported 1280x720@60/bitrate 10000 —
        the library's remote-profile default — while connect() was
        requesting 1080p on the local preset only. If a future
        reading disagrees with the request in connect(), the
        disagreement is reported, not smoothed over.
        """
        if self._session is None:
            return None
        try:
            vp = self._session.cp_session.get_video_profile()
        except Exception:
            return None
        return {
            "width": vp.width,
            "height": vp.height,
            "max_fps": vp.max_fps,
            "bitrate": vp.bitrate,
            "codec": str(vp.codec),
        }

    # -- frames -----------------------------------------------------------
    def grab_frame(self, path: str, timeout: float = 2.0) -> FrameResult:
        """Capture one decoded frame, waiting at most ``timeout``.

        Pulls from the session's frame handler directly
        (``CpuFrameHandler.get_frame()``: the next decoded frame, or
        None if none is available yet) and polls it under a bounded
        deadline. This path cannot wedge: worst case it raises
        FrameGrabError at the deadline — typed, never silent.

        The previous implementation took ``next(iter(session
        .frames()))`` — a fresh event subscription per grab, waited
        on with no timeout. At the panel pump's 0.2 s cadence the
        frame event stopped arriving after ~2 grabs and the wait
        blocked forever: no exception, no error, a frozen frame
        served as live (defect D1, BUILDLOG-M2R/M2F). Direct grabs
        at ~1 s cadence never tripped it, which is why M1/M2's
        proofs passed while the pump died.
        """
        self._require_connected()
        handler = self._session.frame_handler
        deadline = time.monotonic() + timeout
        frame = None
        while frame is None:
            try:
                frame = handler.get_frame()
            except RuntimeError:
                # One unreadable frame: drop it and keep polling
                # (the library's own iterator does the same).
                frame = None
            except Exception as exc:
                raise FrameGrabError(
                    f"frame pull failed ({type(exc).__name__}): {exc}"
                ) from exc
            if frame is None:
                if time.monotonic() >= deadline:
                    raise FrameGrabError(
                        f"no decoded frame within {timeout:.1f} s"
                    )
                time.sleep(0.005)
        try:
            from PIL import Image
        except ImportError as exc:
            raise FrameGrabError("Pillow is required to save frames") from exc
        img = Image.fromarray(frame)
        img.save(path)
        return FrameResult(
            path=path, captured_ts=time.time(), width=img.width, height=img.height
        )

    # -- input --------------------------------------------------------------
    def send_state(self, state: ControllerState, why: str = "") -> InputAck:
        sent = time.monotonic()
        try:
            self._require_connected()
            cp = self._session.cp_session
            if not cp.is_connected():
                raise SessionDownError("session reports not connected")
            apply_state(cp, state)
        except ClientError as exc:
            self._emit_failed(state, why, exc)
            raise
        except Exception as exc:
            error = InputChannelError(
                f"controller state hand-off failed ({type(exc).__name__}): {exc}"
            )
            self._emit_failed(state, why, error)
            raise error from exc
        ack = InputAck(sent_ts=sent, ack_ts=time.monotonic(), ok=True)
        self._emit(
            InputLogRecord.now(
                input={"tool": "send_state", "state": _state_dict(state)},
                why=why,
                result="state handed to libchiaki session",
                ack=ack,
            )
        )
        return ack

    def _emit_failed(self, state: ControllerState, why: str, exc: Exception) -> None:
        self._emit(
            InputLogRecord.now(
                input={"tool": "send_state", "state": _state_dict(state)},
                why=why,
                result=f"FAILED: {type(exc).__name__}: {exc}",
                outcome="failed",
            )
        )
