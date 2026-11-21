"""gamepad-driver: the reflex layer that plays between planner decisions.

Why this exists (measured 2026-11-21): a planner model driving through
gamepad-mcp one tool call at a time runs at roughly one move per minute —
frame, reason, act — while a game needs control decisions several times
per second. Cars drove off mid-thought; a character walked into a bay.
The fix is the split every game-AI and robotics stack uses:

  planner (slow, smart)  -- steering commands -->  driver (fast, dumb)
  "take me to the waypoint"                         minimap -> stick, 5 Hz

The driver is deliberately simple. It knows one sense (the GTA minimap)
and one skill (steer toward the waypoint pin). It takes steering
commands over a tiny HTTP API and turns them into hold_stick calls
against a gamepad-mcp server, closing the loop on every tick:

  1. grab a frame (Mac window capture, or a file for replay/testing)
  2. crop the minimap, find the teal waypoint pin, measure its bearing
     relative to the player (the minimap is player-up, so the bearing
     is relative to the character's facing)
  3. steer: new stick direction = current movement direction + bearing
     error, quantized to the 8 stick directions, pulsed for one tick

No world frame is needed anywhere: everything is relative, and the loop
re-senses 5x per second, so errors correct themselves the way they do
when a person steers by a rotating minimap.

Safety, in the same spirit as the Controller's hold caps:
  * every command carries a TTL (default 90 s) and then auto-stops;
  * if the pin is lost for more than ~3 s of ticks, the driver stops;
  * stop / TTL expiry / shutdown all call release_all on the server;
  * the MCP client uses short timeouts so a hung server stalls at most
    one tick, never the loop.

Data: every tick is appended to a JSONL session log (timestamp, sight,
action). With --save-frames, each tick's frame is written next to the
log, giving an aligned (frame -> input) record — the seed dataset for
training a learned driver later.

Run it on the same machine as the gamepad-mcp server:

  gamepad-driver --frames mac \
      --server-url http://<tailscale-ip>:8765/mcp --server-token <token>

Then steer it from anywhere that can reach it:

  curl -X POST localhost:8771/command -d '{"mode": "waypoint", "ttl_s": 120}'
  curl localhost:8771/status
  curl -X POST localhost:8771/command -d '{"mode": "stop"}'
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ----------------------------------------------------------------------
# Vision: find the waypoint pin in the minimap
# ----------------------------------------------------------------------

# Minimap geometry as fractions of the frame (left, top, right, bottom),
# calibrated on real GTA VI frames (1280x640) on 2026-11-21. SEARCH_BOX
# includes a margin above the minimap proper: when the target is far,
# the game pins the marker on the minimap's rim, half outside the box.
SEARCH_BOX = (0.083, 0.760, 0.216, 0.917)
MAP_PROPER = (0.083, 0.789, 0.216, 0.917)  # the minimap itself

MIN_PIN_PIXELS = 8  # smaller teal specks are reflections / HUD noise


def is_waypoint_teal(r: int, g: int, b: int) -> bool:
    """The waypoint pin's teal: bright green-blue, little red."""
    return g > 140 and b > 110 and r < 110 and g > r + 60 and b > r + 40


@dataclass
class Sight:
    found: bool
    bearing_deg: float = 0.0  # + = target is to the right of facing
    pixels: int = 0
    centroid: tuple | None = None  # (x, y) within the search crop

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "bearing_deg": round(self.bearing_deg, 1),
            "pixels": self.pixels,
            "centroid": list(self.centroid) if self.centroid else None,
        }


class MinimapVision:
    def __init__(self, search_box=SEARCH_BOX, map_proper=MAP_PROPER):
        self.search_box = search_box
        self.map_proper = map_proper

    def sense(self, frame) -> Sight:
        """frame: a PIL Image (RGB). Returns the pin bearing, if seen."""
        w, h = frame.size
        x0, y0 = int(self.search_box[0] * w), int(self.search_box[1] * h)
        x1, y1 = int(self.search_box[2] * w), int(self.search_box[3] * h)
        crop = frame.crop((x0, y0, x1, y1))
        px = crop.load()
        pts = [
            (x, y)
            for y in range(crop.height)
            for x in range(crop.width)
            if is_waypoint_teal(*px[x, y])
        ]
        if len(pts) < MIN_PIN_PIXELS:
            return Sight(found=False, pixels=len(pts))
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        # Player reference: center of the minimap proper, expressed in
        # crop coordinates. The minimap is player-up, so "up" in the
        # crop is the character's facing.
        pcx = crop.width / 2
        top_frac = (self.map_proper[1] - self.search_box[1]) / (
            self.search_box[3] - self.search_box[1]
        )
        bot_frac = (self.map_proper[3] - self.search_box[1]) / (
            self.search_box[3] - self.search_box[1]
        )
        pcy = (top_frac + bot_frac) / 2 * crop.height
        # Normalize by the minimap's half-extents before measuring the
        # angle: the map is much wider than tall, and the game places
        # the rim pin by casting the target direction to the border, so
        # raw pixel angles are stretched toward the horizontal.
        rx = crop.width / 2
        ry = (bot_frac - top_frac) / 2 * crop.height
        bearing = math.degrees(
            math.atan2((cx - pcx) / rx, -(cy - pcy) / ry)
        )
        return Sight(found=True, bearing_deg=bearing, pixels=len(pts),
                     centroid=(round(cx), round(cy)))


# ----------------------------------------------------------------------
# Steering law: bearing error -> one stick pulse
# ----------------------------------------------------------------------

DIRECTIONS = [
    "up", "up-right", "right", "down-right",
    "down", "down-left", "left", "up-left",
]
PULSE_MS = 240        # one tick of stick per decision
LOST_LIMIT = 15       # ticks without a sighting (~3 s at 5 Hz) -> stop


def quantize(deg: float) -> tuple:
    """Angle (0 = up, clockwise +) -> (direction name, snapped angle)."""
    snapped = int(round(deg / 45.0)) % 8
    return DIRECTIONS[snapped], snapped * 45.0


class SteeringLaw:
    """Stateful: tracks the current movement direction in camera space.

    The stick is camera-relative and the character turns to face his
    movement direction, so while moving, heading == last stick angle.
    The minimap reports the target relative to heading, hence:
        new stick angle = last stick angle + bearing error.
    """

    def __init__(self):
        self.stick_deg = 0.0
        self.lost_ticks = 0
        self.moving = False

    def reset(self):
        self.stick_deg = 0.0
        self.lost_ticks = 0
        self.moving = False

    def decide(self, sight: Sight, mode: str) -> tuple | None:
        """Returns (direction, hold_ms), or None to stop."""
        if mode == "forward":
            self.stick_deg = 0.0
            self.moving = True
            return ("up", PULSE_MS)
        # mode == "waypoint"
        if not sight.found:
            self.lost_ticks += 1
            if self.lost_ticks > LOST_LIMIT or not self.moving:
                return None
            name, _ = quantize(self.stick_deg)
            return (name, PULSE_MS)
        self.lost_ticks = 0
        self.moving = True
        name, snapped = quantize(self.stick_deg + sight.bearing_deg)
        self.stick_deg = snapped
        return (name, PULSE_MS)


# ----------------------------------------------------------------------
# MCP client: just enough streamable-HTTP JSON-RPC to call tools
# ----------------------------------------------------------------------


class DriverError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 2.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self._session: str | None = None
        self._next_id = 0

    def _post(self, payload: dict, session: bool = True,
              notification: bool = False) -> dict:
        payload.setdefault("jsonrpc", "2.0")
        if notification:
            # A JSON-RPC notification MUST NOT carry an id: with one,
            # the server validates it as a request, finds no matching
            # request type, and answers -32602 (seen live 2026-11-21,
            # it cost the first tick of every session).
            payload.pop("id", None)
        else:
            self._next_id += 1
            payload.setdefault("id", self._next_id)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if session and self._session:
            req.add_header("mcp-session-id", self._session)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self._session = sid
                body = resp.read().decode()
        except Exception as exc:
            raise DriverError(f"MCP request failed: {exc}") from exc
        if not body.strip():
            return {}  # notifications answer 202 with an empty body
        # Streamable HTTP answers plain JSON or an SSE stream of them.
        result = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    pass
        if result is None:
            try:
                result = json.loads(body)
            except json.JSONDecodeError as exc:
                raise DriverError(f"unparseable MCP response: {body[:200]}") from exc
        if "error" in result:
            raise DriverError(f"MCP error: {result['error']}")
        return result.get("result", {})

    def connect(self) -> dict:
        result = self._post(
            {
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "gamepad-driver", "version": "0.2.0"},
                },
            },
            session=False,
        )
        self._post({"method": "notifications/initialized", "params": {}},
                   session=True, notification=True)
        return result

    def call(self, tool: str, arguments: dict) -> dict:
        if self._session is None:
            self.connect()
        result = self._post(
            {"method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        )
        # Tool results wrap the payload as text content; unwrap if so.
        content = result.get("content")
        if isinstance(content, list) and content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except (json.JSONDecodeError, KeyError):
                return result
        return result


# ----------------------------------------------------------------------
# Frame sources
# ----------------------------------------------------------------------


class FileFrameSource:
    """Re-reads an image file each grab (or cycles a directory of them).

    Works with anything that keeps a fresh screenshot at a path — and
    with recorded frames for offline testing.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._frames: list = []
        self._idx = 0
        if self.path.is_dir():
            self._frames = sorted(
                p for p in self.path.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg")
            )

    def grab(self):
        from PIL import Image

        if self._frames:
            path = self._frames[self._idx % len(self._frames)]
            self._idx += 1
        else:
            path = self.path
        with Image.open(path) as im:
            return im.convert("RGB")


class MacWindowFrameSource:
    """Captures the chiaki-ng window on macOS via screencapture."""

    def __init__(self, owner: str = "chiaki-ng"):
        self.owner = owner
        self._tmp = Path(tempfile.gettempdir()) / "gamepad-driver-frame.png"

    def _window_id(self) -> int:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        best, best_area = None, 0
        for win in windows or []:
            if win.get("kCGWindowOwnerName") != self.owner:
                continue
            bounds = win.get("kCGWindowBounds", {})
            area = bounds.get("Width", 0) * bounds.get("Height", 0)
            if area > best_area:
                best, best_area = win.get("kCGWindowNumber"), area
        if best is None:
            raise DriverError(f"no on-screen window owned by {self.owner!r}")
        return best

    def grab(self):
        from PIL import Image

        wid = self._window_id()
        subprocess.run(
            ["screencapture", f"-l{wid}", "-x", "-o", str(self._tmp)],
            check=True, timeout=5,
        )
        with Image.open(self._tmp) as im:
            return im.convert("RGB")


def make_frame_source(kind: str, path: str | None):
    if kind == "mac":
        return MacWindowFrameSource()
    if kind == "file":
        if not path:
            raise DriverError("--frames file requires --frame-path")
        return FileFrameSource(path)
    raise DriverError(f"unknown frame source {kind!r}")


# ----------------------------------------------------------------------
# The driver loop + command API
# ----------------------------------------------------------------------


@dataclass
class Command:
    mode: str = "stop"  # "waypoint" | "forward" | "stop"
    expires_at: float = 0.0


@dataclass
class DriverState:
    command: Command = field(default_factory=Command)
    last_sight: Sight | None = None
    last_action: dict | None = None
    ticks: int = 0
    started_at: float = field(default_factory=time.time)
    note: str = ""


class Driver:
    def __init__(self, client: MCPClient, frames, log_path: Path,
                 save_frames_dir: Path | None = None, tick_s: float = 0.2):
        self.client = client
        self.frames = frames
        self.vision = MinimapVision()
        self.law = SteeringLaw()
        self.state = DriverState()
        self.tick_s = tick_s
        # RLock: status() is called both standalone and from inside
        # set_command's critical section (a plain Lock deadlocked the
        # command API on its first live test, 2026-11-21).
        self._lock = threading.RLock()
        self._stopped = False
        self._released = True  # nothing held right now
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log_path, "a", buffering=1)
        self.log_path = log_path
        self.save_frames_dir = save_frames_dir
        if save_frames_dir:
            save_frames_dir.mkdir(parents=True, exist_ok=True)
        atexit.register(self.shutdown)

    # -- command side ---------------------------------------------------
    def set_command(self, mode: str, ttl_s: float) -> dict:
        with self._lock:
            if mode == "stop":
                self.state.command = Command("stop", 0.0)
                self.law.reset()
            else:
                self.state.command = Command(mode, time.time() + ttl_s)
                self.law.reset()
                self.state.note = ""
            result = self.status()
        if mode == "stop":
            self._release()  # network call: never under the state lock
        return result

    def status(self) -> dict:
        with self._lock:
            cmd = self.state.command
            return {
                "mode": cmd.mode,
                "ttl_remaining_s": max(0.0, round(cmd.expires_at - time.time(), 1))
                if cmd.mode != "stop" else 0.0,
                "ticks": self.state.ticks,
                "uptime_s": round(time.time() - self.state.started_at, 1),
                "last_sight": self.state.last_sight.as_dict()
                if self.state.last_sight else None,
                "last_action": self.state.last_action,
                "note": self.state.note,
                "log": str(self.log_path),
            }

    # -- actuation ------------------------------------------------------
    def _release(self):
        if not self._released:
            try:
                self.client.call("release_all", {})
            except DriverError:
                pass
            self._released = True

    def _act(self, direction: str, hold_ms: int) -> dict:
        result = self.client.call(
            "hold_stick", {"stick": "left", "direction": direction,
                           "hold_ms": hold_ms}
        )
        self._released = False
        return result

    # -- main loop ------------------------------------------------------
    def run(self):
        while not self._stopped:
            t0 = time.time()
            try:
                self._tick()
            except DriverError as exc:
                with self._lock:
                    self.state.note = f"error: {exc}"
                self._release()
            except Exception as exc:  # never let one bad frame kill the loop
                with self._lock:
                    self.state.note = f"error: {type(exc).__name__}: {exc}"
            elapsed = time.time() - t0
            time.sleep(max(0.0, self.tick_s - elapsed))

    def _tick(self):
        with self._lock:
            cmd = self.state.command
            active = cmd.mode != "stop" and time.time() < cmd.expires_at
            mode = cmd.mode
        if not active:
            self._release()
            return
        frame = self.frames.grab()
        sight = self.vision.sense(frame)
        action = self.law.decide(sight, mode)
        record = {
            "t": round(time.time(), 3),
            "mode": mode,
            "sight": sight.as_dict(),
            "action": None,
        }
        if self.save_frames_dir is not None:
            name = f"frame-{self.state.ticks:06d}.png"
            frame.save(self.save_frames_dir / name)
            record["frame"] = name
        if action is None:
            self._release()
            with self._lock:
                self.state.note = "stopped: waypoint lost"
                self.state.command = Command("stop", 0.0)
            record["note"] = "auto-stop: sight lost"
        else:
            direction, hold_ms = action
            self._act(direction, hold_ms)
            record["action"] = {"direction": direction, "hold_ms": hold_ms}
        with self._lock:
            self.state.last_sight = sight
            self.state.last_action = record["action"]
            self.state.ticks += 1
            if action is not None:
                self.state.note = ""  # a good tick clears the last error
        self._log.write(json.dumps(record) + "\n")

    def shutdown(self):
        self._stopped = True
        self._release()


def make_http_server(driver: Driver, host: str, port: int,
                     token: str | None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep stdout for real news
            pass

        def _authorized(self) -> bool:
            return not token or self.headers.get("Authorization") == f"Bearer {token}"

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if self.path == "/status":
                return self._json(200, driver.status())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if self.path != "/command":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                mode = payload.get("mode", "stop")
                if mode not in ("waypoint", "forward", "stop"):
                    raise ValueError(f"unknown mode {mode!r}")
                ttl = float(payload.get("ttl_s", 90))
                ttl = max(1.0, min(ttl, 3600.0))
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, driver.set_command(mode, ttl))

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="gamepad-driver",
        description="Reflex-layer driver: takes steering commands and "
                    "steers by the GTA minimap waypoint at 5 Hz.",
    )
    parser.add_argument("--server-url",
                        default=os.environ.get("GAMEPAD_URL",
                                               "http://127.0.0.1:8765/mcp"))
    parser.add_argument("--server-token",
                        default=os.environ.get("GAMEPAD_TOKEN"))
    parser.add_argument("--frames", choices=["mac", "file"], default="mac")
    parser.add_argument("--frame-path", default=None,
                        help="image file (re-read each tick) or directory "
                             "of frames (cycled) for --frames file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--token", default=os.environ.get("DRIVER_TOKEN"),
                        help="require this bearer token on the command API")
    parser.add_argument("--tick", type=float, default=0.2,
                        help="control-loop period in seconds (default 0.2)")
    parser.add_argument("--log", default=None,
                        help="JSONL session log path (default: "
                             "~/gamepad-driver-logs/session-<time>.jsonl)")
    parser.add_argument("--save-frames", default=None,
                        help="directory to write each tick's frame into "
                             "(builds an aligned frame->input dataset)")
    args = parser.parse_args(argv)

    log_path = Path(args.log) if args.log else (
        Path.home() / "gamepad-driver-logs"
        / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    client = MCPClient(args.server_url, args.server_token)
    frames = make_frame_source(args.frames, args.frame_path)
    driver = Driver(client, frames, log_path,
                    Path(args.save_frames) if args.save_frames else None,
                    tick_s=args.tick)
    server = make_http_server(driver, args.host, args.port, args.token)
    print(f"gamepad-driver: command API on http://{args.host}:{args.port} "
          f"(POST /command, GET /status)", file=sys.stderr)
    print(f"gamepad-driver: session log {log_path}", file=sys.stderr)
    loop = threading.Thread(target=driver.run, daemon=True)
    loop.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        driver.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
