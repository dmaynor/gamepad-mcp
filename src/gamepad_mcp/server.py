"""gamepad-mcp server: DualSense-shaped MCP tools over an input backend.

Transports:
  stdio (default)            - for agents running on the same machine
  streamable HTTP (--http)   - for remote agents; bind a private address
                               (e.g. a Tailscale IP) and set --token.

Config via flags or environment: GAMEPAD_PROFILE, GAMEPAD_BACKEND,
GAMEPAD_TOKEN, GAMEPAD_MAX_HOLD_MS, GAMEPAD_PUBLIC.

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
    """Logging filter for public mode: rewrites each record's message
    through mask_ips before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        masked = mask_ips(record.getMessage())
        if masked != record.getMessage():
            record.msg = masked
            record.args = ()
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

_controller: Controller | None = None


def get_controller() -> Controller:
    global _controller
    if _controller is None:
        profile = load_profile(os.environ.get("GAMEPAD_PROFILE", "chiaki-ng"))
        backend = make_backend(os.environ.get("GAMEPAD_BACKEND", "auto"))
        max_hold = int(os.environ.get("GAMEPAD_MAX_HOLD_MS", "10000"))
        _controller = Controller(backend, profile, max_hold_ms=max_hold)
        atexit.register(_controller.release_all)
    return _controller


def build_server() -> FastMCP:
    mcp = FastMCP("gamepad-mcp")

    @mcp.tool()
    def status() -> dict:
        """Backend, permission state, active keymap profile, held keys."""
        return get_controller().status()

    @mcp.tool()
    def press(button: str, hold_ms: int = 120) -> dict:
        """Press a DualSense button: cross, circle, square, triangle,
        dpad_up/down/left/right, l1, r1, l2, r2, l3, r3, options, share,
        touchpad, ps. hold_ms is how long the button stays down."""
        return get_controller().press_button(button, hold_ms)

    @mcp.tool()
    def hold_stick(stick: str, direction: str, hold_ms: int = 1000) -> dict:
        """Hold an analog stick direction. stick: 'left' or 'right'.
        direction: up, down, left, right, or a diagonal like 'up-left'.
        This is how a character walks/runs and how the camera turns."""
        return get_controller().hold_stick(stick, direction, hold_ms)

    @mcp.tool()
    def key_tap(key: str, hold_ms: int = 120) -> dict:
        """Tap a raw keyboard key by name (e.g. 'return', 'w', 'esc')."""
        return get_controller().press_key(key, hold_ms)

    @mcp.tool()
    def key_down(key: str) -> dict:
        """Hold a raw key down until key_up or the hard cap releases it."""
        get_controller().key_down(key)
        return {"key": key, "down": True}

    @mcp.tool()
    def key_up(key: str) -> dict:
        """Release a raw key held by key_down."""
        get_controller().key_up(key)
        return {"key": key, "down": False}

    @mcp.tool()
    def combo(keys: list, hold_ms: int = 120) -> dict:
        """Press several raw keys together (e.g. ['cmd', 'tab'])."""
        return get_controller().combo(keys, hold_ms)

    @mcp.tool()
    def release_all() -> dict:
        """Panic button: release every held key immediately."""
        return get_controller().release_all()

    @mcp.tool()
    def mouse_move(x: int, y: int) -> dict:
        """Move the mouse pointer to absolute screen coordinates."""
        return get_controller().mouse_move(x, y)

    @mcp.tool()
    def mouse_click(button: str = "left", hold_ms: int = 80) -> dict:
        """Click the mouse ('left' or 'right')."""
        return get_controller().mouse_click(button, hold_ms)

    @mcp.tool()
    def profiles() -> dict:
        """List available keymap profiles."""
        return {"profiles": list_profiles()}

    @mcp.tool()
    def set_profile(name: str) -> dict:
        """Switch the active keymap profile."""
        controller = get_controller()
        controller.profile = load_profile(name)
        return {"profile": controller.profile.name}

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
                        help="'auto', 'macos', or 'null' (dry-run)")
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
