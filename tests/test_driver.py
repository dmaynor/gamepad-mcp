"""Driver tests: minimap vision on synthetic frames + the steering law.

The synthetic frames mimic the real HUD geometry calibrated on
2026-11-21 GTA VI frames (1280x640): a dark minimap rectangle in the
bottom-left with a teal waypoint pin on its rim at a known bearing.
The detector must recover that bearing; the law must turn it into the
right stick pulse.
"""

import math

import pytest
from PIL import Image, ImageDraw

from gamepad_mcp.driver import (
    MAP_PROPER,
    MinimapVision,
    SteeringLaw,
    Sight,
    quantize,
)

W, H = 1280, 640
TEAL = (20, 230, 190)


def frame_with_pin(bearing_deg: float | None) -> Image.Image:
    """Dark frame + minimap; pin (if any) on the rim at bearing_deg,
    where 0 = straight ahead (up), + = to the right."""
    im = Image.new("RGB", (W, H), (8, 8, 14))
    draw = ImageDraw.Draw(im)
    x0, y0 = int(MAP_PROPER[0] * W), int(MAP_PROPER[1] * H)
    x1, y1 = int(MAP_PROPER[2] * W), int(MAP_PROPER[3] * H)
    draw.rectangle([x0, y0, x1, y1], fill=(30, 32, 48))
    if bearing_deg is not None:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = (x1 - x0) / 2 - 4, (y1 - y0) / 2 - 4
        rad = math.radians(bearing_deg)
        px, py = cx + rx * math.sin(rad), cy - ry * math.cos(rad)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=TEAL)
    return im


@pytest.mark.parametrize("bearing", [0, 45, -45, 90, -90, 135, 180])
def test_vision_recovers_pin_bearing(bearing):
    sight = MinimapVision().sense(frame_with_pin(bearing))
    assert sight.found
    assert sight.bearing_deg == pytest.approx(bearing, abs=14)


def test_vision_no_pin():
    sight = MinimapVision().sense(frame_with_pin(None))
    assert not sight.found


def test_vision_ignores_tiny_teal_specks():
    im = frame_with_pin(None)
    draw = ImageDraw.Draw(im)
    draw.rectangle([150, 520, 151, 521], fill=TEAL)  # 4 px speck < MIN_PIN_PIXELS
    sight = MinimapVision().sense(im)
    assert not sight.found


def test_quantize():
    assert quantize(0)[0] == "up"
    assert quantize(20)[0] == "up"
    assert quantize(30)[0] == "up-right"
    assert quantize(-30)[0] == "up-left"
    assert quantize(100)[0] == "right"
    assert quantize(179)[0] == "down"
    assert quantize(-179)[0] == "down"
    assert quantize(360 + 44)[0] == "up-right"


def sight_at(bearing):
    return Sight(found=True, bearing_deg=bearing, pixels=100)


def test_law_steers_toward_bearing():
    law = SteeringLaw()
    # facing up, target 40 deg right -> up-right
    assert law.decide(sight_at(40), "waypoint")[0] == "up-right"
    # now moving up-right (45); target dead ahead -> keep up-right
    assert law.decide(sight_at(0), "waypoint")[0] == "up-right"
    # target 90 left of heading -> 45-90 = -45 -> up-left
    assert law.decide(sight_at(-90), "waypoint")[0] == "up-left"


def test_law_forward_mode():
    law = SteeringLaw()
    assert law.decide(Sight(found=False), "forward") == ("up", 240)


def test_law_lost_sight_stops():
    law = SteeringLaw()
    law.decide(sight_at(0), "waypoint")  # get moving
    lost = Sight(found=False)
    for _ in range(15):
        assert law.decide(lost, "waypoint") is not None  # coasts briefly
    assert law.decide(lost, "waypoint") is None  # then gives up


def test_law_never_moves_blind_from_standstill():
    law = SteeringLaw()
    assert law.decide(Sight(found=False), "waypoint") is None


# ----------------------------------------------------------------------
# Driver wiring: command -> tick -> server call, with no deadlock
# (regression: set_command once deadlocked on its own status() call)
# ----------------------------------------------------------------------

from gamepad_mcp.driver import Driver, FileFrameSource, MCPClient  # noqa: E402


class _FakeResp:
    def __init__(self, body=b""):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_mcp_notification_carries_no_id(monkeypatch):
    # Regression (live, 2026-11-21): an id on notifications/initialized
    # made the server answer -32602 and cost the first tick per session.
    import json as _json

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = req.data
        return _FakeResp(b"")  # notifications answer 202, empty body

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MCPClient("http://127.0.0.1:9/mcp")
    assert client._post({"method": "notifications/initialized",
                         "params": {}}, notification=True) == {}
    assert "id" not in _json.loads(sent["body"])
    client._post({"method": "tools/call", "params": {}}, notification=False)
    assert "id" in _json.loads(sent["body"])


class StubClient:
    def __init__(self):
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {}


def test_driver_tick_steers_and_logs(tmp_path):
    frame_path = tmp_path / "frame.jpg"
    frame_with_pin(-45).save(frame_path)
    client = StubClient()
    driver = Driver(client, FileFrameSource(str(frame_path)),
                    tmp_path / "session.jsonl")
    result = driver.set_command("waypoint", 30)
    assert result["mode"] == "waypoint"
    driver._tick()
    assert client.calls, "tick issued no input"
    tool, args = client.calls[-1]
    assert tool == "hold_stick"
    assert args["direction"] == "up-left"  # pin at -45 from standstill
    log_lines = (tmp_path / "session.jsonl").read_text().strip().splitlines()
    assert len(log_lines) == 1
    result = driver.set_command("stop", 0)
    assert result["mode"] == "stop"
    assert client.calls[-1][0] == "release_all"
