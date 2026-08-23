# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Cross platform gamepad polling for the XInput Reader add-on.

On Windows the bundled XInput_Python wheel is used, on Linux the evdev
character devices in /dev/input are read directly so no extra dependency
is needed. Both backends hand back the exact same dictionary of values,
using XInput's normalization and dead zones, so the rest of the add-on
does not need to know which platform it is running on.
"""

import math
import os
import sys
import time


# The custom properties written to the reader empty, in creation order.
INPUT_NAMES = (
    "A",
    "B",
    "X",
    "Y",
    "DPadUp",
    "DPadDown",
    "DPadLeft",
    "DPadRight",
    "Start",
    "Back",
    "LeftThumb",
    "LeftThumbX",
    "LeftThumbY",
    "RightThumb",
    "RightThumbX",
    "RightThumbY",
    "LeftShoulder",
    "RightShoulder",
    "LeftTrigger",
    "RightTrigger",
)

BUTTON_NAMES = tuple(
    name for name in INPUT_NAMES
    if name not in {"LeftThumbX", "LeftThumbY", "RightThumbX", "RightThumbY",
                    "LeftTrigger", "RightTrigger"}
)

# Dead zones as defined by XInput, applied on both platforms so that a
# controller behaves the same way regardless of the operating system.
LEFT_THUMB_DEADZONE = 7849
RIGHT_THUMB_DEADZONE = 8689
TRIGGER_THRESHOLD = 30

THUMB_RANGE = 32767.0
TRIGGER_RANGE = 255.0


class GamepadError(Exception):
    """Raised when no gamepad backend is usable on this system."""


#------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------NORMALIZATION-------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


def _normalize_thumb(x, y, deadzone):
    """Radial dead zone, matching XInput.get_thumb_values()."""
    magnitude = math.sqrt(x * x + y * y)

    if magnitude == 0:  # centered stick, there is no direction
        return 0.0, 0.0

    norm_x = x / magnitude
    norm_y = y / magnitude

    if magnitude <= deadzone:
        return 0.0, 0.0

    magnitude = min(THUMB_RANGE, magnitude) - deadzone
    norm_magnitude = magnitude / (THUMB_RANGE - deadzone)

    return norm_x * norm_magnitude, norm_y * norm_magnitude


def _normalize_trigger(value):
    """Trigger threshold, matching XInput.get_trigger_values()."""
    if value > TRIGGER_THRESHOLD:
        return (value - TRIGGER_THRESHOLD) / (TRIGGER_RANGE - TRIGGER_THRESHOLD)
    return 0.0


def _make_values(buttons, thumbs, triggers):
    """Build the property dictionary from raw, XInput scaled, values.

    buttons  -- dict of booleans keyed by the names in BUTTON_NAMES
    thumbs   -- (left_x, left_y, right_x, right_y), -32768 .. 32767
    triggers -- (left, right), 0 .. 255
    """
    left_x, left_y = _normalize_thumb(thumbs[0], thumbs[1], LEFT_THUMB_DEADZONE)
    right_x, right_y = _normalize_thumb(thumbs[2], thumbs[3], RIGHT_THUMB_DEADZONE)

    values = {name: bool(buttons.get(name, False)) for name in BUTTON_NAMES}
    values["LeftThumbX"] = left_x
    values["LeftThumbY"] = left_y
    values["RightThumbX"] = right_x
    values["RightThumbY"] = right_y
    values["LeftTrigger"] = _normalize_trigger(triggers[0])
    values["RightTrigger"] = _normalize_trigger(triggers[1])

    # Keep the documented property order rather than the insertion order.
    return {name: values[name] for name in INPUT_NAMES}


#------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------WINDOWS-------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


class XInputBackend:
    """Polls a controller through the XInput API (Windows only)."""

    platform_name = "XInput"

    def __init__(self, index=0):
        try:
            import XInput
        except Exception as error:  # pragma: no cover - depends on platform
            raise GamepadError(
                "The XInput library could not be loaded: {}".format(error))

        self._xinput = XInput
        self.index = index

    def poll(self):
        """Return the current values, or None if no controller is connected."""
        try:
            state = self._xinput.get_state(self.index)
        except self._xinput.XInputNotConnectedError:
            return None

        pad = state.Gamepad
        pressed = pad.wButtons

        buttons = {
            "A": pressed & 0x1000,
            "B": pressed & 0x2000,
            "X": pressed & 0x4000,
            "Y": pressed & 0x8000,
            "DPadUp": pressed & 0x0001,
            "DPadDown": pressed & 0x0002,
            "DPadLeft": pressed & 0x0004,
            "DPadRight": pressed & 0x0008,
            "Start": pressed & 0x0010,
            "Back": pressed & 0x0020,
            "LeftThumb": pressed & 0x0040,
            "RightThumb": pressed & 0x0080,
            "LeftShoulder": pressed & 0x0100,
            "RightShoulder": pressed & 0x0200,
        }

        return _make_values(
            buttons,
            (pad.sThumbLX, pad.sThumbLY, pad.sThumbRX, pad.sThumbRY),
            (pad.bLeftTrigger, pad.bRightTrigger),
        )

    def describe(self):
        connected = self._xinput.get_connected()
        if not connected[self.index]:
            return "Controller {} (not connected)".format(self.index + 1)
        return "Controller {}".format(self.index + 1)

    def close(self):
        pass

    def list_devices(self):
        return ["Controller {}".format(slot + 1)
                for slot, connected in enumerate(self._xinput.get_connected())
                if connected]


#------------------------------------------------------------------------------------------------------------------------------#
#-----------------------------------------------------------LINUX--------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#

# Subset of linux/input-event-codes.h needed to talk to an evdev device.
_EV_KEY = 0x01
_EV_ABS = 0x03

_ABS_X = 0x00
_ABS_Y = 0x01
_ABS_Z = 0x02
_ABS_RX = 0x03
_ABS_RY = 0x04
_ABS_RZ = 0x05
_ABS_HAT0X = 0x10
_ABS_HAT0Y = 0x11
_ABS_MAX = 0x3F

_KEY_MAX = 0x2FF

_BTN_JOYSTICK = 0x120       # first of the legacy joystick buttons
_BTN_JOYSTICK_LAST = 0x12F  # last of the legacy joystick buttons
_BTN_GAMEPAD_LAST = 0x13F   # last of the gamepad buttons
_BTN_DPAD_UP = 0x220
_BTN_DPAD_RIGHT = 0x223

# Buttons as reported by the kernel gamepad drivers (xpad, hid-sony, ...).
# The A/B/X/Y codes follow the xpad convention, which is what the Xbox style
# controllers this add-on targets use. Face buttons are the one place where
# Linux drivers disagree with each other, so swap these two lines if your
# controller reports X and Y the other way around.
_EVDEV_BUTTONS = {
    0x130: "A",              # BTN_SOUTH / BTN_A
    0x131: "B",              # BTN_EAST / BTN_B
    0x133: "X",              # BTN_NORTH / BTN_X
    0x134: "Y",              # BTN_WEST / BTN_Y
    0x136: "LeftShoulder",   # BTN_TL
    0x137: "RightShoulder",  # BTN_TR
    0x13A: "Back",           # BTN_SELECT
    0x13B: "Start",          # BTN_START
    0x13D: "LeftThumb",      # BTN_THUMBL
    0x13E: "RightThumb",     # BTN_THUMBR
    0x220: "DPadUp",         # BTN_DPAD_UP
    0x221: "DPadDown",       # BTN_DPAD_DOWN
    0x222: "DPadLeft",       # BTN_DPAD_LEFT
    0x223: "DPadRight",      # BTN_DPAD_RIGHT
}

# Some controllers are picked up by the generic HID driver and report the old
# joystick button codes instead. Those carry no meaning of their own, so they
# are assigned in the order the pads usually declare them.
_EVDEV_JOYSTICK_BUTTONS = (
    "A", "B", "X", "Y", "LeftShoulder", "RightShoulder",
    "Back", "Start", "LeftThumb", "RightThumb",
)

# Digital triggers, used only when the pad has no analog trigger axes.
_BTN_TL2 = 0x138
_BTN_TR2 = 0x139

_INPUT_EVENT_FORMAT = "llHHi"  # struct input_event
_INPUT_EVENT_SIZE = 24


def _ioc(direction, letter, number, size):
    return (direction << 30) | (size << 16) | (ord(letter) << 8) | number


def _EVIOCGNAME(length):
    return _ioc(2, "E", 0x06, length)


def _EVIOCGKEY(length):
    return _ioc(2, "E", 0x18, length)


def _EVIOCGBIT(event_type, length):
    return _ioc(2, "E", 0x20 + event_type, length)


def _EVIOCGABS(axis):
    return _ioc(2, "E", 0x40 + axis, 24)  # sizeof(struct input_absinfo)


def _test_bit(buffer, bit):
    return bool(buffer[bit >> 3] & (1 << (bit & 7)))


def _read_capability(event_name, kind):
    """Read a capability bitmap of an input device from sysfs.

    They are written as space separated 64 bit words, most significant
    word first. Returns None when sysfs cannot be read.
    """
    path = "/sys/class/input/{}/device/capabilities/{}".format(event_name, kind)
    try:
        with open(path) as capabilities:
            words = capabilities.read().split()
    except OSError:
        return None

    bits = 0
    for shift, word in enumerate(reversed(words)):
        try:
            bits |= int(word, 16) << (shift * 64)
        except ValueError:
            return None
    return bits


def _sysfs_is_gamepad(path):
    """Guess from sysfs whether a device is a gamepad.

    This keeps the scan from opening every keyboard, mouse and tablet on
    the system. Returns None when sysfs has no answer, in which case the
    device has to be opened to find out.
    """
    event_name = os.path.basename(path)
    keys = _read_capability(event_name, "key")
    axes = _read_capability(event_name, "abs")

    if keys is None or axes is None:
        return None

    def mask(first, last):
        return ((1 << (last + 1)) - 1) ^ ((1 << first) - 1)

    has_stick = bool(axes & (1 << _ABS_X))
    has_buttons = bool(keys & (mask(_BTN_JOYSTICK, _BTN_GAMEPAD_LAST)
                               | mask(_BTN_DPAD_UP, _BTN_DPAD_RIGHT)))

    return has_stick and has_buttons


class _EvdevDevice:
    """A single opened /dev/input/event* node."""

    def __init__(self, path):
        import fcntl
        import struct

        self._fcntl = fcntl
        self._struct = struct
        self.path = path
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

        try:
            self.name = self._read_name()
            self._key_bits = self._read_bits(_EV_KEY, (_KEY_MAX + 7) // 8)
            self._abs_bits = self._read_bits(_EV_ABS, (_ABS_MAX + 7) // 8)
            self._buttons = self._build_button_map()
            self._axis_info = {}
            self._axes = {}
            self._keys = {}
            self._read_initial_state()
        except Exception:
            self.close()
            raise

    #-- setup ------------------------------------------------------------------

    def _ioctl(self, request, size):
        buffer = bytearray(size)
        self._fcntl.ioctl(self._fd, request, buffer)
        return buffer

    def _read_name(self):
        try:
            raw = self._ioctl(_EVIOCGNAME(256), 256)
        except OSError:
            return os.path.basename(self.path)
        return raw.split(b"\x00")[0].decode("utf-8", "replace")

    def _read_bits(self, event_type, size):
        try:
            return self._ioctl(_EVIOCGBIT(event_type, size), size)
        except OSError:
            return bytearray(size)

    def _build_button_map(self):
        """Map the evdev key codes this device reports to property names."""
        mapping = {code: name for code, name in _EVDEV_BUTTONS.items()
                   if _test_bit(self._key_bits, code)}

        if not mapping:
            # Fall back to the legacy joystick button codes.
            codes = [code for code in range(_BTN_JOYSTICK, _BTN_JOYSTICK_LAST + 1)
                     if _test_bit(self._key_bits, code)]
            mapping = dict(zip(codes, _EVDEV_JOYSTICK_BUTTONS))

        return mapping

    def _read_initial_state(self):
        for axis in (_ABS_X, _ABS_Y, _ABS_Z, _ABS_RX, _ABS_RY, _ABS_RZ,
                     _ABS_HAT0X, _ABS_HAT0Y):
            if not _test_bit(self._abs_bits, axis):
                continue
            try:
                raw = self._ioctl(_EVIOCGABS(axis), 24)
            except OSError:
                continue
            value, minimum, maximum = self._struct.unpack_from("iii", raw)
            if maximum <= minimum:
                continue
            self._axis_info[axis] = (minimum, maximum)
            self._axes[axis] = value

        size = (_KEY_MAX + 7) // 8
        try:
            state = self._ioctl(_EVIOCGKEY(size), size)
        except OSError:
            return
        for code in list(self._buttons) + [_BTN_TL2, _BTN_TR2]:
            self._keys[code] = _test_bit(state, code)

    def is_gamepad(self):
        """A gamepad has at least one stick and at least one known button."""
        return _ABS_X in self._axis_info and bool(self._buttons)

    #-- polling ----------------------------------------------------------------

    def read_events(self):
        """Drain everything the kernel has queued up for this device."""
        unpack = self._struct.unpack_from

        while True:
            try:
                data = os.read(self._fd, _INPUT_EVENT_SIZE * 64)
            except BlockingIOError:
                return
            except InterruptedError:
                continue

            if not data:
                return

            for offset in range(0, len(data) - _INPUT_EVENT_SIZE + 1,
                                _INPUT_EVENT_SIZE):
                _, _, event_type, code, value = unpack(
                    _INPUT_EVENT_FORMAT, data, offset)

                if event_type == _EV_KEY:
                    self._keys[code] = value != 0
                elif event_type == _EV_ABS:
                    self._axes[code] = value

            if len(data) < _INPUT_EVENT_SIZE * 64:
                return

    def _axis(self, axis, invert=False):
        """Return an axis as -1 .. 1, or 0 if the device has no such axis."""
        info = self._axis_info.get(axis)
        if info is None:
            return 0.0

        minimum, maximum = info
        value = self._axes.get(axis, 0)
        value = min(max(value, minimum), maximum)
        normalized = (value - minimum) / (maximum - minimum) * 2.0 - 1.0
        return -normalized if invert else normalized

    def _trigger(self, axis, digital_code):
        """Return a trigger as 0 .. 255, on the scale XInput reports."""
        if axis in self._axis_info:
            return (self._axis(axis) + 1.0) * 0.5 * TRIGGER_RANGE
        return TRIGGER_RANGE if self._keys.get(digital_code) else 0.0

    def poll(self):
        self.read_events()

        buttons = {}
        for code, name in self._buttons.items():
            if self._keys.get(code):
                buttons[name] = True

        # Most pads report the d-pad as a hat instead of as buttons.
        hat_x = self._axis(_ABS_HAT0X)
        hat_y = self._axis(_ABS_HAT0Y)
        if hat_x < -0.5:
            buttons["DPadLeft"] = True
        elif hat_x > 0.5:
            buttons["DPadRight"] = True
        if hat_y < -0.5:
            buttons["DPadUp"] = True
        elif hat_y > 0.5:
            buttons["DPadDown"] = True

        # evdev has Y pointing down, XInput has it pointing up.
        thumbs = (
            self._axis(_ABS_X) * THUMB_RANGE,
            self._axis(_ABS_Y, invert=True) * THUMB_RANGE,
            self._axis(_ABS_RX) * THUMB_RANGE,
            self._axis(_ABS_RY, invert=True) * THUMB_RANGE,
        )

        triggers = (
            self._trigger(_ABS_Z, _BTN_TL2),
            self._trigger(_ABS_RZ, _BTN_TR2),
        )

        return _make_values(buttons, thumbs, triggers)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class EvdevBackend:
    """Polls a controller through the Linux evdev interface."""

    platform_name = "evdev"

    # How long to wait before looking for a controller again, in seconds.
    RESCAN_INTERVAL = 1.0

    def __init__(self, index=0):
        if not os.path.isdir("/dev/input"):
            raise GamepadError("/dev/input is not available on this system")

        self.index = index
        self._device = None
        self._next_scan = 0.0
        self._last_error = ""

    #-- device discovery -------------------------------------------------------

    @staticmethod
    def _event_paths():
        try:
            entries = os.listdir("/dev/input")
        except OSError:
            return []

        paths = []
        for entry in entries:
            if not entry.startswith("event"):
                continue
            try:
                number = int(entry[5:])
            except ValueError:
                continue
            paths.append((number, os.path.join("/dev/input", entry)))

        return [path for _, path in sorted(paths)]

    @classmethod
    def _open_gamepads(cls, wanted_index=None):
        """Open the gamepads found in /dev/input.

        With no wanted index every gamepad is returned, otherwise only the
        one at that index is, and the others are closed again. The second
        return value tells whether a device had to be skipped because it
        could not be opened.
        """
        found = []
        permission_denied = False
        seen = 0

        for path in cls._event_paths():
            looks_like_gamepad = _sysfs_is_gamepad(path)
            if looks_like_gamepad is False:
                continue

            try:
                device = _EvdevDevice(path)
            except PermissionError:
                # Anything that is not a gamepad was skipped above, so this
                # is a controller we are not allowed to read.
                permission_denied = True
                continue
            except OSError:
                continue

            if not device.is_gamepad():
                device.close()
                continue

            if wanted_index is None:
                found.append(device)
                continue

            if seen == wanted_index:
                found.append(device)
                break

            seen += 1
            device.close()

        return found, permission_denied

    def list_devices(self):
        devices, permission_denied = self._open_gamepads()
        names = ["{} ({})".format(device.name, device.path) for device in devices]
        for device in devices:
            device.close()

        if not names and permission_denied:
            raise GamepadError(
                "No readable gamepad found. The input devices in /dev/input "
                "are not readable by this user, add yourself to the 'input' "
                "group and log back in")

        return names

    def _ensure_device(self):
        if self._device is not None:
            return self._device

        now = time.monotonic()
        if now < self._next_scan:
            return None
        self._next_scan = now + self.RESCAN_INTERVAL

        devices, permission_denied = self._open_gamepads(self.index)
        self._device = devices[0] if devices else None

        if self._device is None and permission_denied:
            self._last_error = (
                "A gamepad was found but is not readable by this user, add "
                "yourself to the 'input' group and log back in")
        else:
            self._last_error = ""

        return self._device

    #-- polling ----------------------------------------------------------------

    def poll(self):
        device = self._ensure_device()
        if device is None:
            return None

        try:
            return device.poll()
        except OSError:
            # The controller was unplugged, look for it again later.
            device.close()
            self._device = None
            return None

    def describe(self):
        device = self._ensure_device()
        if device is not None:
            return device.name
        if self._last_error:
            return self._last_error
        return "No controller connected"

    def close(self):
        if self._device is not None:
            self._device.close()
            self._device = None


#------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------BACKEND SELECTION---------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


def get_backend_class():
    """Return the backend class for this platform."""
    if sys.platform == "win32":
        return XInputBackend
    if sys.platform.startswith("linux"):
        return EvdevBackend
    raise GamepadError(
        "Gamepad input is only supported on Windows and Linux, "
        "this is '{}'".format(sys.platform))


def open_gamepad(index=0):
    """Create a backend for this platform, raises GamepadError if there is none."""
    return get_backend_class()(index)


def list_devices():
    """Return a list of human readable names for the connected gamepads."""
    backend = open_gamepad()
    try:
        return backend.list_devices()
    finally:
        backend.close()
