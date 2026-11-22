"""The two-panel diagnostic surface (PRD §4.10).

Panel one is the live frame view; panel two is a DualSense whose
buttons, sticks, and triggers send real inputs through the client's
native input API. Every press renders its acknowledgement and
measured latency beside the input log. This page is how a human
tests the input path end to end — it is the acceptance-walk surface
(PRD §9.7) — and it is a *thin client of the same API*: the page
holds a ManualSource, a StatePump turns its changes into
client.send_state calls, frames come from client.grab_frame, and the
log lines are the client's own MoveLogger records. No panel-specific
hooks exist anywhere in the client.

The page sends API calls from our own code — never OS key events —
so the focus/leak defect class of the old chain stays dead.

Run on the host (the mini) as:

    python -m gamepad_mcp.client.panel --host <tailscale-ip> \
        --port 8791 --log ~/chiaki-spike/panel-movelog.md
"""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .base import RemotePlayClient
from .errors import ClientError
from .log import MoveLogger, describe_input
from .sources import ManualSource, StatePump
from .state import ControllerState, InputLogRecord


class PanelService:
    """Owns the client connection the panel drives."""

    def __init__(
        self,
        client: RemotePlayClient,
        logger: Optional[MoveLogger] = None,
        frame_dir: Optional[str] = None,
        stall_threshold: float = 2.0,
    ):
        self.client = client
        self.logger = logger if logger is not None else MoveLogger()
        self.client.set_log_sink(self.logger)
        self.source = ManualSource()
        self.pump = StatePump(self.source, self.client)
        self._state_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._frame_jpeg: Optional[bytes] = None
        self._frame_ts: float = 0.0
        self._frame_error: str = ""
        self._frame_dir = Path(frame_dir) if frame_dir else Path(".")
        self._frame_stop = threading.Event()
        self._frame_thread: Optional[threading.Thread] = None
        # D1: how old the newest frame may get before the surface
        # must call it stale — disclosed in status, logged once per
        # episode, never presented as live.
        self.stall_threshold = stall_threshold
        self._pump_started_ts: float = 0.0
        self._stall_logged = False

    # -- frames -------------------------------------------------------
    def start_frames(self, interval: float = 0.2) -> None:
        self._frame_stop.clear()
        self._pump_started_ts = time.time()
        self._frame_thread = threading.Thread(
            target=self._frame_loop, args=(interval,), daemon=True
        )
        self._frame_thread.start()

    def stop_frames(self) -> None:
        self._frame_stop.set()

    def _frame_loop(self, interval: float) -> None:
        path = str(self._frame_dir / "panel-frame.png")
        while not self._frame_stop.is_set():
            try:
                result = self.client.grab_frame(path)
                jpeg = self._to_jpeg(path)
                with self._frame_lock:
                    self._frame_jpeg = jpeg
                    self._frame_ts = result.captured_ts
                    self._frame_error = ""
                    self._stall_logged = False
            except Exception as exc:  # frame path reports, never dies
                with self._frame_lock:
                    self._frame_error = f"{type(exc).__name__}: {exc}"
            self._frame_stop.wait(interval)

    @staticmethod
    def _to_jpeg(png_path: str) -> bytes:
        from PIL import Image

        buf = io.BytesIO()
        with Image.open(png_path) as img:
            img.convert("RGB").save(buf, format="JPEG", quality=65)
        return buf.getvalue()

    # -- frame freshness (D1: a frozen frame is never "live") --------
    def _frame_age(self) -> Optional[float]:
        with self._frame_lock:
            ts = self._frame_ts
        if ts:
            return max(0.0, time.time() - ts)
        if self._pump_started_ts:
            return max(0.0, time.time() - self._pump_started_ts)
        return None

    def _surface_stall(self) -> Optional[str]:
        """The stall message if frames are stale, else None.

        Computed on the status/frame read path — HTTP handler
        threads — so the disclosure happens even when the pump
        thread itself is the thing that is wedged (exactly the D1
        failure shape). The first crossing per stall episode is
        written to the MoveLogger as a FAILED record; a fresh frame
        ends the episode (the pump clears the flag on success).
        """
        age = self._frame_age()
        if age is None or age <= self.stall_threshold:
            return None
        message = (
            f"FrameStallError: no new frame for {age:.1f} s "
            f"(stall bound {self.stall_threshold:.1f} s)"
        )
        with self._frame_lock:
            already = self._stall_logged
            self._stall_logged = True
        if not already:
            self.logger.log(
                InputLogRecord.now(
                    input={"tool": "frame_pump"},
                    why="panel frame freshness watchdog",
                    result=f"FAILED: {message}",
                    outcome="failed",
                )
            )
        return message

    def frame(self) -> tuple[Optional[bytes], float, str]:
        with self._frame_lock:
            jpeg, ts, error = self._frame_jpeg, self._frame_ts, self._frame_error
        if not error:
            error = self._surface_stall() or ""
        return jpeg, ts, error

    # -- input ----------------------------------------------------------
    def post_state(self, payload: dict) -> dict:
        state = ControllerState(
            buttons=frozenset(payload.get("buttons", [])),
            axes={k: float(v) for k, v in payload.get("axes", {}).items()},
            triggers={k: float(v) for k, v in payload.get("triggers", {}).items()},
        )
        with self._state_lock:
            self.source.set_state(state)
            try:
                ack = self.pump.step(why="panel")
            except ClientError as exc:
                return {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
        if ack is None:
            return {"ok": True, "unchanged": True}
        return {"ok": True, "latency_ms": round(ack.latency_ms, 2)}

    def release(self) -> dict:
        with self._state_lock:
            try:
                ack = self.pump.release()
            except ClientError as exc:
                return {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
        if ack is None:
            return {"ok": True, "unchanged": True}
        return {"ok": True, "latency_ms": round(ack.latency_ms, 2)}

    # -- status / log ---------------------------------------------------
    def status(self) -> dict:
        profile = None
        negotiated = getattr(self.client, "negotiated_profile", None)
        if callable(negotiated):
            profile = negotiated()
        stall = self._surface_stall()
        age = self._frame_age()
        with self._frame_lock:
            error = self._frame_error
        return {
            "client": self.client.name,
            "console": self.client.console,
            "connected": bool(self.client.connected),
            "profile": profile,
            "frame_ts": self._frame_ts,
            "frame_age_s": (round(age, 2) if age is not None else None),
            "frame_stale": stall is not None,
            "frame_error": error or (stall or ""),
        }

    def log_records(self, limit: int = 30) -> list:
        out = []
        records = self.logger.records[-limit:]
        base = len(self.logger.records) - len(records)
        for i, record in enumerate(records):
            out.append(
                {
                    "n": base + i + 1,
                    "text": MoveLogger.render(record, base + i + 1),
                    "outcome": record.outcome,
                    "latency_ms": (
                        round(record.ack.latency_ms, 2) if record.ack else None
                    ),
                }
            )
        return out


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gamepad-mcp panel — §4.10 walk surface</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #101418; color: #dfe6ee; }
  header { padding: 8px 14px; background: #171d24; display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap; }
  header b { font-size: 15px; }
  #status { font-size: 13px; color: #9fb2c8; }
  #ack { font-size: 13px; font-weight: 600; }
  #ack.err { color: #ff7b72; } #ack.ok { color: #7ee2a0; }
  main { display: flex; gap: 14px; padding: 14px; align-items: flex-start; flex-wrap: wrap; }
  #framePanel { flex: 1 1 560px; }
  #framePanel img { width: 100%; background: #000; border: 1px solid #2b3644; }
  #side { flex: 0 1 430px; display: flex; flex-direction: column; gap: 12px; }
  #pad { background: #171d24; border: 1px solid #2b3644; border-radius: 14px; padding: 12px; user-select: none; touch-action: none; }
  .row { display: flex; justify-content: space-between; align-items: center; margin: 6px 0; }
  .cluster { display: grid; grid-template-columns: repeat(3, 44px); grid-template-rows: repeat(3, 44px); gap: 3px; }
  .btn { background: #232c38; border: 1px solid #3a4759; color: #dfe6ee; border-radius: 10px;
         font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; min-height: 40px; }
  .btn.down { background: #2f6fed; border-color: #2f6fed; color: #fff; }
  .btn.wide { padding: 0 10px; font-size: 12px; }
  .stick { width: 104px; height: 104px; border-radius: 50%; background: #0c0f13; border: 1px solid #3a4759; position: relative; }
  .nub { width: 44px; height: 44px; border-radius: 50%; background: #2a3442; border: 1px solid #46566e;
         position: absolute; left: 30px; top: 30px; pointer-events: none; }
  .mid { display: flex; flex-direction: column; gap: 6px; align-items: center; }
  #log { background: #0c0f13; border: 1px solid #2b3644; border-radius: 10px; padding: 8px 10px;
         font: 11.5px/1.5 ui-monospace, monospace; height: 240px; overflow-y: auto; white-space: pre-wrap; }
  #log .failed { color: #ff7b72; }
</style>
</head>
<body>
<header>
  <b>gamepad-mcp panel</b>
  <span id="status">connecting…</span>
  <span id="ack"></span>
</header>
<main>
  <div id="framePanel"><img id="frame" alt="console frame"></div>
  <div id="side">
    <div id="pad">
      <div class="row">
        <button class="btn wide" data-btn="l2">L2</button>
        <button class="btn wide" data-btn="l1">L1</button>
        <span></span>
        <button class="btn wide" data-btn="r1">R1</button>
        <button class="btn wide" data-btn="r2">R2</button>
      </div>
      <div class="row">
        <div class="cluster">
          <span></span><button class="btn" data-btn="dpad_up">▲</button><span></span>
          <button class="btn" data-btn="dpad_left">◀</button><span></span><button class="btn" data-btn="dpad_right">▶</button>
          <span></span><button class="btn" data-btn="dpad_down">▼</button><span></span>
        </div>
        <div class="mid">
          <div class="row" style="gap:6px">
            <button class="btn wide" data-btn="create">create</button>
            <button class="btn wide" data-btn="ps">PS</button>
            <button class="btn wide" data-btn="options">options</button>
          </div>
          <button class="btn wide" data-btn="touchpad">touchpad</button>
          <button class="btn wide" id="releaseBtn">RELEASE ALL</button>
        </div>
        <div class="cluster">
          <span></span><button class="btn" data-btn="triangle">△</button><span></span>
          <button class="btn" data-btn="square">□</button><span></span><button class="btn" data-btn="circle">○</button>
          <span></span><button class="btn" data-btn="cross">✕</button><span></span>
        </div>
      </div>
      <div class="row">
        <div class="stick" id="stickL"><div class="nub"></div></div>
        <div class="stick" id="stickR"><div class="nub"></div></div>
      </div>
      <div class="row">
        <button class="btn wide" data-btn="l3">L3</button>
        <button class="btn wide" data-btn="r3">R3</button>
      </div>
    </div>
    <div id="log"></div>
  </div>
</main>
<script>
const state = { buttons: [], axes: {left_x:0, left_y:0, right_x:0, right_y:0}, triggers: {l2:0, r2:0} };
const held = new Set();
const ackEl = document.getElementById('ack');

async function send() {
  state.buttons = [...held];
  try {
    const r = await fetch('/api/state', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(state)});
    const j = await r.json();
    if (j.ok && !j.unchanged) { ackEl.textContent = 'ack ' + j.latency_ms.toFixed(1) + ' ms'; ackEl.className = 'ok'; }
    else if (j.ok) { ackEl.textContent = ''; ackEl.className = ''; }
    else { ackEl.textContent = j.error + ': ' + j.detail; ackEl.className = 'err'; }
  } catch (e) { ackEl.textContent = 'panel request failed: ' + e; ackEl.className = 'err'; }
}
function bindButton(el, name) {
  const on = e => { e.preventDefault(); held.add(name); el.classList.add('down');
    if (name === 'l2') state.triggers.l2 = 1; if (name === 'r2') state.triggers.r2 = 1; send(); };
  const off = e => { if (!held.has(name)) return; held.delete(name); el.classList.remove('down');
    if (name === 'l2') state.triggers.l2 = 0; if (name === 'r2') state.triggers.r2 = 0; send(); };
  el.addEventListener('pointerdown', on);
  el.addEventListener('pointerup', off);
  el.addEventListener('pointerleave', off);
  el.addEventListener('pointercancel', off);
}
document.querySelectorAll('[data-btn]').forEach(el => bindButton(el, el.dataset.btn));
document.getElementById('releaseBtn').addEventListener('click', async () => {
  held.clear(); document.querySelectorAll('.btn.down').forEach(b => b.classList.remove('down'));
  state.axes = {left_x:0, left_y:0, right_x:0, right_y:0}; state.triggers = {l2:0, r2:0};
  document.querySelectorAll('.nub').forEach(n => { n.style.left = '30px'; n.style.top = '30px'; });
  await fetch('/api/release', {method:'POST'}); send();
});
function bindStick(el, xKey, yKey) {
  const nub = el.querySelector('.nub'); let active = null;
  function move(e) {
    const r = el.getBoundingClientRect();
    let x = ((e.clientX - r.left) / r.width) * 2 - 1;
    let y = ((e.clientY - r.top) / r.height) * 2 - 1;
    x = Math.max(-1, Math.min(1, x)); y = Math.max(-1, Math.min(1, y));
    state.axes[xKey] = Math.round(x * 100) / 100; state.axes[yKey] = Math.round(y * 100) / 100;
    nub.style.left = (30 + x * 26) + 'px'; nub.style.top = (30 + y * 26) + 'px'; send();
  }
  el.addEventListener('pointerdown', e => { active = e.pointerId; el.setPointerCapture(active); move(e); });
  el.addEventListener('pointermove', e => { if (e.pointerId === active) move(e); });
  const end = e => { if (e.pointerId !== active) return; active = null;
    state.axes[xKey] = 0; state.axes[yKey] = 0; nub.style.left = '30px'; nub.style.top = '30px'; send(); };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
}
bindStick(document.getElementById('stickL'), 'left_x', 'left_y');
bindStick(document.getElementById('stickR'), 'right_x', 'right_y');

const frameImg = document.getElementById('frame');
setInterval(() => { frameImg.src = '/api/frame?t=' + Date.now(); }, 250);
async function refreshStatus() {
  try {
    const j = await (await fetch('/api/status')).json();
    const p = j.profile ? ` · ${j.profile.width}x${j.profile.height}@${j.profile.max_fps} ${j.profile.codec}` : '';
    document.getElementById('status').textContent =
      `${j.client} · ${j.console} · ${j.connected ? 'connected' : 'NOT CONNECTED'}${p}` +
      (j.frame_stale ? ' · FRAME STALE ' + j.frame_age_s + 's' : '') +
      (j.frame_error ? ' · frame: ' + j.frame_error : '');
  } catch (e) { /* next tick */ }
}
async function refreshLog() {
  try {
    const j = await (await fetch('/api/log')).json();
    const log = document.getElementById('log');
    log.innerHTML = j.records.map(r =>
      `<div class="${r.outcome === 'failed' ? 'failed' : ''}">${r.text.replace(/</g, '&lt;')}</div>`).join('');
    log.scrollTop = log.scrollHeight;
  } catch (e) { /* next tick */ }
}
setInterval(refreshStatus, 2000); refreshStatus();
setInterval(refreshLog, 1000); refreshLog();
</script>
</body>
</html>
"""


def make_handler(service: PanelService):
    class PanelHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the host log clean
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/frame"):
                jpeg, _ts, _err = service.frame()
                if jpeg is None:
                    self._send(503, b"no frame yet", "text/plain")
                else:
                    self._send(200, jpeg, "image/jpeg")
            elif self.path.startswith("/api/status"):
                self._json(service.status())
            elif self.path.startswith("/api/log"):
                self._json({"records": service.log_records()})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.path.startswith("/api/state"):
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    self._json({"ok": False, "error": "BadRequest", "detail": str(exc)})
                    return
                self._json(service.post_state(payload))
            elif self.path.startswith("/api/release"):
                self._json(service.release())
            else:
                self._send(404, b"not found", "text/plain")

    return PanelHandler


def serve(
    service: PanelService, host: str, port: int
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server


def install_shutdown_handlers(server: ThreadingHTTPServer) -> None:
    """Make SIGINT and SIGTERM stop the panel gracefully (D2).

    A panel launched in the background (nohup over ssh, as the
    rehearsal runs it) inherits SIGINT set to SIG_IGN — a shell
    launching an async job without job control does that — and
    Python preserves an inherited ignore, so Ctrl-C and kill -INT
    did nothing at all: the M2 rehearsal had to SIGTERM the walk
    surface, skipping the graceful stop and dropping the console
    session abruptly. Installing explicit handlers overrides the
    inherited disposition in every launch mode.

    shutdown() runs on a helper thread: HTTPServer.shutdown() must
    be called from a thread other than serve_forever()'s, and a
    signal lands in the main thread — the serve_forever() thread
    here. Must be called from the main thread (signal rules).
    """
    import signal

    def _handler(signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def serve_until_signal(service: PanelService, server: ThreadingHTTPServer) -> int:
    """serve_forever() plus the graceful stop, however the serve ends:
    stop the frame pump, release all input (dead-man rule),
    disconnect the client, close the server."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_frames()
        try:
            service.release()
        except Exception:
            pass
        service.client.disconnect()
        server.server_close()
    return 0


def main(argv=None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="gamepad-mcp §4.10 panel")
    parser.add_argument("--host", default=os.environ.get("GAMEPAD_PANEL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GAMEPAD_PANEL_PORT", "8791")))
    parser.add_argument("--log", default=os.environ.get("GAMEPAD_PANEL_LOG", ""))
    parser.add_argument("--frame-dir", default=os.environ.get("GAMEPAD_PANEL_FRAMES", ""))
    args = parser.parse_args(argv)

    from .ps import PsClient

    frame_dir = args.frame_dir or str(Path.home() / "chiaki-spike" / "panel-frames")
    Path(frame_dir).mkdir(parents=True, exist_ok=True)
    logger = MoveLogger(path=args.log or None)
    client = PsClient()
    service = PanelService(client, logger=logger, frame_dir=frame_dir)
    client.wake()
    client.connect()
    service.start_frames()
    server = serve(service, args.host, args.port)
    install_shutdown_handlers(server)
    print(f"panel serving on http://{args.host}:{args.port}/ (client {client.name})", flush=True)
    return serve_until_signal(service, server)


if __name__ == "__main__":
    raise SystemExit(main())
