#!/usr/bin/env python3
"""receiver.py — TCP server that receives an MPEGTS/H.264 game stream.

Listens on a TCP port, accepts a single sender connection, and:
  - Optionally saves the raw .ts stream to disk
  - Optionally pipes to ffplay for live low-latency viewing

Usage:
    python3 receiver.py                         # listen :9000, auto-play
    python3 receiver.py --port 9000 --play      # explicit
    python3 receiver.py --save game.ts          # save + play
    python3 receiver.py --save game.ts --no-play  # save only
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time


def log(msg):
    print(f"[receiver] {msg}", flush=True)


def build_ffplay_cmd(extra_args: str) -> list[str]:
    cmd = [
        "ffplay",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-framedrop",
        "-analyzeduration", "100000",
        "-probesize", "32768",
        "-sync", "ext",
        "-window_title", "Waydroid Stream",
        "-i", "pipe:0",
    ]
    if extra_args:
        cmd.extend(extra_args.split())
    return cmd


def receive_stream(conn: socket.socket, save_file, ffplay_proc, start_time: float):
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

        if ffplay_proc is not None and ffplay_proc.stdin:
            try:
                ffplay_proc.stdin.write(data)
            except BrokenPipeError:
                log("ffplay closed — stopping playback pipe.")
                ffplay_proc = None

        now = time.monotonic()
        if now - last_report >= 5.0:
            elapsed = now - start_time
            mb = total_bytes / (1024 * 1024)
            mbps = (total_bytes * 8) / (elapsed * 1_000_000)
            log(f"  {mb:.1f} MB received | {mbps:.2f} Mbps | {elapsed:.0f}s")
            last_report = now

    return total_bytes


def cleanup(conn, save_file, ffplay_proc, total_bytes, start_time):
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

    if ffplay_proc is not None:
        try:
            ffplay_proc.stdin.close()
        except Exception:
            pass
        ffplay_proc.terminate()
        try:
            ffplay_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ffplay_proc.kill()


def main():
    ap = argparse.ArgumentParser(description="Receive MPEGTS/H.264 game stream over TCP")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9000, help="Listen port (default: 9000)")
    ap.add_argument("--save", metavar="FILE", default=None,
                    help="Save raw stream to file (e.g. game.ts)")
    ap.add_argument("--play", action="store_true", default=True,
                    help="Launch ffplay for live viewing (default: on)")
    ap.add_argument("--no-play", dest="play", action="store_false",
                    help="Disable ffplay live viewing")
    ap.add_argument("--ffplay-args", default="",
                    help="Extra arguments for ffplay (quoted string)")
    args = ap.parse_args()

    log("=== Game Stream Receiver ===")
    log(f"Listening on {args.host}:{args.port}")
    if args.save:
        log(f"Save file: {args.save}")
    log(f"Live playback: {'on' if args.play else 'off'}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(1)

    def on_signal(sig, _frame):
        log("Interrupted — shutting down.")
        srv.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # Loop: accept connections, ignoring short-lived ones (e.g. health checks)
    while True:
        log("Waiting for sender to connect...")
        conn, addr = srv.accept()
        log(f"Sender connected: {addr[0]}:{addr[1]}")

        # Peek at first data — skip connections that close immediately
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

        ffplay_proc = None
        if args.play:
            cmd = build_ffplay_cmd(args.ffplay_args)
            log(f"Starting: {' '.join(cmd)}")
            ffplay_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        start_time = time.monotonic()

        # Write the first chunk we already read
        total_bytes = len(first)
        if save_file is not None:
            save_file.write(first)
        if ffplay_proc is not None and ffplay_proc.stdin:
            try:
                ffplay_proc.stdin.write(first)
            except BrokenPipeError:
                ffplay_proc = None

        try:
            total_bytes += receive_stream(conn, save_file, ffplay_proc, start_time)
        except (ConnectionResetError, BrokenPipeError):
            log("Connection lost.")

        cleanup(conn, save_file, ffplay_proc, total_bytes, start_time)
        log("Ready for next connection.\n")


if __name__ == "__main__":
    main()
