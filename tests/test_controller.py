import time

from gamepad_mcp.backends.null import NullBackend
from gamepad_mcp.controller import Controller
from gamepad_mcp.keymaps import load_profile


def make_controller(**kw):
    backend = NullBackend()
    return Controller(backend, load_profile("chiaki-ng"), **kw), backend


def kinds(backend):
    return [(kind, detail) for _, kind, detail in backend.events]


def test_press_button_posts_down_then_up():
    controller, backend = make_controller()
    result = controller.press_button("cross", hold_ms=30)
    assert result["key"] == "return"  # Chiaki: Cross = Return (verified map)
    assert ("key_down", "return") in kinds(backend)
    time.sleep(0.15)
    assert ("key_up", "return") in kinds(backend)
    assert controller.held() == []


def test_hold_stick_diagonal_holds_two_keys():
    controller, backend = make_controller()
    result = controller.hold_stick("left", "up-left", hold_ms=30)
    assert sorted(result["keys"]) == ["a", "w"]
    time.sleep(0.15)
    assert controller.held() == []


def test_hold_is_capped():
    controller, _ = make_controller(max_hold_ms=50)
    result = controller.press_key("w", hold_ms=999_999)
    assert result["hold_ms"] == 50
    controller.release_all()


def test_release_all():
    controller, backend = make_controller()
    controller.key_down("w")
    controller.key_down("a")
    out = controller.release_all()
    assert sorted(out["released"]) == ["a", "w"]
    assert controller.held() == []


def test_unknown_button_rejected():
    controller, _ = make_controller()
    try:
        controller.press_button("nope")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
