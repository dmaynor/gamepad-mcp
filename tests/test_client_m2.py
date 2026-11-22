"""M2: native PS input, wake, pad source, and the §4.10 panel.

The PsClient tests drive the real send_state code against a
recording fake of the pybind session — the mapping from
ControllerState to libchiaki calls is the contract, and it is tested
exactly. Live-console proof is the M2 rehearsal (BUILDLOG-M2).
"""

from __future__ import annotations

import json
import plistlib
import threading
import urllib.request

import pytest

from gamepad_mcp.client import (
    ControllerState,
    InputChannelError,
    MoveLogger,
    NotConnectedError,
    PadSource,
    PadUnavailableError,
    PanelService,
    SessionDownError,
    StubClient,
    parse_pad_report,
    serve_panel,
)
from gamepad_mcp.client.ps import PsClient, apply_state
from gamepad_mcp.client.sources import HidPadReader


# ---------------------------------------------------------------------
# apply_state: ControllerState -> libchiaki calls
# ---------------------------------------------------------------------


class RecordingCp:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))

        return record

    def is_connected(self):
        return True


def test_apply_state_full():
    cp = RecordingCp()
    state = ControllerState(
        buttons=frozenset({"cross", "triangle"}),
        axes={"left_x": 0.5, "left_y": -1.0},
        triggers={"l2": 0.5},
    )
    apply_state(cp, state)
    names = [c[0] for c in cp.calls]
    assert "press_cross" in names and "press_triangle" in names
    assert "release_circle" in names and "release_square" in names
    assert ("set_left", (512, -1023)) in cp.calls
    assert ("set_right", (0, 0)) in cp.calls
    assert ("set_l2", (128,)) in cp.calls
    assert ("set_r2", (0,)) in cp.calls
    assert cp.calls[-1][0] == "send_feedback_state"


def test_apply_state_neutral_releases_everything():
    cp = RecordingCp()
    apply_state(cp, ControllerState.neutral())
    names = [c[0] for c in cp.calls]
    assert not any(n.startswith("press_") for n in names)
    assert ("set_left", (0, 0)) in cp.calls
    assert ("set_l2", (0,)) in cp.calls and ("set_r2", (0,)) in cp.calls


def test_apply_state_l2_button_means_full_trigger():
    cp = RecordingCp()
    apply_state(cp, ControllerState(buttons=frozenset({"l2"})))
    assert ("set_l2", (255,)) in cp.calls


# ---------------------------------------------------------------------
# PsClient.send_state: ack + log record, typed failures
# ---------------------------------------------------------------------


class FakeSession:
    def __init__(self, cp):
        self.cp_session = cp
        self.is_active = True


def _ps_client_with_cp(cp):
    client = PsClient()
    client._session = FakeSession(cp)
    records = []
    client.set_log_sink(records.append)
    return client, records


def test_send_state_success_acks_and_logs():
    cp = RecordingCp()
    client, records = _ps_client_with_cp(cp)
    ack = client.send_state(
        ControllerState(buttons=frozenset({"cross"})), why="unit test"
    )
    assert ack.ok and ack.latency_ms >= 0
    assert ("press_cross", ()) in cp.calls
    assert len(records) == 1
    assert records[0].outcome == "sent" and records[0].ack is ack


def test_send_state_library_failure_is_typed_and_logged():
    class ExplodingCp(RecordingCp):
        def send_feedback_state(self):
            raise RuntimeError("link dead")

    client, records = _ps_client_with_cp(ExplodingCp())
    with pytest.raises(InputChannelError):
        client.send_state(ControllerState(buttons=frozenset({"cross"})))
    assert records[0].outcome == "failed"
    assert "InputChannelError" in records[0].result


def test_send_state_not_connected_is_typed_and_logged():
    client = PsClient()
    records = []
    client.set_log_sink(records.append)
    with pytest.raises(NotConnectedError):
        client.send_state(ControllerState.neutral())
    assert records[0].outcome == "failed"


def test_send_state_session_not_connected_is_session_down():
    class DeadCp(RecordingCp):
        def is_connected(self):
            return False

    client, records = _ps_client_with_cp(DeadCp())
    with pytest.raises(SessionDownError):
        client.send_state(ControllerState.neutral())
    assert records[0].outcome == "failed"


# ---------------------------------------------------------------------
# PsClient.wake: wakeup packet + poll until Ready
# ---------------------------------------------------------------------


class FakeDisc:
    def __init__(self, state, addr="10.0.0.9"):
        self.state = state
        self.host_addr = addr


class FakeDiscoveryManager:
    sent = []

    def send_wakeup(self, host, regist_key, ps5):
        FakeDiscoveryManager.sent.append((host, regist_key, ps5))


class FakeChiakiPy:
    lib = None  # set below
    states = []

    @staticmethod
    def find_host(name, timeout=0):
        return FakeChiakiPy.states.pop(0)


FakeChiakiPy.lib = type("L", (), {"DiscoveryManager": FakeDiscoveryManager})


@pytest.fixture
def plist_file(tmp_path):
    path = tmp_path / "chiaki.plist"
    with open(path, "wb") as fh:
        plistlib.dump(
            {"registered_hosts.1.rp_regist_key": "0123456789abcdef"}, fh
        )
    return path


def test_wake_sends_packet_and_waits_for_ready(plist_file, monkeypatch):
    FakeDiscoveryManager.sent = []
    FakeChiakiPy.states = [FakeDisc("Standby"), FakeDisc("Ready")]
    monkeypatch.setattr(PsClient, "_lib", staticmethod(lambda: FakeChiakiPy))
    client = PsClient(plist_path=plist_file)
    client.wake(timeout=10)
    assert FakeDiscoveryManager.sent == [
        ("10.0.0.9", "0123456789abcdef", True)
    ]


def test_wake_already_ready_sends_nothing(plist_file, monkeypatch):
    FakeDiscoveryManager.sent = []
    FakeChiakiPy.states = [FakeDisc("Ready")]
    monkeypatch.setattr(PsClient, "_lib", staticmethod(lambda: FakeChiakiPy))
    PsClient(plist_path=plist_file).wake(timeout=5)
    assert FakeDiscoveryManager.sent == []


# ---------------------------------------------------------------------
# Pad report parsing + PadSource
# ---------------------------------------------------------------------


def _report(byte5=0, byte6=0, byte7=0, sticks=(128, 128, 128, 128), trig=(0, 0)):
    return bytes(
        [0x01, *sticks, byte5, byte6, byte7, trig[0], trig[1]] + [0] * 54
    )


def test_parse_pad_report():
    data = _report(
        byte5=0x21,  # hat=1 (up-right) + cross
        byte6=0x81,  # L1 + R3
        byte7=0x01,  # PS
        sticks=(128, 0, 255, 128),
        trig=(255, 0),
    )
    state = parse_pad_report(data)
    assert state.buttons == frozenset(
        {"dpad_up", "dpad_right", "cross", "l1", "r3", "ps"}
    )
    assert state.axes["left_x"] == 0.0
    assert state.axes["left_y"] == -1.0  # stick up is -1 (libchiaki convention)
    assert state.axes["right_x"] == pytest.approx(127 / 128)
    assert state.triggers == {"l2": 1.0, "r2": 0.0}


def test_parse_pad_report_deadzone_and_short():
    state = parse_pad_report(_report(sticks=(130, 128, 128, 128)))
    assert state.axes["left_x"] == 0.0
    with pytest.raises(PadUnavailableError):
        parse_pad_report(b"\x01\x02")


def test_parse_pad_report_bluetooth_offset():
    payload = _report(byte5=0x20)  # cross
    state = parse_pad_report(bytes([0x31, 0x00]) + payload[1:], offset=1)
    # offset is applied to the payload start; byte layout shifts by the
    # caller's offset — here we emulate: payload begins after 0x31.
    assert "cross" in state.buttons


class FakeReader:
    def __init__(self, state=None, error=None):
        self._state = state
        self._error = error

    def probe(self):
        return self._error is None

    def read(self):
        if self._error is not None:
            raise self._error
        return self._state

    def close(self):
        pass


def test_pad_source_passthrough_and_typed_gap():
    want = ControllerState(buttons=frozenset({"cross"}))
    source = PadSource(reader=FakeReader(state=want))
    assert source.probe() is True
    assert source.read_state() == want
    gap = PadSource(reader=FakeReader(error=PadUnavailableError("no pad")))
    assert gap.probe() is False
    with pytest.raises(PadUnavailableError):
        gap.read_state()


class FakeHidDevice:
    opened = False

    def __init__(self, info):
        self.info = info

    def open(self):
        FakeHidDevice.opened = True

    def set_nonblocking(self, _flag):
        pass

    def read(self, _n):
        return list(_report(byte5=0x20))

    def close(self):
        pass


class FakeHidModule:
    def __init__(self, infos):
        self._infos = infos

    def get_all_device_infos(self, vid, pid):
        return [i for i in self._infos if (i.vendor_id, i.product_id) == (vid, pid)]

    HidDevice = FakeHidDevice


def test_hid_reader_finds_and_reads():
    info = type(
        "Info", (), {"vendor_id": 0x054C, "product_id": 0x0CE6}
    )()
    reader = HidPadReader(hid_module=FakeHidModule([info]))
    assert reader.probe() is True
    state = reader.read()
    assert "cross" in state.buttons
    empty = HidPadReader(hid_module=FakeHidModule([]))
    assert empty.probe() is False
    with pytest.raises(PadUnavailableError):
        empty.read()


# ---------------------------------------------------------------------
# Panel service + HTTP surface (StubClient behind it)
# ---------------------------------------------------------------------


@pytest.fixture
def panel():
    client = StubClient()
    client.connect()
    service = PanelService(client, logger=MoveLogger())
    return service, client


def test_panel_post_state_flows_through_pump(panel):
    service, client = panel
    result = service.post_state({"buttons": ["cross"]})
    assert result["ok"] and "latency_ms" in result
    assert client.states[-1].buttons == frozenset({"cross"})
    assert service.post_state({"buttons": ["cross"]}) == {
        "ok": True,
        "unchanged": True,
    }
    assert service.release()["ok"]


def test_panel_typed_error_is_rendered_not_hidden(panel):
    service, client = panel
    client.fail_next(InputChannelError("boom"))
    result = service.post_state({"buttons": ["triangle"]})
    assert result == {
        "ok": False,
        "error": "InputChannelError",
        "detail": "boom",
    }
    records = service.log_records()
    assert records[-1]["outcome"] == "failed"


def test_panel_http_surface(panel):
    service, _client = panel
    server = serve_panel(service, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            page = resp.read().decode()
        assert "RELEASE ALL" in page and "stickL" in page
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/state",
            data=json.dumps({"buttons": ["square"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert json.loads(resp.read())["ok"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status") as resp:
            assert json.loads(resp.read())["connected"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/log") as resp:
            records = json.loads(resp.read())["records"]
        assert any("square" in r["text"] for r in records)
    finally:
        server.shutdown()
        server.server_close()
