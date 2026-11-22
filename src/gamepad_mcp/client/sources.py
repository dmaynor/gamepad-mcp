"""Input sources: interchangeable front-ends that produce state.

PRD §4.10/§4.11: the diagnostic panel and a real physical gamepad
are the same thing to the client — an InputSource whose read_state()
returns the current ControllerState. Neither produces keystrokes;
both feed the StatePump, which hands changed states to the client's
send_state and so gets the same ack, latency, and logging whichever
front-end a human (or the loop) is driving.

Sources in M1: ManualSource (the panel's model — presses set state).
M2 adds PadSource: a physical gamepad (Bluetooth/USB) read over HID,
implementing this same ABC — nothing above it changes.
"""

from __future__ import annotations

from typing import Optional

from .base import RemotePlayClient
from .errors import PadUnavailableError
from .state import ControllerState, InputAck


class InputSource:
    """A producer of controller state."""

    name = "base"

    def read_state(self) -> ControllerState:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ManualSource(InputSource):
    """State set explicitly by the caller — the §4.10 panel's model.

    A panel press is set_button(name, True); a release is
    set_button(name, False); sticks/triggers set their values. The
    pump turns the resulting state changes into send_state calls.
    """

    name = "manual"

    def __init__(self):
        self._state = ControllerState.neutral()

    def set_button(self, button: str, down: bool) -> None:
        if down:
            self._state = self._state.with_buttons(add={button})
        else:
            self._state = self._state.with_buttons(remove={button})

    def set_axis(self, axis: str, value: float) -> None:
        axes = dict(self._state.axes)
        axes[axis] = value
        self._state = ControllerState(
            buttons=self._state.buttons, axes=axes, triggers=dict(self._state.triggers)
        )

    def set_trigger(self, trigger: str, value: float) -> None:
        triggers = dict(self._state.triggers)
        triggers[trigger] = value
        self._state = ControllerState(
            buttons=self._state.buttons, axes=dict(self._state.axes), triggers=triggers
        )

    def set_state(self, state: ControllerState) -> None:
        self._state = state

    def read_state(self) -> ControllerState:
        return self._state


class StatePump:
    """Moves state changes from a source to a client.

    step() reads the source; if the state changed since the last
    send, it hands the new state to client.send_state (which acks,
    timestamps, and logs it — PRD §4.4/§4.5/§4.9) and returns the ack.
    Unchanged state sends nothing and returns None. A typed error
    from send_state propagates — the pump never swallows it.
    """

    def __init__(self, source: InputSource, client: RemotePlayClient):
        self.source = source
        self.client = client
        self._last_sent: Optional[ControllerState] = None

    def step(self, why: str = "") -> Optional[InputAck]:
        state = self.source.read_state()
        if state == self._last_sent:
            return None
        ack = self.client.send_state(state, why=why or f"source:{self.source.name}")
        self._last_sent = state
        return ack

    def release(self) -> Optional[InputAck]:
        """Dead-man: force the neutral state through, whatever the source says."""
        if self._last_sent is not None and self._last_sent.is_neutral():
            return None
        ack = self.client.send_state(
            ControllerState.neutral(), why=f"release:{self.source.name}"
        )
        self._last_sent = ControllerState.neutral()
        return ack


# ---------------------------------------------------------------------
# Physical pad source (PRD §4.11)
#
# DualSense and DualShock 4 share the input-report layout for every
# field this layer reads (USB report 0x01; the Bluetooth DualSense
# report 0x31 inserts one sequence byte, shifting the payload by 1):
#
#   [1..4]  stick bytes LX, LY, RX, RY (0..255, 128 centre)
#   [5]     low nibble: dpad hat; bit4 square, bit5 cross,
#           bit6 circle, bit7 triangle
#   [6]     bit0 L1, bit1 R1, bit2 L2, bit3 R2, bit4 create/share,
#           bit5 options, bit6 L3, bit7 R3
#   [7]     bit0 PS, bit1 touchpad click
#   [8, 9]  L2 / R2 analog (0..255)
#
# HID stick convention matches libchiaki's: up is the small value, so
# (v - 128) / 128 gives -1.0 fully up — the same axis convention
# PsClient.apply_state consumes. A small deadzone keeps a resting
# pad from drifting the character.
# ---------------------------------------------------------------------

_DEADZONE = 0.08

_HAT_DPAD = {
    0: ("dpad_up",),
    1: ("dpad_up", "dpad_right"),
    2: ("dpad_right",),
    3: ("dpad_down", "dpad_right"),
    4: ("dpad_down",),
    5: ("dpad_down", "dpad_left"),
    6: ("dpad_left",),
    7: ("dpad_up", "dpad_left"),
}

# (vendor_id, product_id) -> kind
SUPPORTED_PADS = {
    (0x054C, 0x0CE6): "dualsense",
    (0x054C, 0x09CC): "dualshock4",
    (0x054C, 0x05C4): "dualshock4",
}


def _axis(byte: int) -> float:
    value = (byte - 128) / 128.0
    value = max(-1.0, min(1.0, value))
    return 0.0 if abs(value) < _DEADZONE else value


def parse_pad_report(data: bytes, offset: int = 0) -> ControllerState:
    """Parse one DualSense/DS4 input report into a ControllerState.

    ``offset`` skips a transport header (Bluetooth DualSense reports
    carry the USB-layout payload at offset 2). Pure function — the
    unit tests feed it synthetic reports.
    """
    if len(data) < offset + 10:
        raise PadUnavailableError(
            f"short pad report: {len(data)} bytes (offset {offset})"
        )
    r = data[offset:]
    buttons = set(_HAT_DPAD.get(r[5] & 0x0F, ()))
    if r[5] & 0x10:
        buttons.add("square")
    if r[5] & 0x20:
        buttons.add("cross")
    if r[5] & 0x40:
        buttons.add("circle")
    if r[5] & 0x80:
        buttons.add("triangle")
    for bit, name in (
        (0x01, "l1"), (0x02, "r1"), (0x04, "l2"), (0x08, "r2"),
        (0x10, "create"), (0x20, "options"), (0x40, "l3"), (0x80, "r3"),
    ):
        if r[6] & bit:
            buttons.add(name)
    if r[7] & 0x01:
        buttons.add("ps")
    if r[7] & 0x02:
        buttons.add("touchpad")
    return ControllerState(
        buttons=frozenset(buttons),
        axes={
            "left_x": _axis(r[1]),
            "left_y": _axis(r[2]),
            "right_x": _axis(r[3]),
            "right_y": _axis(r[4]),
        },
        triggers={"l2": r[8] / 255.0, "r2": r[9] / 255.0},
    )


class HidPadReader:
    """Reads a supported pad over hidapi (hidapi_py).

    The reader is deliberately thin and injectable: tests substitute
    a fake with the same probe/read shape. On a host with no pad,
    probe() is False and read() raises PadUnavailableError — typed,
    never a fabricated neutral state.
    """

    def __init__(self, hid_module=None):
        self._hid = hid_module  # None -> import hidapi_py lazily
        self._device = None
        self.kind: Optional[str] = None

    def _module(self):
        if self._hid is None:
            try:
                import hidapi_py

                self._hid = hidapi_py
            except ImportError as exc:
                raise PadUnavailableError("hidapi_py is not installed") from exc
        return self._hid

    def _find(self):
        hid = self._module()
        for (vid, pid), kind in SUPPORTED_PADS.items():
            try:
                infos = hid.get_all_device_infos(vid, pid)
            except Exception:
                infos = []
            if infos:
                return infos[0], kind
        return None, None

    def probe(self) -> bool:
        info, _kind = self._find()
        return info is not None

    def _open(self):
        if self._device is not None:
            return self._device
        hid = self._module()
        info, kind = self._find()
        if info is None:
            raise PadUnavailableError(
                "no supported gamepad found (DualSense/DS4 over USB or Bluetooth)"
            )
        try:
            device = hid.HidDevice(info)
        except Exception:
            device = hid.HidDevice(info.vendor_id, info.product_id)
        device.open()
        device.set_nonblocking(True)
        self._device = device
        self.kind = kind
        return device

    def read(self) -> ControllerState:
        device = self._open()
        data = device.read(64)
        if not data:
            # Nonblocking read with nothing queued: the device exists
            # and is open, so "no report yet" reads as neutral.
            return ControllerState.neutral()
        data = bytes(data)
        # Report 0x01 (USB, both pads) is parsed at offset 0. The
        # Bluetooth DualSense report 0x31 inserts one sequence byte
        # after the report id, shifting the payload by 1 (the parser
        # never reads byte 0, which is the id slot in both layouts).
        offset = 1 if data[0] == 0x31 else 0
        return parse_pad_report(data, offset=offset)

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            finally:
                self._device = None


class PadSource(InputSource):
    """A physical gamepad as an InputSource (PRD §4.11).

    Interchangeable with ManualSource by construction: read_state()
    returns the pad's current ControllerState, and the StatePump
    above it produces the same send_state/ack/log flow as the panel.
    """

    name = "pad"

    def __init__(self, reader: Optional[HidPadReader] = None):
        self._reader = reader if reader is not None else HidPadReader()
        self._last: Optional[ControllerState] = None

    def probe(self) -> bool:
        return self._reader.probe()

    def read_state(self) -> ControllerState:
        state = self._reader.read()
        self._last = state
        return state

    def close(self) -> None:
        self._reader.close()
