#!/usr/bin/env python3
"""input_server.py — UDP input event receiver + uinput injector.

Listens for binary input events from the receiver (Windows) and injects
them into the Linux kernel via python-evdev UInput. Weston/libinput picks
them up and forwards to Waydroid.

Requires:
  - pip install evdev
  - User must be in the 'input' group, or run as root, for /dev/uinput access

Usage:
    python3 input_server.py                     # listen on 0.0.0.0:9001
    python3 input_server.py --port 9001         # explicit port
"""

import argparse
import signal
import socket
import struct
import sys

from evdev import UInput, AbsInfo, ecodes

# Binary event protocol: 13 bytes per event, little-endian
# | type(u8) | timestamp(u32) | arg1(i16) | arg2(i16) | arg3(i16) | arg4(i16) |
EVENT_FMT = "<BIhhhh"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# Event type constants (must match receiver.py)
EVT_MOUSE_MOVE = 0
EVT_MOUSE_DOWN = 1
EVT_MOUSE_UP = 2
EVT_KEY_DOWN = 3
EVT_KEY_UP = 4

# Stream resolution (must match receiver.py)
STREAM_WIDTH = 1280
STREAM_HEIGHT = 720

# Common keyboard keys to register with UInput
KEYBOARD_KEYS = [
    ecodes.KEY_ESC, ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
    ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8, ecodes.KEY_9,
    ecodes.KEY_0, ecodes.KEY_MINUS, ecodes.KEY_EQUAL, ecodes.KEY_BACKSPACE,
    ecodes.KEY_TAB, ecodes.KEY_Q, ecodes.KEY_W, ecodes.KEY_E, ecodes.KEY_R,
    ecodes.KEY_T, ecodes.KEY_Y, ecodes.KEY_U, ecodes.KEY_I, ecodes.KEY_O,
    ecodes.KEY_P, ecodes.KEY_LEFTBRACE, ecodes.KEY_RIGHTBRACE, ecodes.KEY_ENTER,
    ecodes.KEY_LEFTCTRL, ecodes.KEY_A, ecodes.KEY_S, ecodes.KEY_D, ecodes.KEY_F,
    ecodes.KEY_G, ecodes.KEY_H, ecodes.KEY_J, ecodes.KEY_K, ecodes.KEY_L,
    ecodes.KEY_SEMICOLON, ecodes.KEY_APOSTROPHE, ecodes.KEY_GRAVE,
    ecodes.KEY_LEFTSHIFT, ecodes.KEY_BACKSLASH, ecodes.KEY_Z, ecodes.KEY_X,
    ecodes.KEY_C, ecodes.KEY_V, ecodes.KEY_B, ecodes.KEY_N, ecodes.KEY_M,
    ecodes.KEY_COMMA, ecodes.KEY_DOT, ecodes.KEY_SLASH, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTALT, ecodes.KEY_SPACE, ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_RIGHTALT, ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT,
    ecodes.KEY_RIGHT, ecodes.KEY_DELETE, ecodes.KEY_HOME, ecodes.KEY_END,
    ecodes.KEY_PAGEUP, ecodes.KEY_PAGEDOWN,
    ecodes.KEY_F1, ecodes.KEY_F2, ecodes.KEY_F3, ecodes.KEY_F4,
    ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8,
    ecodes.KEY_F9, ecodes.KEY_F10, ecodes.KEY_F11, ecodes.KEY_F12,
]


def log(msg):
    print(f"[input_server] {msg}", flush=True)


def create_uinput_device():
    """Create a virtual touchscreen device (INPUT_PROP_DIRECT + MT type B)."""
    capabilities = {
        ecodes.EV_ABS: [
            (ecodes.ABS_X, AbsInfo(value=0, min=0, max=STREAM_WIDTH - 1,
                                   fuzz=0, flat=0, resolution=0)),
            (ecodes.ABS_Y, AbsInfo(value=0, min=0, max=STREAM_HEIGHT - 1,
                                   fuzz=0, flat=0, resolution=0)),
            (ecodes.ABS_MT_SLOT, AbsInfo(value=0, min=0, max=9,
                                         fuzz=0, flat=0, resolution=0)),
            (ecodes.ABS_MT_TRACKING_ID, AbsInfo(value=0, min=-1, max=9,
                                                 fuzz=0, flat=0, resolution=0)),
            (ecodes.ABS_MT_POSITION_X, AbsInfo(value=0, min=0, max=STREAM_WIDTH - 1,
                                                fuzz=0, flat=0, resolution=0)),
            (ecodes.ABS_MT_POSITION_Y, AbsInfo(value=0, min=0, max=STREAM_HEIGHT - 1,
                                                fuzz=0, flat=0, resolution=0)),
        ],
        ecodes.EV_KEY: [
            ecodes.BTN_TOUCH,
        ] + KEYBOARD_KEYS,
    }

    dev = UInput(events=capabilities, name="waydroid-stream-touch",
                 vendor=0x1234, product=0x5678,
                 input_props=[ecodes.INPUT_PROP_DIRECT])
    log(f"Created virtual touchscreen: {dev.device.path}")
    return dev


# Track which touch slots are currently active
active_slots = set()


def handle_event(dev, evt_type, arg1, arg2, arg3, arg4):
    """Inject a single input event into the virtual touchscreen.

    arg3 carries the touch slot ID (0-9). Backward compatible: single-touch
    senders send slot 0.
    """
    slot = max(0, min(9, arg3))

    if evt_type == EVT_MOUSE_MOVE:
        # arg1=abs_x, arg2=abs_y — finger drag (only sent while touching)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, arg1)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, arg2)
        if slot == 0:
            dev.write(ecodes.EV_ABS, ecodes.ABS_X, arg1)
            dev.write(ecodes.EV_ABS, ecodes.ABS_Y, arg2)
        dev.syn()

    elif evt_type == EVT_MOUSE_DOWN:
        # arg1=abs_x, arg2=abs_y — finger touch down
        active_slots.add(slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, arg1)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, arg2)
        if len(active_slots) == 1:
            dev.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)
        if slot == 0:
            dev.write(ecodes.EV_ABS, ecodes.ABS_X, arg1)
            dev.write(ecodes.EV_ABS, ecodes.ABS_Y, arg2)
        dev.syn()

    elif evt_type == EVT_MOUSE_UP:
        # finger lift
        active_slots.discard(slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
        dev.write(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1)
        if not active_slots:
            dev.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
        dev.syn()

    elif evt_type == EVT_KEY_DOWN:
        # arg1=evdev keycode
        dev.write(ecodes.EV_KEY, arg1, 1)
        dev.syn()

    elif evt_type == EVT_KEY_UP:
        # arg1=evdev keycode
        dev.write(ecodes.EV_KEY, arg1, 0)
        dev.syn()


def main():
    ap = argparse.ArgumentParser(description="UDP input event receiver + uinput injector")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9001, help="Listen port (default: 9001)")
    args = ap.parse_args()

    log("=== Input Server ===")

    dev = create_uinput_device()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    log(f"Listening on {args.host}:{args.port}")

    def on_signal(sig, _frame):
        log("Shutting down.")
        sock.close()
        dev.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    event_count = 0
    while True:
        try:
            data, addr = sock.recvfrom(256)
        except OSError:
            break

        if len(data) < EVENT_SIZE:
            continue

        # Process all complete events in the datagram
        offset = 0
        while offset + EVENT_SIZE <= len(data):
            evt_type, ts, arg1, arg2, arg3, arg4 = struct.unpack_from(
                EVENT_FMT, data, offset
            )
            offset += EVENT_SIZE

            try:
                handle_event(dev, evt_type, arg1, arg2, arg3, arg4)
                event_count += 1
            except Exception as e:
                log(f"Event injection error: {e}")

            if event_count % 500 == 0 and event_count > 0:
                log(f"  {event_count} events injected")


if __name__ == "__main__":
    main()
