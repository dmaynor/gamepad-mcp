"""Keymap profiles: DualSense-shaped controls -> keyboard key names.

A profile is a JSON object:
{
  "name": "chiaki-ng",
  "buttons": {"cross": "return", ...},
  "left_stick":  {"up": "w", "down": "s", "left": "a", "right": "d"},
  "right_stick": {"up": "i", "down": "k", "left": "j", "right": "l"}
}

Profiles ship in the package's keymaps/ directory and can be overridden or
extended by dropping a JSON file in ~/.config/gamepad-mcp/keymaps/.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BUTTONS = [
    "cross", "circle", "square", "triangle",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "l1", "r1", "l2", "r2", "l3", "r3",
    "options", "share", "touchpad", "ps",
]
DIRECTIONS = ["up", "down", "left", "right"]


@dataclass
class Profile:
    name: str
    buttons: dict = field(default_factory=dict)
    left_stick: dict = field(default_factory=dict)
    right_stick: dict = field(default_factory=dict)

    def button_key(self, button: str) -> str:
        try:
            return self.buttons[button]
        except KeyError:
            raise ValueError(
                f"profile {self.name!r} has no mapping for button {button!r}"
            ) from None

    def stick_keys(self, stick: str, direction: str) -> list:
        """Key names for a stick direction; diagonals return two keys."""
        table = self.left_stick if stick == "left" else self.right_stick
        parts = direction.split("-")
        keys = []
        for part in parts:
            if part not in DIRECTIONS:
                raise ValueError(f"bad direction component: {part!r}")
            if part not in table:
                raise ValueError(
                    f"profile {self.name!r} has no {stick}-stick mapping for {part!r}"
                )
            keys.append(table[part])
        return keys


def _search_dirs() -> list:
    dirs = []
    user_dir = os.environ.get("GAMEPAD_KEYMAPS")
    if user_dir:
        dirs.append(Path(user_dir))
    dirs.append(Path.home() / ".config" / "gamepad-mcp" / "keymaps")
    # keymaps/ at the repo root, two levels up from this file in a source tree
    dirs.append(Path(__file__).resolve().parents[2] / "keymaps")
    return dirs


def load_profile(name: str) -> Profile:
    for directory in _search_dirs():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            data = json.loads(candidate.read_text())
            return Profile(
                name=data.get("name", name),
                buttons=data.get("buttons", {}),
                left_stick=data.get("left_stick", {}),
                right_stick=data.get("right_stick", {}),
            )
    raise ValueError(f"keymap profile not found: {name!r}")


def list_profiles() -> list:
    names = set()
    for directory in _search_dirs():
        if directory.is_dir():
            for path in directory.glob("*.json"):
                names.add(path.stem)
    return sorted(names)
