"""macOS virtual keycodes by friendly name (ANSI layout, standard constants).

Only names listed here can be posted by the macOS backend. The null backend
accepts any name, which keeps the rest of the system testable off-Mac.
"""

KEYCODES = {
    # letters
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
    "m": 46,
    # digits (top row)
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28,
    "9": 25, "0": 29,
    # named keys
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51,
    "delete": 51, "esc": 53, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "backslash": 42, "minus": 27, "equals": 24, "comma": 43, "period": 47,
    "slash": 44, "semicolon": 41, "quote": 39, "grave": 50,
    "leftbracket": 33, "rightbracket": 30,
    "shift": 56, "ctrl": 59, "control": 59, "alt": 58, "option": 58,
    "cmd": 55, "command": 55,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def keycode(name: str) -> int:
    try:
        return KEYCODES[name.lower()]
    except KeyError:
        raise ValueError(f"unknown key name: {name!r}") from None
