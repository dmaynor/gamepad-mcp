"""M3: the MCP server on the native client (David: "i want the new
stuff", 2026-11-22).

The wiring under test: make_backend("ps") builds a NativeBackend
over PsClient WITHOUT connecting (lazy session ownership); the
NativeTools translation layer turns press/hold_stick/release_all
into controller states whose results carry the InputAck — or the
typed error, verbatim — and answers keystroke/mouse tools with a
typed unsupported error; the server dispatches to that layer when
its backend is native.

Everything runs over StubClient (plus failure-injecting subclasses);
no console, no chiaki_py, no numpy — the suite runs in every venv.
"""

import asyncio
import hashlib
import json

import pytest

from gamepad_mcp.backends import make_backend
from gamepad_mcp.backends.native import NativeBackend
from gamepad_mcp.client.errors import InputChannelError, SessionDownError
from gamepad_mcp.client.state import ControllerState
from gamepad_mcp.client.stub import StubClient
from gamepad_mcp.native_tools import NativeTools

IN_USE = (
    "session connect failed (SessionConnectError): "
    "Remote Play on Console is already in use"
)


class RefusingClient(StubClient):
    """A client whose console refuses every session request — the
    live condition on the night M3 was built (the panel owns the
    console's one session)."""

    def connect(self) -> None:
        raise SessionDownError(IN_USE)


class ReleaseDiesClient(StubClient):
    """First send lands; the second (the release) dies typed."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def send_state(self, state, why=""):
        self.calls += 1
        if self.calls == 2:
            raise InputChannelError("controller channel died mid-press")
        return super().send_state(state, why=why)


def tools_for(client) -> NativeTools:
    return NativeTools(NativeBackend(client))


# -- backend selection + lazy connect -----------------------------------


def test_make_backend_ps_is_native_and_lazy():
    backend = make_backend("ps")
    assert isinstance(backend, NativeBackend)
    assert backend.client.name == "ps-chiaki-py"
    # Constructed without a session: nothing was connected, and
    # asking did not import the streaming library or touch the LAN.
    assert backend.client.connected is False
    assert backend.client._session is None


def test_native_backend_connects_lazily_on_first_send():
    client = StubClient()  # deliberately NOT connected
    backend = NativeBackend(client)
    ack = backend.set_state(ControllerState(buttons={"cross"}), why="first")
    assert ack.ok
    assert client.connected is True
    assert client.states[-1].buttons == {"cross"}
    assert backend.current_state.buttons == {"cross"}


def test_lazy_connect_refusal_raises_typed_verbatim():
    backend = NativeBackend(RefusingClient())
    with pytest.raises(SessionDownError) as caught:
        backend.set_state(ControllerState.neutral())
    assert "already in use" in str(caught.value)


# -- translation: press / hold_stick / release_all ------------------------


def test_press_sends_then_releases_and_carries_both_acks():
    client = StubClient()
    tools = tools_for(client)
    result = tools.press("cross", hold_ms=1)
    assert result["ok"] is True
    assert result["ack"]["ok"] is True
    assert result["ack"]["latency_ms"] >= 0
    assert result["release_ack"]["ok"] is True
    assert client.states[0].buttons == {"cross"}
    assert client.states[-1].buttons == frozenset()
    assert tools.backend.current_state.is_neutral()


def test_press_maps_share_to_create():
    client = StubClient()
    result = tools_for(client).press("share", hold_ms=1)
    assert result["ok"] is True
    assert result["client_button"] == "create"
    assert client.states[0].buttons == {"create"}


def test_press_unknown_button_sends_nothing_and_never_connects():
    client = StubClient()
    result = tools_for(client).press("bogus", hold_ms=1)
    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert client.states == []
    assert client.connected is False  # no session spent on a bad name


def test_press_refusal_is_the_typed_error_verbatim():
    tools = tools_for(RefusingClient())
    result = tools.press("cross", hold_ms=1)
    assert result["ok"] is False
    assert result["error"] == {"type": "SessionDownError", "message": IN_USE}
    # ...and the refusal is on the status record, with the
    # session-ownership note (the panel is the other owner).
    status = tools.client_status()
    assert status["connected"] is False
    assert status["last_error"] == {"type": "SessionDownError",
                                     "message": IN_USE}
    assert "panel" in status["session_ownership"]


def test_press_release_failure_reports_the_press_ack_and_the_error():
    client = ReleaseDiesClient()
    result = tools_for(client).press("triangle", hold_ms=1)
    assert result["ok"] is False
    assert result["phase"] == "release"
    assert result["ack"]["ok"] is True  # the press DID land
    assert result["error"]["type"] == "InputChannelError"
    assert client.states[0].buttons == {"triangle"}


def test_hold_stick_axes_diagonal_and_prior_state_restored():
    client = StubClient()
    tools = tools_for(client)
    tools.backend.set_state(ControllerState(buttons={"l1"}), why="setup")
    result = tools.hold_stick("right", "down-left", hold_ms=1)
    assert result["ok"] is True
    assert result["axes"] == {"right_x": -1.0, "right_y": 1.0}
    during = client.states[-2]
    assert during.axes == {"right_x": -1.0, "right_y": 1.0}
    assert during.buttons == {"l1"}  # the held button rides along
    assert client.states[-1] == ControllerState(buttons={"l1"})


def test_hold_stick_up_is_negative_y():
    client = StubClient()
    result = tools_for(client).hold_stick("left", "up", hold_ms=1)
    assert result["ok"] is True
    assert client.states[0].axes == {"left_x": 0.0, "left_y": -1.0}


def test_release_all_sends_neutral_with_ack():
    client = StubClient()
    tools = tools_for(client)
    tools.backend.set_state(ControllerState(buttons={"r1"}), why="setup")
    result = tools.release_all()
    assert result["ok"] is True
    assert result["ack"]["ok"] is True
    assert client.states[-1].is_neutral()


# -- keystroke/mouse tools: typed unsupported, never emulated ------------


@pytest.mark.parametrize(
    "tool", ["key_tap", "key_down", "key_up", "combo",
             "mouse_move", "mouse_click", "profiles", "set_profile"]
)
def test_keymap_only_tools_are_typed_unsupported(tool):
    client = StubClient()
    result = tools_for(client).unsupported(tool)
    assert result["ok"] is False
    assert result["error"]["type"] == "UnsupportedStateError"
    assert "native-stub" in result["error"]["message"]
    assert client.states == []  # nothing emulated, nothing sent


# -- the new tools --------------------------------------------------------


def test_frame_tool_saves_and_reports_sha256(tmp_path):
    client = StubClient()  # not connected: frame must lazy-connect
    tools = NativeTools(NativeBackend(client), frame_dir=str(tmp_path))
    result = tools.frame()
    assert result["ok"] is True
    assert client.connected is True
    data = open(result["path"], "rb").read()
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["width"] == 1 and result["height"] == 1
    assert result["frame_ts"] > 0
    status = tools.client_status()
    assert status["frame_ts"] == result["frame_ts"]
    assert status["frame_stale"] is False


def test_wake_and_disconnect_tools():
    client = StubClient()
    tools = tools_for(client)
    assert tools.client_status()["connected"] is False
    woke = tools.wake()
    assert woke["ok"] is True and woke["connected"] is True
    gone = tools.disconnect()
    assert gone == {"ok": True, "disconnected": True, "connected": False}
    # A second disconnect claims nothing that did not happen.
    again = tools.disconnect()
    assert again == {"ok": True, "disconnected": False, "connected": False}
    assert tools.backend.current_state.is_neutral()


# -- server dispatch --------------------------------------------------------


@pytest.fixture()
def server_env(monkeypatch):
    from gamepad_mcp import server

    def _set(kind):
        monkeypatch.setenv("GAMEPAD_BACKEND", kind)
        server._backend = None
        server._controller = None
        server._native_tools = None
        return server.build_server()

    yield _set
    server._backend = None
    server._controller = None
    server._native_tools = None


def call(mcp, name, arguments=None):
    async def _call():
        return await mcp.call_tool(name, arguments or {})

    content = asyncio.run(_call())
    return json.loads(content[0].text)


def test_server_dispatches_native_when_backend_is_native(server_env):
    mcp = server_env("stub")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"frame", "client_status", "wake", "disconnect"} <= names

    pressed = call(mcp, "press", {"button": "cross", "hold_ms": 1})
    assert pressed["ok"] is True and "ack" in pressed

    tapped = call(mcp, "key_tap", {"key": "w"})
    assert tapped["ok"] is False
    assert tapped["error"]["type"] == "UnsupportedStateError"

    status = call(mcp, "client_status")
    assert status["connected"] is True  # the press's lazy connect
    assert status["client"] == "stub"


def test_server_new_tools_unsupported_on_keymap_backend(server_env):
    mcp = server_env("null")
    result = call(mcp, "frame")
    assert result["ok"] is False
    assert result["error"]["type"] == "UnsupportedStateError"
    assert result["backend"] == "null"
    # The legacy path is untouched: press still speaks keymap.
    pressed = call(mcp, "press", {"button": "cross", "hold_ms": 1})
    assert pressed["button"] == "cross"
