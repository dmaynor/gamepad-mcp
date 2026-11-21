#!/bin/sh
# gamepad-mcp installer (macOS or Linux).
#
# Creates an isolated venv at ~/.local/share/gamepad-mcp/venv, installs the
# package from this directory into it, and links `gamepad-mcp` into
# ~/.local/bin. Nothing is installed system-wide.
set -eu

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${GAMEPAD_PREFIX:-$HOME/.local/share/gamepad-mcp}"
BIN_DIR="${GAMEPAD_BIN_DIR:-$HOME/.local/bin}"

command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "python >= 3.10 required" >&2; exit 1; }

mkdir -p "$PREFIX" "$BIN_DIR"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  python3 -m venv "$PREFIX/venv"
fi

EXTRA=""
if [ "$(uname -s)" = "Darwin" ]; then
  EXTRA="[macos]"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC_DIR$EXTRA"

# Keymap profiles live where the installed server looks for them.
mkdir -p "$HOME/.config/gamepad-mcp/keymaps"
cp "$SRC_DIR"/keymaps/*.json "$HOME/.config/gamepad-mcp/keymaps/" 2>/dev/null || true

ln -sf "$PREFIX/venv/bin/gamepad-mcp" "$BIN_DIR/gamepad-mcp"

echo "installed: $BIN_DIR/gamepad-mcp"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: add $BIN_DIR to your PATH" ;;
esac

if [ "$(uname -s)" = "Darwin" ]; then
  cat <<'EOF'

macOS permission (one time, required): the server posts real HID events,
which macOS gates behind Accessibility trust.
  1. Run the server once (step below) so the python process exists.
  2. System Settings -> Privacy & Security -> Accessibility -> enable the
     terminal/python entry that is running gamepad-mcp.
  3. Call the `status` tool: "trusted": true means you are ready.

Run it:
  stdio (local agent):   gamepad-mcp
  HTTP (remote agent):   gamepad-mcp --http --host <tailscale-ip> --port 8765 --token <secret>
EOF
fi
echo "done."
