#!/usr/bin/env python3
"""receiver.py — TCP server that receives an MPEGTS/H.264 game stream.

Listens on a TCP port, accepts a single sender connection, and:
  - Decodes via ffmpeg subprocess to raw RGB24 frames
  - Renders frames in a pygame window with low-latency display
  - Captures mouse + keyboard input and sends to sender via UDP
  - Optionally saves the raw .ts stream to disk

Usage:
    python3 receiver.py                                    # listen :9000, auto-play
    python3 receiver.py --sender-host 192.168.86.33        # send input to sender
    python3 receiver.py --save game.ts                     # save + play
    python3 receiver.py --save game.ts --no-play           # save only
"""

import argparse
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

STREAM_WIDTH = 1280
STREAM_HEIGHT = 720
FRAME_SIZE = STREAM_WIDTH * STREAM_HEIGHT * 3  # RGB24

# Binary event protocol: 13 bytes per event, little-endian
# | type(u8) | timestamp(u32) | arg1(i16) | arg2(i16) | arg3(i16) | arg4(i16) |
EVENT_FMT = "<BIhhhh"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# Event type constants
EVT_MOUSE_MOVE = 0
EVT_MOUSE_DOWN = 1
EVT_MOUSE_UP = 2
EVT_KEY_DOWN = 3
EVT_KEY_UP = 4


def log(msg):
    print(f"[receiver] {msg}", flush=True)


def build_ffmpeg_decode_cmd() -> list[str]:
    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-fflags", "nobuffer+fastseek",
        "-flags", "low_delay",
        "-analyzeduration", "100000",
        "-probesize", "32768",
        "-i", "pipe:0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{STREAM_WIDTH}x{STREAM_HEIGHT}",
        "-vsync", "drop",
        "pipe:1",
    ]


def make_event(evt_type, arg1=0, arg2=0, arg3=0, arg4=0):
    ts = int(time.monotonic() * 1000) & 0xFFFFFFFF
    return struct.pack(EVENT_FMT, evt_type, ts, arg1, arg2, arg3, arg4)


def map_mouse_coords(pos, window_size):
    """Map window pixel coordinates to stream resolution."""
    wx, wy = pos
    ww, wh = window_size
    sx = int(wx * STREAM_WIDTH / ww)
    sy = int(wy * STREAM_HEIGHT / wh)
    sx = max(0, min(STREAM_WIDTH - 1, sx))
    sy = max(0, min(STREAM_HEIGHT - 1, sy))
    return sx, sy


# Map pygame key constants to evdev key codes (linux/input-event-codes.h)
def build_keymap():
    """Build a mapping from pygame key constants to Linux evdev key codes."""
    import pygame
    m = {}
    # Letters A-Z: pygame.K_a=97..K_z=122, evdev KEY_A=30..KEY_Z=52 (non-contiguous)
    evdev_letters = [
        30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50,
        49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44,
    ]
    for i, code in enumerate(evdev_letters):
        m[pygame.K_a + i] = code
    # Digits 0-9: pygame.K_0=48..K_9=57, evdev KEY_0=11, KEY_1=2..KEY_9=10
    m[pygame.K_0] = 11
    for i in range(1, 10):
        m[pygame.K_0 + i] = i + 1
    # Common keys
    m[pygame.K_ESCAPE] = 1
    m[pygame.K_RETURN] = 28
    m[pygame.K_SPACE] = 57
    m[pygame.K_BACKSPACE] = 14
    m[pygame.K_TAB] = 15
    m[pygame.K_LSHIFT] = 42
    m[pygame.K_RSHIFT] = 54
    m[pygame.K_LCTRL] = 29
    m[pygame.K_RCTRL] = 97
    m[pygame.K_LALT] = 56
    m[pygame.K_RALT] = 100
    m[pygame.K_UP] = 103
    m[pygame.K_DOWN] = 108
    m[pygame.K_LEFT] = 105
    m[pygame.K_RIGHT] = 106
    m[pygame.K_F1] = 59
    m[pygame.K_F2] = 60
    m[pygame.K_F3] = 61
    m[pygame.K_F4] = 62
    m[pygame.K_F5] = 63
    m[pygame.K_F6] = 64
    m[pygame.K_F7] = 65
    m[pygame.K_F8] = 66
    m[pygame.K_F9] = 67
    m[pygame.K_F10] = 68
    m[pygame.K_F11] = 87
    m[pygame.K_F12] = 88
    m[pygame.K_DELETE] = 111
    m[pygame.K_HOME] = 102
    m[pygame.K_END] = 107
    m[pygame.K_PAGEUP] = 104
    m[pygame.K_PAGEDOWN] = 109
    m[pygame.K_MINUS] = 12
    m[pygame.K_EQUALS] = 13
    m[pygame.K_LEFTBRACKET] = 26
    m[pygame.K_RIGHTBRACKET] = 27
    m[pygame.K_SEMICOLON] = 39
    m[pygame.K_QUOTE] = 40
    m[pygame.K_BACKQUOTE] = 41
    m[pygame.K_BACKSLASH] = 43
    m[pygame.K_COMMA] = 51
    m[pygame.K_PERIOD] = 52
    m[pygame.K_SLASH] = 53
    return m


def input_sender_loop(udp_sock, sender_addr, keymap, running_event):
    """Pygame event loop: capture mouse/keyboard and send UDP packets."""
    import pygame

    while running_event.is_set():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running_event.clear()
                return

            if event.type == pygame.VIDEORESIZE:
                # Window resized — no packet needed, just let pygame handle it
                continue

            screen = pygame.display.get_surface()
            if screen is None:
                continue
            win_size = screen.get_size()

            if sender_addr is None:
                continue

            if event.type == pygame.MOUSEMOTION:
                sx, sy = map_mouse_coords(event.pos, win_size)
                pkt = make_event(EVT_MOUSE_MOVE, sx, sy, event.rel[0], event.rel[1])
                udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.MOUSEBUTTONDOWN:
                sx, sy = map_mouse_coords(event.pos, win_size)
                pkt = make_event(EVT_MOUSE_DOWN, sx, sy, event.button, 0)
                udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.MOUSEBUTTONUP:
                sx, sy = map_mouse_coords(event.pos, win_size)
                pkt = make_event(EVT_MOUSE_UP, sx, sy, event.button, 0)
                udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.KEYDOWN:
                evdev_code = keymap.get(event.key, 0)
                if evdev_code:
                    pkt = make_event(EVT_KEY_DOWN, evdev_code, 0, 0, 0)
                    udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.KEYUP:
                evdev_code = keymap.get(event.key, 0)
                if evdev_code:
                    pkt = make_event(EVT_KEY_UP, evdev_code, 0, 0, 0)
                    udp_sock.sendto(pkt, sender_addr)

        time.sleep(0.002)


def receive_and_decode(conn, save_file, ffmpeg_proc, screen, running_event,
                       udp_sock, sender_addr, keymap):
    """Main loop: receive TCP stream, optionally save, feed ffmpeg, render frames."""
    import pygame

    start_time = time.monotonic()
    total_bytes = 0
    last_report = start_time

    # Start input sender on the main thread (pygame events must be on main thread)
    # So we read frames in a background thread instead
    frame_buf = bytearray(FRAME_SIZE)
    frame_offset = 0

    def tcp_reader():
        """Read TCP data, write to save file and ffmpeg stdin."""
        nonlocal total_bytes, last_report
        try:
            while running_event.is_set():
                data = conn.recv(65536)
                if not data:
                    log("Sender disconnected.")
                    running_event.clear()
                    return
                total_bytes += len(data)

                if save_file is not None:
                    save_file.write(data)

                if ffmpeg_proc is not None and ffmpeg_proc.stdin:
                    try:
                        ffmpeg_proc.stdin.write(data)
                    except BrokenPipeError:
                        log("ffmpeg decode pipe broken.")
                        running_event.clear()
                        return

                now = time.monotonic()
                if now - last_report >= 5.0:
                    elapsed = now - start_time
                    mb = total_bytes / (1024 * 1024)
                    mbps = (total_bytes * 8) / (elapsed * 1_000_000)
                    log(f"  {mb:.1f} MB received | {mbps:.2f} Mbps | {elapsed:.0f}s")
                    last_report = now
        except (ConnectionResetError, BrokenPipeError):
            log("Connection lost.")
            running_event.clear()

    reader_thread = threading.Thread(target=tcp_reader, daemon=True)
    reader_thread.start()

    # Main thread: read decoded frames from ffmpeg stdout + handle pygame events
    frame_offset = 0
    touching = False  # track finger-on-screen state (left mouse button)
    while running_event.is_set():
        # Handle pygame events (must be on main thread)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running_event.clear()
                break

            if event.type == pygame.VIDEORESIZE:
                continue

            if sender_addr is None:
                continue

            win_size = screen.get_size()

            if event.type == pygame.MOUSEMOTION:
                # Only send drag events while finger is down (left button held)
                if touching:
                    sx, sy = map_mouse_coords(event.pos, win_size)
                    pkt = make_event(EVT_MOUSE_MOVE, sx, sy, 0, 0)
                    udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                touching = True
                sx, sy = map_mouse_coords(event.pos, win_size)
                pkt = make_event(EVT_MOUSE_DOWN, sx, sy, 0, 0)
                udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                touching = False
                sx, sy = map_mouse_coords(event.pos, win_size)
                pkt = make_event(EVT_MOUSE_UP, sx, sy, 0, 0)
                udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.KEYDOWN:
                evdev_code = keymap.get(event.key, 0)
                if evdev_code:
                    pkt = make_event(EVT_KEY_DOWN, evdev_code, 0, 0, 0)
                    udp_sock.sendto(pkt, sender_addr)

            elif event.type == pygame.KEYUP:
                evdev_code = keymap.get(event.key, 0)
                if evdev_code:
                    pkt = make_event(EVT_KEY_UP, evdev_code, 0, 0, 0)
                    udp_sock.sendto(pkt, sender_addr)

        if not running_event.is_set():
            break

        # Try to read a frame from ffmpeg stdout (non-blocking-ish via small reads)
        if ffmpeg_proc is None or ffmpeg_proc.stdout is None:
            time.sleep(0.01)
            continue

        try:
            needed = FRAME_SIZE - frame_offset
            chunk = ffmpeg_proc.stdout.read(needed)
            if not chunk:
                # ffmpeg exited or EOF
                time.sleep(0.01)
                continue
            frame_buf[frame_offset:frame_offset + len(chunk)] = chunk
            frame_offset += len(chunk)
        except Exception:
            time.sleep(0.01)
            continue

        if frame_offset >= FRAME_SIZE:
            # Full frame ready — render it
            try:
                surface = pygame.image.frombuffer(bytes(frame_buf), (STREAM_WIDTH, STREAM_HEIGHT), "RGB")
                win_size = screen.get_size()
                if win_size != (STREAM_WIDTH, STREAM_HEIGHT):
                    surface = pygame.transform.scale(surface, win_size)
                screen.blit(surface, (0, 0))
                pygame.display.flip()
            except Exception as e:
                log(f"Render error: {e}")
            frame_offset = 0

    reader_thread.join(timeout=2)
    return total_bytes


def receive_stream_no_play(conn, save_file, start_time):
    """Save-only mode: just receive TCP data and write to file."""
    total_bytes = 0
    last_report = start_time

    while True:
        data = conn.recv(65536)
        if not data:
            log("Sender disconnected.")
            break

        total_bytes += len(data)

        if save_file is not None:
            save_file.write(data)

        now = time.monotonic()
        if now - last_report >= 5.0:
            elapsed = now - start_time
            mb = total_bytes / (1024 * 1024)
            mbps = (total_bytes * 8) / (elapsed * 1_000_000)
            log(f"  {mb:.1f} MB received | {mbps:.2f} Mbps | {elapsed:.0f}s")
            last_report = now

    return total_bytes


def cleanup(conn, save_file, ffmpeg_proc, total_bytes, start_time):
    elapsed = time.monotonic() - start_time
    mb = total_bytes / (1024 * 1024)
    log(f"Total: {mb:.1f} MB in {elapsed:.0f}s")

    try:
        conn.close()
    except Exception:
        pass

    if save_file is not None:
        name = save_file.name
        save_file.close()
        log(f"Saved: {name}")

    if ffmpeg_proc is not None:
        try:
            ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            ffmpeg_proc.stdout.close()
        except Exception:
            pass
        ffmpeg_proc.terminate()
        try:
            ffmpeg_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.kill()


def main():
    ap = argparse.ArgumentParser(description="Receive MPEGTS/H.264 game stream over TCP")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9000, help="Listen port (default: 9000)")
    ap.add_argument("--save", metavar="FILE", default=None,
                    help="Save raw stream to file (e.g. game.ts)")
    ap.add_argument("--play", action="store_true", default=True,
                    help="Launch viewer for live viewing (default: on)")
    ap.add_argument("--no-play", dest="play", action="store_false",
                    help="Disable live viewing")
    ap.add_argument("--sender-host", default=None,
                    help="Sender IP for input forwarding (UDP port 9001)")
    ap.add_argument("--input-port", type=int, default=9001,
                    help="UDP port for input forwarding (default: 9001)")
    args = ap.parse_args()

    log("=== Game Stream Receiver ===")
    log(f"Listening on {args.host}:{args.port}")
    if args.save:
        log(f"Save file: {args.save}")
    log(f"Live playback: {'on' if args.play else 'off'}")
    if args.sender_host:
        log(f"Input forwarding: {args.sender_host}:{args.input_port}")
    else:
        log("Input forwarding: off (use --sender-host to enable)")

    # Set up UDP socket for input events
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender_addr = (args.sender_host, args.input_port) if args.sender_host else None

    # Init pygame if playing
    screen = None
    keymap = {}
    if args.play:
        import pygame
        pygame.init()
        screen = pygame.display.set_mode(
            (STREAM_WIDTH, STREAM_HEIGHT), pygame.RESIZABLE
        )
        pygame.display.set_caption("Waydroid Stream")
        keymap = build_keymap()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(1)

    def on_signal(sig, _frame):
        log("Interrupted — shutting down.")
        srv.close()
        if args.play:
            import pygame
            pygame.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while True:
        log("Waiting for sender to connect...")
        conn, addr = srv.accept()
        log(f"Sender connected: {addr[0]}:{addr[1]}")

        conn.settimeout(10)
        try:
            first = conn.recv(65536)
        except socket.timeout:
            log("Connection timed out with no data — ignoring.")
            conn.close()
            continue
        if not first:
            log("Empty connection (health check?) — waiting for real sender.")
            conn.close()
            continue
        conn.settimeout(None)

        save_file = open(args.save, "wb") if args.save else None
        start_time = time.monotonic()

        if not args.play:
            # Save-only mode
            total_bytes = len(first)
            if save_file is not None:
                save_file.write(first)
            try:
                total_bytes += receive_stream_no_play(conn, save_file, start_time)
            except (ConnectionResetError, BrokenPipeError):
                log("Connection lost.")
            cleanup(conn, save_file, None, total_bytes, start_time)
            log("Ready for next connection.\n")
            continue

        # Play mode: start ffmpeg decode subprocess
        cmd = build_ffmpeg_decode_cmd()
        log(f"Starting decoder: {' '.join(cmd)}")
        ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        # Write the first chunk
        total_bytes = len(first)
        if save_file is not None:
            save_file.write(first)
        if ffmpeg_proc.stdin:
            try:
                ffmpeg_proc.stdin.write(first)
            except BrokenPipeError:
                ffmpeg_proc = None

        running = threading.Event()
        running.set()

        try:
            total_bytes += receive_and_decode(
                conn, save_file, ffmpeg_proc, screen, running,
                udp_sock, sender_addr, keymap,
            )
        except (ConnectionResetError, BrokenPipeError):
            log("Connection lost.")

        cleanup(conn, save_file, ffmpeg_proc, total_bytes, start_time)
        log("Ready for next connection.\n")


if __name__ == "__main__":
    main()
