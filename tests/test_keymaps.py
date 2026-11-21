from gamepad_mcp.keycodes import KEYCODES
from gamepad_mcp.keymaps import BUTTONS, DIRECTIONS, list_profiles, load_profile


def test_chiaki_profile_covers_every_button():
    profile = load_profile("chiaki-ng")
    for button in BUTTONS:
        key = profile.button_key(button)
        assert key in KEYCODES, f"{button} -> {key} has no macOS keycode"


def test_chiaki_profile_sticks_resolve():
    profile = load_profile("chiaki-ng")
    for stick in ("left", "right"):
        for direction in DIRECTIONS:
            keys = profile.stick_keys(stick, direction)
            assert keys and all(k in KEYCODES for k in keys)


def test_chiaki_verified_button_facts():
    """The map read live from Chiaki Settings -> Keys, 2026-11-21."""
    profile = load_profile("chiaki-ng")
    assert profile.buttons["cross"] == "return"
    assert profile.buttons["circle"] == "backspace"
    assert profile.buttons["triangle"] == "c"
    assert profile.buttons["square"] == "backslash"
    assert profile.buttons["ps"] == "esc"
    assert profile.buttons["options"] == "o"


def test_profiles_listed():
    assert "chiaki-ng" in list_profiles()
