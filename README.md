# gamepad-mcp

A scriptable gamepad for AI agents: an [MCP](https://modelcontextprotocol.io)
server that plays games by posting **real HID keyboard and mouse events** on
the host machine.

## Why this exists

Driving a game from an agent usually dies at the same wall: synthetic input
APIs that go through accessibility layers need the target window to expose a
keyboard recipient, and game/streaming windows — Chiaki's stream view is the
one that taught us this — often don't. A physical keyboard works because its
events enter at the HID layer.

gamepad-mcp posts events at that layer (macOS: `CGEventPost` at the HID
event tap). To any window, its input is indistinguishable from a person
typing. On top of that transport it exposes a **DualSense-shaped tool
surface** — `press("cross")`, `hold_stick("left", "up", 2000)` — translated
through a keymap profile for whatever client is actually receiving the keys
(Chiaki-ng ships built in; any keyboard-driven game works with a profile you
write in five minutes).

## Install

```sh
./install.sh          # venv at ~/.local/share/gamepad-mcp, link in ~/.local/bin
```

On macOS, one permission is required — the server posts HID events, which
macOS gates behind Accessibility trust:

1. Start the server once (below).
2. System Settings → Privacy & Security → Accessibility → enable the
   terminal/python entry running gamepad-mcp. (On macOS 27 this list lives
   under Privacy & Security → **Device Control and Data Access** instead.)
   The grant only latches after the server process restarts.
3. Call the `status` tool: `"trusted": true` means you're ready.

## Run

```sh
# stdio — for an agent on the same machine
gamepad-mcp

# streamable HTTP — for a remote agent; bind a private address (e.g. your
# Tailscale IP), never a public interface, and always set a token
gamepad-mcp --http --host 100.x.y.z --port 8765 --token <secret>

# dry-run anywhere (records events instead of posting them)
gamepad-mcp --backend null
```

An optional launchd agent lives in `launchd/com.gamepadmcp.server.plist`.

MCP client config (stdio):

```json
{"mcpServers": {"gamepad": {"command": "gamepad-mcp",
  "env": {"GAMEPAD_PROFILE": "chiaki-ng"}}}}
```

## Tools

| Tool | What it does |
|---|---|
| `status` | Backend, Accessibility trust, active profile, currently held keys |
| `press(button, hold_ms=120)` | DualSense buttons: `cross circle square triangle dpad_* l1 r1 l2 r2 l3 r3 options share touchpad ps` |
| `hold_stick(stick, direction, hold_ms=1000)` | `left`/`right` stick; directions incl. diagonals (`up-left`). Walking, running, camera |
| `key_tap(key, hold_ms)` / `key_down(key)` / `key_up(key)` | Raw keys by name (`return`, `w`, `esc`, …) |
| `combo(keys, hold_ms)` | Chords, e.g. `["cmd", "tab"]` |
| `release_all` | Panic button: every held key up, now |
| `mouse_move(x, y)` / `mouse_click(button, hold_ms)` | Absolute pointer control |
| `profiles` / `set_profile(name)` | Keymap management |

## Safety model

- Every key-down is paired with a scheduled key-up. Holds default to a
  **10 s cap** (`GAMEPAD_MAX_HOLD_MS`) with a **30 s hard cap** no caller can
  exceed — a crashed client can't leave a character running into a wall.
- `release_all` also runs automatically at server shutdown.
- The HTTP transport refuses to be useful without a token; bind private
  addresses only. This server *is* remote control of a keyboard — treat the
  token like a password.

## Keymap profiles

Profiles are JSON: DualSense names → key names. See
[`keymaps/chiaki-ng.json`](keymaps/chiaki-ng.json) — its button map was read
live from Chiaki's Settings → Keys against a PS5 and verified in game.
The stick entries assume WASD (left) / IJKL (right), but Chiaki ships its
stick bindings **unbound**: bind Left Stick to W/A/S/D and Right Stick to
I/J/K/L in Chiaki's Settings → Keys first, or `hold_stick` will do nothing.
Drop your own profiles in `~/.config/gamepad-mcp/keymaps/`.

## Status

v0.1.2. The controller, keymaps, and MCP protocol are covered by tests that
run anywhere (`pip install -e ".[test]" && pytest`). The macOS event backend
is exercised on a real Mac; off-Mac it stays behind the backend interface,
with the null backend standing in.

## License

Apache-2.0. See [LICENSE](LICENSE).
