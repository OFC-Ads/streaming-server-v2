#!/usr/bin/env bash
set -euo pipefail

# sender.sh — Start Waydroid, launch Empires & Puzzles, capture display, stream H.264 over TCP
#
# Usage:
#   ./sender.sh                             # defaults: portal capture, receiver at 192.168.86.33:9000
#   STREAM_HOST=10.0.0.5 ./sender.sh       # override receiver address
#   CAPTURE_METHOD=headless ./sender.sh     # headless: weston virtual display, no monitor needed (SSH-friendly)
#   CAPTURE_METHOD=test ./sender.sh         # test pipeline without Waydroid (videotestsrc)
#   CAPTURE_METHOD=x11grab DISPLAY=:0 ./sender.sh  # X11 fallback
#   INPUT_SERVER=0 ./sender.sh                     # disable input server
#
# Start the receiver FIRST, then run this script.

RECEIVER_HOST="${STREAM_HOST:-192.168.86.29}"
RECEIVER_PORT="${STREAM_PORT:-9000}"
CAPTURE_METHOD="${CAPTURE_METHOD:-portal}"
GAME_PACKAGE="${GAME_PACKAGE:-}"
FRAMERATE="${FRAMERATE:-30}"
BITRATE="${BITRATE:-4000}"
HEADLESS_WIDTH="${HEADLESS_WIDTH:-1280}"
HEADLESS_HEIGHT="${HEADLESS_HEIGHT:-720}"
INPUT_SERVER="${INPUT_SERVER:-1}"
INPUT_PORT="${INPUT_PORT:-9001}"

WESTON_PID=""
INPUT_SERVER_PID=""
WESTON_SOCKET="waydroid-stream"

log() { echo "[sender] $(date +%T) $*" >&2; }

# ---------------------------------------------------------------------------
# Cleanup — kill weston on exit if we started it
# ---------------------------------------------------------------------------
cleanup() {
    log "Cleaning up..."
    if [ -n "$INPUT_SERVER_PID" ] && kill -0 "$INPUT_SERVER_PID" 2>/dev/null; then
        log "Stopping input_server (PID $INPUT_SERVER_PID)"
        kill "$INPUT_SERVER_PID" 2>/dev/null || true
        wait "$INPUT_SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "$WESTON_PID" ] && kill -0 "$WESTON_PID" 2>/dev/null; then
        log "Stopping weston (PID $WESTON_PID)"
        kill "$WESTON_PID" 2>/dev/null || true
        wait "$WESTON_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1 — Start Waydroid session
# ---------------------------------------------------------------------------
restart_waydroid_session() {
    # In headless mode, Waydroid must connect to the NEW weston compositor.
    # A leftover session from a previous compositor won't render anything.
    if waydroid status 2>&1 | grep -q "RUNNING"; then
        log "Stopping stale Waydroid session..."
        waydroid session stop 2>&1 || true
        sleep 2
    fi
}

start_waydroid() {
    log "Checking Waydroid status..."
    if waydroid status 2>&1 | grep -q "RUNNING"; then
        log "Waydroid session already running."
        return 0
    fi

    log "Starting Waydroid session..."
    waydroid session start &
    disown

    for i in $(seq 1 24); do
        if waydroid status 2>&1 | grep -q "RUNNING"; then
            log "Waydroid session is running."
            return 0
        fi
        sleep 5
    done
    log "ERROR: Waydroid did not start within 2 minutes."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 2 — Find & launch the game
# ---------------------------------------------------------------------------
launch_game() {
    local pkg=""

    # If the user supplied a package name, use it directly
    if [ -n "$GAME_PACKAGE" ]; then
        pkg="$GAME_PACKAGE"
    else
        # Search the app list for Empires & Puzzles
        local listing
        listing=$(waydroid app list 2>&1 || true)

        # The listing format is "Name: <name>" on one line and "packageName: <pkg>" on the next.
        pkg=$(echo "$listing" | awk '/[Ee]mpire/{getline; print $2}' | head -1)

        if [ -z "$pkg" ]; then
            # Try well-known package name
            pkg="com.smallgiantgames.empires"
        fi
    fi

    log "Launching package: $pkg"
    if ! waydroid app launch "$pkg" 2>&1; then
        log "WARNING: launch command returned an error — the game may still start."
    fi
}

# ---------------------------------------------------------------------------
# Start input_server.py for receiving remote input events
# ---------------------------------------------------------------------------
start_input_server() {
    if [ "$INPUT_SERVER" != "1" ]; then
        log "Input server disabled (INPUT_SERVER=0)"
        return 0
    fi

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local server_script="${script_dir}/input_server.py"

    if [ ! -f "$server_script" ]; then
        log "WARNING: input_server.py not found at $server_script — skipping"
        return 0
    fi

    log "Starting input server on port $INPUT_PORT..."
    python3 "$server_script" --port "$INPUT_PORT" &
    INPUT_SERVER_PID=$!
    log "Input server PID: $INPUT_SERVER_PID"
}

# ---------------------------------------------------------------------------
# Step 3a — Wait for the receiver to be reachable
# ---------------------------------------------------------------------------
wait_for_receiver() {
    log "Waiting for receiver at $RECEIVER_HOST:$RECEIVER_PORT ..."
    # Skip the check — the receiver loops on connections now, so GStreamer
    # can just connect directly. If the receiver isn't up, GStreamer will
    # fail with a clear "connection refused" error.
    log "Receiver check skipped (receiver handles reconnection)."
    return 0
}

# ---------------------------------------------------------------------------
# Step 3b — Headless: start Weston virtual compositor (no monitor needed)
# ---------------------------------------------------------------------------
start_weston_headless() {
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

    # Kill any leftover weston using our socket and clean stale files
    local socket_path="${XDG_RUNTIME_DIR}/${WESTON_SOCKET}"
    if [ -e "${socket_path}.lock" ]; then
        log "Cleaning stale weston socket..."
        pkill -f "weston.*${WESTON_SOCKET}" 2>/dev/null || true
        sleep 1
        rm -f "$socket_path" "${socket_path}.lock"
    fi

    log "Starting weston PipeWire compositor (${HEADLESS_WIDTH}x${HEADLESS_HEIGHT})..."

    # Single pipewire backend: Waydroid renders directly to the PipeWire output,
    # so GStreamer's pipewiresrc can capture real frames. Dual-backend (headless+pipewire)
    # doesn't work because windows only appear on the headless output.
    local cfg
    cfg=$(mktemp /tmp/weston-headless-XXXXX.ini)
    cat > "$cfg" <<WINI
[output]
name=pipewire
mode=${HEADLESS_WIDTH}x${HEADLESS_HEIGHT}
WINI

    weston --backend=pipewire --renderer=gl --config="$cfg" --socket="$WESTON_SOCKET" 2>&1 &
    WESTON_PID=$!
    log "Weston PID: $WESTON_PID"

    # Wait for the Wayland socket to appear
    local socket_path="${XDG_RUNTIME_DIR}/${WESTON_SOCKET}"
    for i in $(seq 1 20); do
        if [ -e "$socket_path" ]; then
            log "Weston socket ready: $socket_path"
            export WAYLAND_DISPLAY="$WESTON_SOCKET"
            return 0
        fi
        sleep 0.5
    done
    log "ERROR: Weston socket did not appear at $socket_path"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 3b-2 — Headless: find the PipeWire video node weston created
# ---------------------------------------------------------------------------
find_pipewire_video_node() {
    log "Looking for weston PipeWire video node..."

    local node_id=""
    for i in $(seq 1 20); do
        # pw-dump outputs JSON; find the weston.pipewire video output node
        node_id=$(pw-dump 2>/dev/null \
            | python3 -c "
import json, sys
for obj in json.load(sys.stdin):
    props = obj.get('info', {}).get('props', {})
    mc = props.get('media.class', '')
    if 'Video' in mc and 'weston' in props.get('node.name', ''):
        print(obj['id'])
        break
" 2>/dev/null || true)

        if [ -n "$node_id" ]; then
            log "Found PipeWire video node: $node_id"
            echo "$node_id"
            return 0
        fi
        sleep 0.5
    done
    log "ERROR: No PipeWire video node found from weston."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 3b-3 — Capture: headless weston + PipeWire (no monitor, SSH-friendly)
# ---------------------------------------------------------------------------
capture_headless() {
    local node_id
    node_id=$(find_pipewire_video_node)

    local bitrate_bps=$((BITRATE * 1000))
    log "Starting GStreamer capture from PipeWire node $node_id (HW encode)"
    exec gst-launch-1.0 -e \
        pipewiresrc path="$node_id" do-timestamp=true keepalive-time=1000 ! \
        queue max-size-buffers=3 leaky=downstream ! \
        videoconvert ! \
        videorate ! \
        "video/x-raw,format=NV12,framerate=${FRAMERATE}/1" ! \
        v4l2h264enc extra-controls="controls,video_bitrate=${bitrate_bps};" ! \
        "video/x-h264,profile=baseline,stream-format=byte-stream" ! \
        mpegtsmux ! \
        tcpclientsink host="$RECEIVER_HOST" port="$RECEIVER_PORT"
}

# ---------------------------------------------------------------------------
# Step 3c — Capture: PipeWire portal (primary, GNOME Wayland)
# ---------------------------------------------------------------------------
capture_portal() {
    log "Starting PipeWire portal capture..."
    log "A screen-share dialog will appear — select the Waydroid window."
    export STREAM_HOST STREAM_PORT FRAMERATE BITRATE

    exec python3 - <<'PYEOF'
"""Set up an xdg-desktop-portal ScreenCast session, obtain a PipeWire node,
and exec into a GStreamer pipeline that encodes H.264 and streams MPEGTS
over TCP to the receiver."""
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
import os, sys

RECEIVER_HOST = os.environ.get("STREAM_HOST", "192.168.86.33")
RECEIVER_PORT = os.environ.get("STREAM_PORT", "9000")
FRAMERATE      = os.environ.get("FRAMERATE", "30")
BITRATE        = os.environ.get("BITRATE", "4000")

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
loop = GLib.MainLoop()
bus  = dbus.SessionBus()

portal     = bus.get_object("org.freedesktop.portal.Desktop",
                            "/org/freedesktop/portal/desktop")
screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")

sender_name  = bus.get_unique_name().replace(".", "_").lstrip(":")
token_serial = 0
session_path = None

def next_token():
    global token_serial
    token_serial += 1
    return f"u{token_serial}"

def request_path_for(token):
    return f"/org/freedesktop/portal/desktop/request/{sender_name}/{token}"

# ---------- Portal callback chain ----------
def on_create_session(response, results):
    global session_path
    if response != 0:
        print(f"[portal] CreateSession failed ({response})", file=sys.stderr)
        loop.quit(); return
    session_path = str(results["session_handle"])
    print(f"[portal] Session: {session_path}", file=sys.stderr)

    token = next_token()
    bus.add_signal_receiver(on_select_sources, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=request_path_for(token))
    screencast.SelectSources(session_path, dbus.Dictionary({
        "handle_token": dbus.String(token),
        "types":    dbus.UInt32(1 | 2),   # monitor + window
        "multiple": dbus.Boolean(False),
    }, signature="sv"))

def on_select_sources(response, results):
    if response != 0:
        print(f"[portal] SelectSources failed ({response})", file=sys.stderr)
        loop.quit(); return
    print("[portal] Sources selected — starting capture...", file=sys.stderr)

    token = next_token()
    bus.add_signal_receiver(on_start, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=request_path_for(token))
    screencast.Start(session_path, "", dbus.Dictionary({
        "handle_token": dbus.String(token),
    }, signature="sv"))

def on_start(response, results):
    if response != 0:
        print(f"[portal] Start failed ({response})", file=sys.stderr)
        loop.quit(); return

    streams = results.get("streams", [])
    if not streams:
        print("[portal] No streams returned!", file=sys.stderr)
        loop.quit(); return

    node_id = str(streams[0][0])
    print(f"[portal] PipeWire node: {node_id}", file=sys.stderr)

    pw_fd = screencast.OpenPipeWireRemote(session_path,
                dbus.Dictionary({}, signature="sv"))
    fd = pw_fd.take()
    os.set_inheritable(fd, True)
    print(f"[portal] PipeWire fd: {fd}", file=sys.stderr)

    bitrate_bps = int(BITRATE) * 1000
    cmd = [
        "gst-launch-1.0", "-e",
        "pipewiresrc", f"fd={fd}", f"path={node_id}",
            "do-timestamp=true", "keepalive-time=1000",         "!",
        "videoconvert",                                         "!",
        f"video/x-raw,format=NV12,framerate={FRAMERATE}/1",     "!",
        "v4l2h264enc",
            f"extra-controls=controls,video_bitrate={bitrate_bps};", "!",
        f"video/x-h264,profile=baseline,stream-format=byte-stream", "!",
        "mpegtsmux",                                            "!",
        "tcpclientsink", f"host={RECEIVER_HOST}", f"port={RECEIVER_PORT}",
    ]
    print(f"[portal] Launching: {' '.join(cmd)}", file=sys.stderr)
    loop.quit()
    os.execvp(cmd[0], cmd)

# ---------- Kick off the chain ----------
token = next_token()
bus.add_signal_receiver(on_create_session, signal_name="Response",
                        dbus_interface="org.freedesktop.portal.Request",
                        path=request_path_for(token))

screencast.CreateSession(dbus.Dictionary({
    "handle_token":         dbus.String(token),
    "session_handle_token": dbus.String("waydroid_stream"),
}, signature="sv"))

loop.run()
PYEOF
}

# ---------------------------------------------------------------------------
# Step 3c — Capture: x11grab (fallback for X11 sessions / Xwayland)
# ---------------------------------------------------------------------------
capture_x11grab() {
    local display="${DISPLAY:-:0}"
    log "Using x11grab capture on display $display"
    exec ffmpeg -loglevel warning -stats \
        -f x11grab -framerate "$FRAMERATE" -i "$display" \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -b:v "${BITRATE}k" -maxrate "${BITRATE}k" -bufsize "$((BITRATE * 2))k" \
        -g 60 -bf 0 -profile:v baseline \
        -f mpegts "tcp://${RECEIVER_HOST}:${RECEIVER_PORT}"
}

# ---------------------------------------------------------------------------
# Step 3d — Capture: videotestsrc (for testing the pipeline end-to-end)
# ---------------------------------------------------------------------------
capture_test() {
    local bitrate_bps=$((BITRATE * 1000))
    log "Using videotestsrc (pipeline test — no Waydroid needed, HW encode)"
    exec gst-launch-1.0 -e \
        videotestsrc pattern=ball is-live=true ! \
        "video/x-raw,width=1280,height=720,framerate=${FRAMERATE}/1" ! \
        videoconvert ! \
        "video/x-raw,format=NV12" ! \
        v4l2h264enc extra-controls="controls,video_bitrate=${bitrate_bps};" ! \
        "video/x-h264,profile=baseline,stream-format=byte-stream" ! \
        mpegtsmux ! \
        tcpclientsink host="$RECEIVER_HOST" port="$RECEIVER_PORT"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "=== Waydroid Game Streaming Pipeline ==="
    log "Receiver: ${RECEIVER_HOST}:${RECEIVER_PORT}"
    log "Capture:  ${CAPTURE_METHOD}"
    log "Encode:   H.264 baseline (v4l2 HW), ${BITRATE} kbps, ${FRAMERATE} fps"
    echo

    start_input_server

    if [ "$CAPTURE_METHOD" = "test" ]; then
        wait_for_receiver
        capture_test
        return
    fi

    # Headless needs weston started BEFORE Waydroid, and any old
    # Waydroid session must be stopped so it reconnects to the new compositor.
    if [ "$CAPTURE_METHOD" = "headless" ]; then
        restart_waydroid_session
        start_weston_headless
    fi

    start_waydroid
    sleep 2

    # Show the full Android UI inside the compositor
    log "Launching Waydroid full UI..."
    waydroid show-full-ui &
    disown

    # Wait for Android to be fully ready
    log "Waiting for Android to be ready..."
    for i in $(seq 1 30); do
        if waydroid status 2>&1 | grep -q "Android.*ready"; then
            log "Android is ready."
            break
        fi
        sleep 2
    done
    sleep 3

    launch_game
    log "Waiting 10 seconds for the game to render..."
    sleep 10

    wait_for_receiver

    case "$CAPTURE_METHOD" in
        headless) capture_headless  ;;
        portal)   capture_portal    ;;
        x11grab)  capture_x11grab   ;;
        *)
            log "Unknown capture method: $CAPTURE_METHOD"
            log "Available: headless, portal, x11grab, test"
            exit 1
            ;;
    esac
}

main
