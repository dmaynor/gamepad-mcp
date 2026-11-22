"""Tests for the remote-play client interface skeleton (M1).

These pin the PRD's core contracts:
- §4.4 no silent inputs: failures are typed errors AND log records.
- §4.9 latency records: every ack carries send/ack timestamps.
- §4.5/§9.4 logging continuity: records render in MOVELOG format.
- SCOPING §5: the state API is optional on InputBackend and honest
  by default; the native backend folds keys into controller state.
- §4.10: the diagnostic panel is a thin client — everything it needs
  (frame grab, send_state + ack, log records) is on this surface.
"""

import pytest

from gamepad_mcp.backends import make_backend
from gamepad_mcp.backends.base import InputBackend
from gamepad_mcp.backends.native import NativeBackend
from gamepad_mcp.backends.null import NullBackend
from gamepad_mcp.client import (
    ControllerState,
    InputAck,
    InputChannelError,
    ManualSource,
    MoveLogger,
    NotConnectedError,
    StatePump,
    StubClient,
    UnsupportedStateError,
)


# -- ControllerState ----------------------------------------------------

def test_neutral_state_is_neutral():
    assert ControllerState.neutral().is_neutral()


def test_state_rejects_unknown_button():
    with pytest.raises(ValueError):
        ControllerState(buttons={"hyper"})


def test_state_rejects_out_of_range_axis():
    with pytest.raises(ValueError):
        ControllerState(axes={"left_x": 1.5})


def test_with_buttons_adds_and_removes():
    s = ControllerState.neutral().with_buttons(add={"cross"})
    assert s.buttons == {"cross"}
    assert s.with_buttons(remove={"cross"}).is_neutral()


# -- StubClient: acks, frames, typed failures -----------------------------

def test_send_state_returns_ack_with_latency():
    c = StubClient()
    c.connect()
    ack = c.send_state(ControllerState(buttons={"cross"}), why="test")
    assert isinstance(ack, InputAck)
    assert ack.ok and ack.ack_ts >= ack.sent_ts and ack.latency_ms >= 0


def test_send_state_requires_connection():
    c = StubClient()
    with pytest.raises(NotConnectedError):
        c.send_state(ControllerState.neutral())


def test_failure_is_typed_and_logged_never_silent():
    c = StubClient()
    c.connect()
    logger = MoveLogger()
    c.set_log_sink(logger)
    c.fail_next(InputChannelError("library refused the state"))
    with pytest.raises(InputChannelError):
        c.send_state(ControllerState(buttons={"circle"}), why="door test")
    assert len(logger.records) == 1
    rec = logger.records[0]
    assert rec.outcome == "failed"
    assert "InputChannelError" in rec.result
    assert rec.why == "door test"


def test_successful_send_is_logged_with_ack():
    c = StubClient()
    c.connect()
    logger = MoveLogger()
    c.set_log_sink(logger)
    c.send_state(ControllerState(buttons={"triangle"}), why="feed")
    rec = logger.records[0]
    assert rec.ack is not None and rec.outcome == "sent"


def test_grab_frame_writes_a_real_png(tmp_path):
    c = StubClient()
    c.connect()
    out = tmp_path / "frame.png"
    result = c.grab_frame(str(out))
    assert out.read_bytes().startswith(b"\x89PNG")
    assert result.path == str(out) and result.captured_ts > 0


def test_release_all_sends_neutral():
    c = StubClient()
    c.connect()
    c.send_state(ControllerState(buttons={"l1"}))
    c.release_all()
    assert c.states[-1].is_neutral()


def test_disconnect_releases_input():
    c = StubClient()
    c.connect()
    c.send_state(ControllerState(buttons={"r1"}))
    c.disconnect()
    assert c.states[-1].is_neutral() and not c.connected


# -- MoveLogger rendering ---------------------------------------------------

def test_movelog_render_matches_survey_format():
    logger = MoveLogger()
    c = StubClient()
    c.connect()
    c.set_log_sink(logger)
    c.send_state(ControllerState(buttons={"cross"}), why="resume the game")
    line = MoveLogger.render(logger.records[0], 7)
    assert line.startswith("7. **")
    assert "send_state buttons=[cross]" in line
    assert "WHY: resume the game" in line
    assert "RESULT:" in line and "(ack " in line


def test_movelog_writes_markdown_file(tmp_path):
    path = tmp_path / "MOVELOG.md"
    logger = MoveLogger(str(path))
    c = StubClient()
    c.connect()
    c.set_log_sink(logger)
    c.send_state(ControllerState.neutral(), why="check")
    text = path.read_text()
    assert text.startswith("1. **") and "WHY: check" in text


# -- InputBackend state API ---------------------------------------------------

def test_keymap_backend_state_api_is_honestly_unsupported():
    backend = NullBackend()
    assert backend.supports_state() is False
    with pytest.raises(UnsupportedStateError):
        backend.set_state(ControllerState.neutral())


def test_base_backend_state_api_is_honestly_unsupported():
    with pytest.raises(UnsupportedStateError):
        InputBackend().set_state(ControllerState.neutral())


def test_native_backend_folds_keys_into_state():
    c = StubClient()
    c.connect()
    backend = NativeBackend(c)
    assert backend.supports_state() is True
    backend.key_down("cross")
    assert c.states[-1].buttons == {"cross"}
    backend.key_down("l1")
    assert c.states[-1].buttons == {"cross", "l1"}
    backend.key_up("cross")
    assert c.states[-1].buttons == {"l1"}
    backend.release_all()
    assert c.states[-1].is_neutral()


def test_native_backend_mouse_is_typed_refusal():
    backend = NativeBackend(StubClient())
    with pytest.raises(UnsupportedStateError):
        backend.mouse_down()


def test_make_backend_stub_is_native_and_connected():
    backend = make_backend("stub")
    assert isinstance(backend, NativeBackend)
    assert backend.supports_state() is True
    ack = backend.set_state(ControllerState(buttons={"ps"}), why="wake menu")
    assert ack.ok


# -- Input sources (PRD §4.10/§4.11: interchangeable front-ends) --------------

def test_manual_source_builds_state_from_presses():
    src = ManualSource()
    src.set_button("cross", True)
    src.set_axis("left_y", -1.0)
    src.set_trigger("r2", 0.5)
    state = src.read_state()
    assert state.buttons == {"cross"}
    assert state.axes == {"left_y": -1.0}
    assert state.triggers == {"r2": 0.5}
    src.set_button("cross", False)
    assert src.read_state().buttons == frozenset()


def test_pump_sends_only_changes_and_they_are_acked_and_logged():
    c = StubClient()
    c.connect()
    logger = MoveLogger()
    c.set_log_sink(logger)
    src = ManualSource()
    pump = StatePump(src, c)
    assert pump.step() is not None  # first read establishes state
    assert pump.step() is None  # unchanged: nothing sent
    src.set_button("triangle", True)
    ack = pump.step(why="panel press")
    assert ack is not None and ack.ok
    assert c.states[-1].buttons == {"triangle"}
    assert logger.records[-1].why == "panel press"


def test_pump_release_forces_neutral():
    c = StubClient()
    c.connect()
    src = ManualSource()
    pump = StatePump(src, c)
    src.set_button("l1", True)
    pump.step()
    pump.release()
    assert c.states[-1].is_neutral()


def test_pump_propagates_typed_errors():
    c = StubClient()
    c.connect()
    src = ManualSource()
    pump = StatePump(src, c)
    src.set_button("cross", True)
    c.fail_next(InputChannelError("gone"))
    with pytest.raises(InputChannelError):
        pump.step()


def test_ps_client_input_without_session_is_typed_refusal(tmp_path):
    # M1 asserted the pre-M2 refusal (InputChannelError: "lands in
    # M2"). M2 made input real; with no live session the refusal is
    # NotConnectedError — still typed-and-logged, never silent (§4.4).
    from gamepad_mcp.client import NotConnectedError
    from gamepad_mcp.client.ps import PsClient

    client = PsClient(plist_path=tmp_path / "absent.plist")
    logger = MoveLogger()
    client.set_log_sink(logger)
    with pytest.raises(NotConnectedError):
        client.send_state(ControllerState(buttons={"cross"}), why="walk test")
    assert logger.records[-1].outcome == "failed"


def test_ps_client_missing_registration_is_typed(tmp_path):
    from gamepad_mcp.client.ps import PsClient
    from gamepad_mcp.client import RegistrationError

    client = PsClient(plist_path=tmp_path / "absent.plist")
    with pytest.raises(RegistrationError):
        client._registration()


def test_sources_are_interchangeable_by_construction():
    # Any InputSource feeds the same pump -> same client -> same log.
    # ManualSource stands in for both the panel (§4.10) and, in M2,
    # the physical pad (§4.11); the contract tested here is the seam.
    c = StubClient()
    c.connect()
    for src in (ManualSource(), ManualSource()):
        pump = StatePump(src, c)
        src.set_button("ps", True)
        assert pump.step() is not None
        pump.release()
