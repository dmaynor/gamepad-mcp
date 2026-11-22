"""M2 fix pass: D1 (frame pump silent stall), D2 (SIGINT), D4 (1080p).

Each test pins the exact failure shape the 2026-11-21 live rehearsal
recorded (BUILDLOG-M2R), so the defect cannot return quietly:

- D1: grab_frame waited on a fresh event subscription per call with
  no timeout; the panel then served a frozen frame as live with an
  empty frame_error. The grab is now a bounded poll of the frame
  handler, and the panel discloses staleness in status + log even
  if the pump thread itself is the wedged thing.
- D2: a background-launched panel inherited SIGINT as SIG_IGN and
  could only be SIGTERMed. Handlers are now installed explicitly.
- D4: connect() set only the local profile preset (already 1080p by
  default) while the library governed the session by the remote
  preset (720p default). Both are now set.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from gamepad_mcp.client import (
    FrameGrabError,
    MoveLogger,
    PanelService,
    StubClient,
    install_shutdown_handlers,
    serve_panel,
)
from gamepad_mcp.client.panel import serve_until_signal
from gamepad_mcp.client.ps import PsClient

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")


# ---------------------------------------------------------------------
# D1a: PsClient frames come from ONE persistent iterator, owned by a
# pump thread; grab_frame reads the newest slot under a deadline.
# (Per-grab iterators wedged the event; bare get_frame() polling
# starved after one buffered frame — both proven live, BUILDLOG-M2F.)
# ---------------------------------------------------------------------


def _frame(value=7):
    return np.full((4, 5, 3), value, dtype=np.uint8)


class FakeFrameSession:
    """frames() returns a fresh iterator from the given factory and
    counts the calls; get_frame() must never be used."""

    def __init__(self, factory):
        self._factory = factory
        self.is_active = True
        self.frames_calls = 0

    def frames(self, max_fps=60.0):
        self.frames_calls += 1
        return self._factory()

    @property
    def frame_handler(self):
        raise AssertionError("grab path used frame_handler.get_frame()")


def _ps_with_frames(factory):
    client = PsClient()
    client._session = FakeFrameSession(factory)
    client._start_frame_pump()
    return client


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_grab_frame_reads_pumped_frame(tmp_path):
    client = _ps_with_frames(lambda: iter([_frame(7)]))
    out = str(tmp_path / "f.png")
    result = client.grab_frame(out, timeout=2.0)
    assert (result.width, result.height) == (5, 4)
    assert os.path.getsize(out) > 0
    # one persistent iterator across grabs — not one per grab (D1)
    client.grab_frame(out, timeout=2.0)
    assert client._session.frames_calls == 1


def test_grab_frame_waits_for_first_frame(tmp_path):
    def gen():
        time.sleep(0.3)
        yield _frame(9)

    client = _ps_with_frames(lambda: gen())
    t0 = time.monotonic()
    result = client.grab_frame(str(tmp_path / "f.png"), timeout=2.0)
    assert result.width == 5
    assert time.monotonic() - t0 >= 0.25


def test_grab_frame_deadline_is_typed_never_a_hang(tmp_path):
    never = threading.Event()

    def gen():
        never.wait()
        yield _frame()  # pragma: no cover - never reached

    client = _ps_with_frames(lambda: gen())
    t0 = time.monotonic()
    with pytest.raises(FrameGrabError):
        client.grab_frame(str(tmp_path / "f.png"), timeout=0.3)
    assert time.monotonic() - t0 < 2.0


def test_grab_frame_source_failure_is_typed(tmp_path):
    def gen():
        raise RuntimeError("decoder died")
        yield  # pragma: no cover - makes this a generator

    client = _ps_with_frames(lambda: gen())
    with pytest.raises(FrameGrabError) as excinfo:
        client.grab_frame(str(tmp_path / "f.png"), timeout=2.0)
    assert "decoder died" in str(excinfo.value)


# ---------------------------------------------------------------------
# D1b: the panel discloses a stall — status, error text, and log —
# even when the pump thread itself is wedged inside grab_frame
# ---------------------------------------------------------------------


class WedgedClient(StubClient):
    """grab_frame blocks far beyond the stall bound (the D1 shape)."""

    def grab_frame(self, path):
        time.sleep(30)
        return super().grab_frame(path)


class DeadClient(StubClient):
    def grab_frame(self, path):
        raise FrameGrabError("no decoded frame within 2.0 s")


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_panel_wedged_pump_still_surfaces_stall():
    logger = MoveLogger()
    service = PanelService(
        WedgedClient(), logger=logger, stall_threshold=0.3
    )
    service.client.connect()
    service.start_frames(interval=0.05)
    try:
        status = service.status()  # read path must disclose it
        assert _wait_for(lambda: service.status()["frame_stale"])
        status = service.status()
        assert status["frame_stale"] is True
        assert "FrameStallError" in status["frame_error"]
        assert status["frame_age_s"] >= 0.3
        failed = [r for r in logger.records if r.outcome == "failed"]
        assert any("FrameStallError" in r.result for r in failed)
        # exactly one log record per stall episode, not one per poll
        service.status()
        service.status()
        failed = [r for r in logger.records if r.outcome == "failed"]
        assert len([r for r in failed if "FrameStallError" in r.result]) == 1
    finally:
        service.stop_frames()


def test_panel_dead_grab_surfaces_error_and_stall():
    logger = MoveLogger()
    service = PanelService(DeadClient(), logger=logger, stall_threshold=0.3)
    service.client.connect()
    service.start_frames(interval=0.05)
    try:
        assert _wait_for(lambda: "FrameGrabError" in service.status()["frame_error"])
        assert _wait_for(lambda: service.status()["frame_stale"])
    finally:
        service.stop_frames()


def test_panel_fresh_frames_are_not_stale(tmp_path):
    logger = MoveLogger()
    client = StubClient()
    client.connect()
    service = PanelService(
        client, logger=logger, frame_dir=str(tmp_path), stall_threshold=1.0
    )
    service.start_frames(interval=0.05)
    try:
        assert _wait_for(lambda: service.frame()[0] is not None)
        status = service.status()
        assert status["frame_stale"] is False
        assert status["frame_error"] == ""
        assert status["frame_age_s"] is not None
    finally:
        service.stop_frames()


# ---------------------------------------------------------------------
# D2: SIGINT stops the panel gracefully (release -> disconnect)
# ---------------------------------------------------------------------


def test_sigint_runs_the_graceful_stop():
    client = StubClient()
    client.connect()
    logger = MoveLogger()
    service = PanelService(client, logger=logger)
    server = serve_panel(service, "127.0.0.1", 0)
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    install_shutdown_handlers(server)
    thread = threading.Thread(
        target=serve_until_signal, args=(service, server), daemon=True
    )
    thread.start()
    try:
        time.sleep(0.3)  # serve_forever is running
        os.kill(os.getpid(), signal.SIGINT)
        thread.join(timeout=5)
        assert not thread.is_alive(), "serve_forever did not stop on SIGINT"
        # the graceful path ran: release sent a neutral state and the
        # client disconnected — not a dropped process
        assert client.connected is False
        assert client.states, "release/disconnect sent no state"
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)


# ---------------------------------------------------------------------
# D4: connect() requests 1080p60 on BOTH profile presets
# ---------------------------------------------------------------------


class FakeSettings:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))

        return record


class FakeLibSession:
    captured = {}

    def __init__(self, registration, settings=None):
        FakeLibSession.captured = {"registration": registration, "settings": settings}

    def connect(self):
        pass


class FakeChiakiForConnect:
    class lib:
        Settings = FakeSettings
        VideoResolutionPreset = type(
            "VRP", (), {"Resolution1080p": "RES1080"}
        )
        VideoFPSPreset = type("VFP", (), {"FPS60": "FPS60"})

    Session = FakeLibSession


def test_connect_sets_local_and_remote_1080p(monkeypatch):
    monkeypatch.setattr(PsClient, "_lib", staticmethod(lambda: FakeChiakiForConnect))
    client = PsClient()
    monkeypatch.setattr(client, "_registration", lambda: object())
    client.connect()
    settings = FakeLibSession.captured["settings"]
    assert ("set_resolution_local_ps5", ("RES1080",)) in settings.calls
    assert ("set_resolution_remote_ps5", ("RES1080",)) in settings.calls
    assert ("set_fpslocal_ps5", ("FPS60",)) in settings.calls
    assert ("set_fpsremote_ps5", ("FPS60",)) in settings.calls
