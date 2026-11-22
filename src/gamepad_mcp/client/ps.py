"""PlayStation client over chiaki-py (the libchiaki bindings).

M1 state, proven live 2026-11-21 against PS5-582 (BUILDLOG-M1):
discovery finds the console, a registration rebuilt from chiaki-ng's
existing QSettings plist validates, Session.connect() establishes,
and the first decoded frame arrives in ~0.3 s as a 1280x720 RGB
array — no window, no screencapture, no plugin.

Input (send_state) lands in M2; here it raises InputChannelError —
typed and logged, never silent (PRD §4.4). Wake-from-rest is likewise
not yet wired: wake() reports the console's discovery state honestly
rather than pretending.

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
    ConsoleAsleepError,
    ConsoleUnreachableError,
    FrameGrabError,
    InputChannelError,
    RegistrationError,
    SessionDownError,
)
from .state import ControllerState, FrameResult, InputAck, InputLogRecord

DEFAULT_PLIST = Path.home() / "Library/Preferences/com.chiaki.Chiaki.plist"


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

    def _registration(self):
        if not self.plist_path.exists():
            raise RegistrationError(f"no chiaki settings at {self.plist_path}")
        chiaki_py = self._lib()
        with open(self.plist_path, "rb") as fh:
            pl = plistlib.load(fh)
        prefix = f"registered_hosts.{self.host_index}."
        get = lambda k: pl.get(prefix + k)
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
    def wake(self) -> None:
        chiaki_py = self._lib()
        disc = chiaki_py.find_host(self.host_name, timeout=3.0)
        if disc is None:
            raise ConsoleUnreachableError(
                f"{self.host_name} not answering discovery on the LAN"
            )
        state = str(getattr(disc, "state", ""))
        if "Ready" in state or "Running" in state:
            return
        raise ConsoleAsleepError(
            f"{self.host_name} discovery state is {state!r}; "
            "wake-packet wiring lands in M2"
        )

    def connect(self) -> None:
        chiaki_py = self._lib()
        reg = self._registration()
        session = chiaki_py.Session(reg)
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
                self._session.disconnect()
            finally:
                self._session = None

    @property
    def connected(self) -> bool:
        return self._session is not None and bool(self._session.is_active)

    # -- frames -----------------------------------------------------------
    def grab_frame(self, path: str) -> FrameResult:
        self._require_connected()
        try:
            frame = next(iter(self._session.frames()))
        except Exception as exc:
            raise FrameGrabError(
                f"no frame from session ({type(exc).__name__}): {exc}"
            ) from exc
        try:
            from PIL import Image
        except ImportError as exc:
            raise FrameGrabError("Pillow is required to save frames") from exc
        img = Image.fromarray(frame)
        img.save(path)
        return FrameResult(
            path=path, captured_ts=time.time(), width=img.width, height=img.height
        )

    # -- input: M2 ---------------------------------------------------------
    def send_state(self, state: ControllerState, why: str = "") -> InputAck:
        error = InputChannelError(
            "PS controller input lands in M2; M1 proves connect + frames"
        )
        self._emit(
            InputLogRecord.now(
                input={"tool": "send_state", "state": {
                    "buttons": sorted(state.buttons),
                    "axes": dict(state.axes),
                    "triggers": dict(state.triggers),
                }},
                why=why,
                result=f"FAILED: InputChannelError: {error}",
                outcome="failed",
            )
        )
        raise error
