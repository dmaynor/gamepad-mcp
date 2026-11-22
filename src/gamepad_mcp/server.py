"""gamepad-mcp server: DualSense-shaped MCP tools over an input backend.

Transports:
  stdio (default)            - for agents running on the same machine
  streamable HTTP (--http)   - for remote agents; bind a private address
                               (e.g. a Tailscale IP) and set --token.

Config via flags or environment: GAMEPAD_PROFILE, GAMEPAD_BACKEND,
GAMEPAD_TOKEN, GAMEPAD_MAX_HOLD_MS, GAMEPAD_PUBLIC.

With GAMEPAD_BACKEND=ps the server runs on the native PlayStation
client (PsClient on libchiaki) instead of the keymap stack: input
tools send controller states and return their InputAcks, the
keystroke/mouse tools answer a typed unsupported error, and the
frame/client_status/wake/disconnect tools drive the session. The
session connects lazily on first use — the server starts and serves
while the console refuses sessions, and the refusal surfaces typed
on the first input call. This server and the §4.10 panel are
alternative owners of the console's single session; see
native_tools.py.

Public mode (--public / GAMEPAD_PUBLIC=1) masks IPv4 addresses in the
server's log output — every octet except the last is X'd out, ports are
left readable — so terminal screenshots and streams are safe to share.
It is display-only: the server still binds the real address.
"""

from __future__ import annotations

import argparse
import atexit
import copy
import logging
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .backends import make_backend
from .controller import Controller
from .keymaps import list_profiles, load_profile

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def mask_ips(text: str) -> str:
    """X out the numerals of every IPv4 address in text, except the last
    octet: 100.115.147.21 -> XXX.XXX.XXX.21. A following :port is not part
    of the match and stays readable."""
    def _mask(match: re.Match) -> str:
        octets = match.group(0).split(".")
        return ".".join("X" * len(o) for o in octets[:3]) + "." + octets[3]

    return _IPV4_RE.sub(_mask, text)


class PublicIPMaskFilter(logging.Filter):
    """Logging filter for public mode: masks IPv4 addresses in each
    record before it is formatted.

    The mask is applied to the record's args (and to the message itself
    only when there are no args), NEVER by flattening args into the
    message: uvicorn's AccessFormatter unpacks record.args as a 5-tuple
    (client, method, path, version, status), and flattening args to ()
    crashes it on every access-log line (shipped bug, caught live
    2026-11-21; regression-tested in tests/test_public_mask.py)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: mask_ips(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    mask_ips(a) if isinstance(a, str) else a
                    for a in record.args
                )
        elif isinstance(record.msg, str):
            record.msg = mask_ips(record.msg)
        return True


def public_log_config() -> dict:
    """uvicorn's default log config with PublicIPMaskFilter attached to
    both handlers (startup/error and access logs)."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["public_ip"] = {
        "()": "gamepad_mcp.server.PublicIPMaskFilter"
    }
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("public_ip")
    return config

_backend = None
_controller: Controller | None = None
_native_tools = None


def get_backend():
    """The server's one input backend (GAMEPAD_BACKEND / --backend).

    Built once and shared: a native backend owns a console session,
    and two owners in one process would be two session contenders.
    """
    global _backend
    if _backend is None:
        _backend = make_backend(os.environ.get("GAMEPAD_BACKEND", "auto"))
    return _backend


def get_controller() -> Controller:
    global _controller
    if _controller is None:
        profile = load_profile(os.environ.get("GAMEPAD_PROFILE", "chiaki-ng"))
        max_hold = int(os.environ.get("GAMEPAD_MAX_HOLD_MS", "10000"))
        _controller = Controller(get_backend(), profile, max_hold_ms=max_hold)
        atexit.register(_controller.release_all)
    return _controller


def get_native_tools():
    """The native tool dispatch (native_tools.NativeTools) when the
    active backend is a NativeBackend, else None.

    Under a native backend the keymap Controller is bypassed for
    input tools: tool calls become controller states with InputAcks,
    not keystrokes (PRD §4.4 through MCP — see native_tools.py).
    """
    global _native_tools
    if _native_tools is None:
        from .backends.native import NativeBackend
        from .native_tools import NativeTools

        backend = get_backend()
        if not isinstance(backend, NativeBackend):
            return None
        max_hold = int(os.environ.get("GAMEPAD_MAX_HOLD_MS", "10000"))
        _native_tools = NativeTools(backend, max_hold_ms=max_hold)
        atexit.register(_native_tools.shutdown)
    return _native_tools


def build_server() -> FastMCP:
    mcp = FastMCP("gamepad-mcp")

    @mcp.tool()
    def status() -> dict:
        """Backend, permission state, active keymap profile, held keys.
        On a native backend: the client session status instead."""
        native = get_native_tools()
        if native is not None:
            return native.status()
        return get_controller().status()

    @mcp.tool()
    def press(button: str, hold_ms: int = 120) -> dict:
        """Press a DualSense button: cross, circle, square, triangle,
        dpad_up/down/left/right, l1, r1, l2, r2, l3, r3, options, share,
        touchpad, ps. hold_ms is how long the button stays down.
        On a native backend the result carries the send's InputAck."""
        native = get_native_tools()
        if native is not None:
            return native.press(button, hold_ms)
        return get_controller().press_button(button, hold_ms)

    @mcp.tool()
    def hold_stick(stick: str, direction: str, hold_ms: int = 1000) -> dict:
        """Hold an analog stick direction. stick: 'left' or 'right'.
        direction: up, down, left, right, or a diagonal like 'up-left'.
        This is how a character walks/runs and how the camera turns.
        On a native backend the result carries the send's InputAck."""
        native = get_native_tools()
        if native is not None:
            return native.hold_stick(stick, direction, hold_ms)
        return get_controller().hold_stick(stick, direction, hold_ms)

    @mcp.tool()
    def key_tap(key: str, hold_ms: int = 120) -> dict:
        """Tap a raw keyboard key by name (e.g. 'return', 'w', 'esc').
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("key_tap")
        return get_controller().press_key(key, hold_ms)

    @mcp.tool()
    def key_down(key: str) -> dict:
        """Hold a raw key down until key_up or the hard cap releases it.
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("key_down")
        get_controller().key_down(key)
        return {"key": key, "down": True}

    @mcp.tool()
    def key_up(key: str) -> dict:
        """Release a raw key held by key_down.
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("key_up")
        get_controller().key_up(key)
        return {"key": key, "down": False}

    @mcp.tool()
    def combo(keys: list, hold_ms: int = 120) -> dict:
        """Press several raw keys together (e.g. ['cmd', 'tab']).
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("combo")
        return get_controller().combo(keys, hold_ms)

    @mcp.tool()
    def release_all() -> dict:
        """Panic button: release every held key immediately.
        On a native backend: one neutral controller state, acked."""
        native = get_native_tools()
        if native is not None:
            return native.release_all()
        return get_controller().release_all()

    @mcp.tool()
    def mouse_move(x: int, y: int) -> dict:
        """Move the mouse pointer to absolute screen coordinates.
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("mouse_move")
        return get_controller().mouse_move(x, y)

    @mcp.tool()
    def mouse_click(button: str = "left", hold_ms: int = 80) -> dict:
        """Click the mouse ('left' or 'right').
        No native meaning on a console backend: typed unsupported."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("mouse_click")
        return get_controller().mouse_click(button, hold_ms)

    @mcp.tool()
    def profiles() -> dict:
        """List available keymap profiles.
        Keymap machinery: typed unsupported on a native backend."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("profiles")
        return {"profiles": list_profiles()}

    @mcp.tool()
    def set_profile(name: str) -> dict:
        """Switch the active keymap profile.
        Keymap machinery: typed unsupported on a native backend."""
        native = get_native_tools()
        if native is not None:
            return native.unsupported("set_profile")
        controller = get_controller()
        controller.profile = load_profile(name)
        return {"profile": controller.profile.name}

    @mcp.tool()
    def frame() -> dict:
        """Grab the console's current frame (native backends only).
        The frame is saved to a file; the result carries its path,
        sha256, capture timestamp and dimensions."""
        native = get_native_tools()
        if native is not None:
            return native.frame()
        from .native_tools import unsupported_result

        return unsupported_result("frame", get_backend().name)

    @mcp.tool()
    def client_status() -> dict:
        """Native client session status (native backends only):
        connected?, negotiated video profile, frame age/stale,
        last typed error, and the session-ownership note."""
        native = get_native_tools()
        if native is not None:
            return native.client_status()
        from .native_tools import unsupported_result

        return unsupported_result("client_status", get_backend().name)

    @mcp.tool()
    def wake() -> dict:
        """Wake the console from rest mode and connect (native
        backends only)."""
        native = get_native_tools()
        if native is not None:
            return native.wake()
        from .native_tools import unsupported_result

        return unsupported_result("wake", get_backend().name)

    @mcp.tool()
    def disconnect() -> dict:
        """End the console session cleanly, releasing all input
        first (native backends only)."""
        native = get_native_tools()
        if native is not None:
            return native.disconnect()
        from .native_tools import unsupported_result

        return unsupported_result("disconnect", get_backend().name)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="gamepad-mcp")
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("GAMEPAD_TOKEN"),
                        help="require 'Authorization: Bearer <token>' (HTTP)")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--backend", default=None,
                        help="'auto', 'macos', 'null' (dry-run), 'stub' "
                             "(native over the stub client), or 'ps' "
                             "(native PlayStation client; connects "
                             "lazily on first input)")
    parser.add_argument("--public", action="store_true",
                        default=os.environ.get("GAMEPAD_PUBLIC") == "1",
                        help="mask IPv4 addresses in log output (all octets "
                             "but the last become X's; ports stay readable) "
                             "so screenshots are safe to share; display-only, "
                             "the bind address is unchanged")
    args = parser.parse_args()

    if args.profile:
        os.environ["GAMEPAD_PROFILE"] = args.profile
    if args.backend:
        os.environ["GAMEPAD_BACKEND"] = args.backend

    server = build_server()
    if not args.http:
        server.run(transport="stdio")
        return

    server.settings.host = args.host
    server.settings.port = args.port
    # FastMCP's default DNS-rebinding protection builds an allowlist of
    # localhost Host headers only; bound to any other address (e.g. a
    # Tailscale IP) it rejects EVERY request with 421 "Invalid Host
    # header" (verified live 2026-11-21). Access control here is the
    # bearer token plus the private-network bind, so the browser
    # threat model the check defends against does not apply.
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    app = server.streamable_http_app()
    if args.token:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("authorization") != f"Bearer {args.token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)
    import uvicorn

    if args.public:
        uvicorn.run(app, host=args.host, port=args.port,
                    log_config=public_log_config())
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
