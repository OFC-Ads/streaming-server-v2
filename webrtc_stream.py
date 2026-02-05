#!/usr/bin/env python3
"""webrtc_stream.py — WebRTC game streaming server.

Captures video via PipeWire (or videotestsrc for testing), encodes with
Venus HW H.264 encoder, and streams directly to a browser over WebRTC.
Touch/mouse input flows back via a WebRTC data channel using the same
13-byte binary protocol as input_server.py.

Replaces the multi-hop path (sender.sh -> TCP -> web_receiver.py -> ffmpeg
remux -> MSE) with a direct low-latency WebRTC path targeting sub-200ms.

Requirements:
    pip install websockets evdev
    GStreamer 1.x with: gst-plugins-base, gst-plugins-good, gst-plugins-bad
      (specifically: pipewiresrc, videoconvert, v4l2h264enc, webrtcbin)

Usage:
    python3 webrtc_stream.py                          # PipeWire capture
    CAPTURE_METHOD=test python3 webrtc_stream.py      # videotestsrc
    CAPTURE_METHOD=headless python3 webrtc_stream.py  # weston + PipeWire
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import struct
import subprocess
import sys

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstSdp", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import Gst, GstSdp, GstWebRTC, GLib
except (ImportError, ValueError) as e:
    print(f"GStreamer Python bindings required: {e}", file=sys.stderr)
    sys.exit(1)

try:
    import websockets
    from websockets.http11 import Response
    from websockets.datastructures import Headers
except ImportError:
    print("Required: pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    from evdev import UInput, AbsInfo, ecodes
except ImportError:
    print("Required: pip install evdev", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STREAM_WIDTH = 1280
STREAM_HEIGHT = 720
FRAMERATE = 30
BITRATE = 4000000  # 4 Mbps

EVENT_FMT = "<BIhhhh"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

EVT_MOUSE_MOVE = 0
EVT_MOUSE_DOWN = 1
EVT_MOUSE_UP = 2
EVT_KEY_DOWN = 3
EVT_KEY_UP = 4

MAX_TOUCH_SLOTS = 10

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
    print(f"[webrtc] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Inline HTML — WebRTC player with multi-touch
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Game Stream (WebRTC)</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;overflow:hidden;touch-action:none}
video{width:100vw;height:100vh;object-fit:contain;background:#000}
#hud{position:fixed;top:8px;left:8px;color:#0f0;font:13px/1.4 monospace;
     background:rgba(0,0,0,.7);padding:4px 10px;border-radius:4px;
     z-index:10;pointer-events:none}
</style>
</head>
<body>
<div id="hud">Connecting…</div>
<video id="v" autoplay muted playsinline></video>
<script>
const SW=__STREAM_W__,SH=__STREAM_H__;
const EVT_MOVE=0,EVT_DOWN=1,EVT_UP=2;
const hud=document.getElementById('hud'),video=document.getElementById('v');
let ws,pc,dc;

/* --- Multi-touch slot allocation --- */
const MAX_SLOTS=10;
const idToSlot=new Map();  /* Touch.identifier -> slot */
const slotUsed=new Array(MAX_SLOTS).fill(false);

function allocSlot(id){
  if(idToSlot.has(id)) return idToSlot.get(id);
  for(let i=0;i<MAX_SLOTS;i++){
    if(!slotUsed[i]){slotUsed[i]=true;idToSlot.set(id,i);return i}
  }
  return -1; /* no slots */
}
function freeSlot(id){
  const s=idToSlot.get(id);
  if(s!==undefined){slotUsed[s]=false;idToSlot.delete(id)}
}

function info(m){hud.textContent=m}

function mkEvt(type,x,y,slot){
  const b=new ArrayBuffer(13),d=new DataView(b);
  d.setUint8(0,type);
  d.setUint32(1,(performance.now()|0)&0xFFFFFFFF,true);
  d.setInt16(5,x,true);d.setInt16(7,y,true);
  d.setInt16(9,slot||0,true);d.setInt16(11,0,true);
  return b;
}

function mapXY(cx,cy){
  const r=video.getBoundingClientRect();
  const vr=SW/SH,rr=r.width/r.height;
  let rw,rh,ox,oy;
  if(rr>vr){rh=r.height;rw=rh*vr;ox=(r.width-rw)/2;oy=0}
  else{rw=r.width;rh=rw/vr;ox=0;oy=(r.height-rh)/2}
  let sx=Math.round((cx-r.left-ox)*SW/rw);
  let sy=Math.round((cy-r.top-oy)*SH/rh);
  return[Math.max(0,Math.min(SW-1,sx)),Math.max(0,Math.min(SH-1,sy))];
}

function sendEvt(e){if(dc&&dc.readyState==='open')dc.send(e)}

/* --- Multi-touch handlers --- */
video.addEventListener('touchstart',e=>{
  e.preventDefault();
  for(const t of e.changedTouches){
    const slot=allocSlot(t.identifier);
    if(slot<0)continue;
    const[x,y]=mapXY(t.clientX,t.clientY);
    sendEvt(mkEvt(EVT_DOWN,x,y,slot));
  }
},{passive:false});
video.addEventListener('touchmove',e=>{
  e.preventDefault();
  for(const t of e.changedTouches){
    const slot=idToSlot.get(t.identifier);
    if(slot===undefined)continue;
    const[x,y]=mapXY(t.clientX,t.clientY);
    sendEvt(mkEvt(EVT_MOVE,x,y,slot));
  }
},{passive:false});
video.addEventListener('touchend',e=>{
  e.preventDefault();
  for(const t of e.changedTouches){
    const slot=idToSlot.get(t.identifier);
    if(slot===undefined)continue;
    const[x,y]=mapXY(t.clientX,t.clientY);
    sendEvt(mkEvt(EVT_UP,x,y,slot));
    freeSlot(t.identifier);
  }
},{passive:false});
video.addEventListener('touchcancel',e=>{
  e.preventDefault();
  for(const t of e.changedTouches){
    const slot=idToSlot.get(t.identifier);
    if(slot===undefined)continue;
    const[x,y]=mapXY(t.clientX,t.clientY);
    sendEvt(mkEvt(EVT_UP,x,y,slot));
    freeSlot(t.identifier);
  }
},{passive:false});

/* --- Mouse (desktop testing, slot 0) --- */
let mouseDown=false;
video.addEventListener('mousedown',e=>{
  if(e.button)return;mouseDown=true;
  const[x,y]=mapXY(e.clientX,e.clientY);sendEvt(mkEvt(EVT_DOWN,x,y,0));
});
video.addEventListener('mousemove',e=>{
  if(!mouseDown)return;
  const[x,y]=mapXY(e.clientX,e.clientY);sendEvt(mkEvt(EVT_MOVE,x,y,0));
});
video.addEventListener('mouseup',e=>{
  if(e.button)return;mouseDown=false;
  const[x,y]=mapXY(e.clientX,e.clientY);sendEvt(mkEvt(EVT_UP,x,y,0));
});

/* --- WebRTC signaling --- */
function connect(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const url=proto+'//'+location.host+'/ws';
  info('Connecting to '+url+'…');
  ws=new WebSocket(url);

  ws.onopen=()=>{info('WebSocket open — negotiating WebRTC…')};

  ws.onmessage=ev=>{
    const msg=JSON.parse(ev.data);
    if(msg.type==='offer'){
      handleOffer(msg.sdp);
    }else if(msg.type==='ice'&&pc){
      pc.addIceCandidate(new RTCIceCandidate({
        candidate:msg.candidate,
        sdpMLineIndex:msg.sdpMLineIndex
      })).catch(e=>console.warn('addIceCandidate:',e));
    }
  };

  ws.onclose=()=>{
    info('Disconnected — retrying…');
    if(pc){pc.close();pc=null}
    dc=null;
    /* free all touch slots on disconnect */
    idToSlot.clear();
    slotUsed.fill(false);
    setTimeout(connect,2000);
  };
  ws.onerror=()=>ws.close();
}

async function handleOffer(sdp){
  if(pc){pc.close()}
  pc=new RTCPeerConnection({iceServers:[]});

  pc.ontrack=ev=>{
    info('Receiving video…');
    video.srcObject=ev.streams[0]||new MediaStream([ev.track]);
    video.play().catch(()=>{});
  };

  pc.onicecandidate=ev=>{
    if(ev.candidate&&ws.readyState===1){
      ws.send(JSON.stringify({
        type:'ice',
        candidate:ev.candidate.candidate,
        sdpMLineIndex:ev.candidate.sdpMLineIndex
      }));
    }
  };

  pc.ondatachannel=ev=>{
    dc=ev.channel;
    dc.binaryType='arraybuffer';
    dc.onopen=()=>info('▶ Playing (data channel open)');
    dc.onclose=()=>{dc=null};
  };

  pc.oniceconnectionstatechange=()=>{
    if(pc.iceConnectionState==='connected'||pc.iceConnectionState==='completed'){
      info('▶ Playing');
    }else if(pc.iceConnectionState==='failed'||pc.iceConnectionState==='disconnected'){
      info('Connection lost');
    }
  };

  await pc.setRemoteDescription(new RTCSessionDescription({type:'offer',sdp}));
  const answer=await pc.createAnswer();
  await pc.setLocalDescription(answer);
  ws.send(JSON.stringify({type:'answer',sdp:answer.sdp}));
}

connect();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Multi-touch uinput device
# ---------------------------------------------------------------------------
class InputInjector:
    """Virtual touchscreen with multi-touch type B protocol (10 slots)."""

    def __init__(self):
        capabilities = {
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(value=0, min=0, max=STREAM_WIDTH - 1,
                                       fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_Y, AbsInfo(value=0, min=0, max=STREAM_HEIGHT - 1,
                                       fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_MT_SLOT, AbsInfo(value=0, min=0, max=MAX_TOUCH_SLOTS - 1,
                                             fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_MT_TRACKING_ID, AbsInfo(value=0, min=-1,
                                                     max=MAX_TOUCH_SLOTS - 1,
                                                     fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_MT_POSITION_X, AbsInfo(value=0, min=0,
                                                    max=STREAM_WIDTH - 1,
                                                    fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_MT_POSITION_Y, AbsInfo(value=0, min=0,
                                                    max=STREAM_HEIGHT - 1,
                                                    fuzz=0, flat=0, resolution=0)),
            ],
            ecodes.EV_KEY: [ecodes.BTN_TOUCH] + KEYBOARD_KEYS,
        }
        self.dev = UInput(events=capabilities, name="webrtc-stream-touch",
                          vendor=0x1234, product=0x5678,
                          input_props=[ecodes.INPUT_PROP_DIRECT])
        self.active_slots = set()
        log(f"Created virtual touchscreen: {self.dev.device.path}")

    def handle_event(self, evt_type, arg1, arg2, arg3, arg4):
        slot = max(0, min(MAX_TOUCH_SLOTS - 1, arg3))

        if evt_type == EVT_MOUSE_MOVE:
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, arg1)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, arg2)
            if slot == 0:
                self.dev.write(ecodes.EV_ABS, ecodes.ABS_X, arg1)
                self.dev.write(ecodes.EV_ABS, ecodes.ABS_Y, arg2)
            self.dev.syn()

        elif evt_type == EVT_MOUSE_DOWN:
            self.active_slots.add(slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, arg1)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, arg2)
            if len(self.active_slots) == 1:
                self.dev.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)
            if slot == 0:
                self.dev.write(ecodes.EV_ABS, ecodes.ABS_X, arg1)
                self.dev.write(ecodes.EV_ABS, ecodes.ABS_Y, arg2)
            self.dev.syn()

        elif evt_type == EVT_MOUSE_UP:
            self.active_slots.discard(slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
            self.dev.write(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1)
            if not self.active_slots:
                self.dev.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
            self.dev.syn()

        elif evt_type == EVT_KEY_DOWN:
            self.dev.write(ecodes.EV_KEY, arg1, 1)
            self.dev.syn()

        elif evt_type == EVT_KEY_UP:
            self.dev.write(ecodes.EV_KEY, arg1, 0)
            self.dev.syn()

    def close(self):
        self.dev.close()


# ---------------------------------------------------------------------------
# Headless Weston + PipeWire node discovery (ported from sender.sh)
# ---------------------------------------------------------------------------
WESTON_SOCKET = "waydroid-stream"


def start_weston_headless():
    """Start weston with PipeWire backend, return the process."""
    xdg = os.environ.setdefault("XDG_RUNTIME_DIR",
                                f"/run/user/{os.getuid()}")

    socket_path = os.path.join(xdg, WESTON_SOCKET)
    lock_path = socket_path + ".lock"
    if os.path.exists(lock_path):
        log("Cleaning stale weston socket...")
        subprocess.run(["pkill", "-f", f"weston.*{WESTON_SOCKET}"],
                        capture_output=True)
        import time; time.sleep(1)
        for p in (socket_path, lock_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    log(f"Starting weston PipeWire compositor ({STREAM_WIDTH}x{STREAM_HEIGHT})...")
    import tempfile
    cfg = tempfile.NamedTemporaryFile(mode="w", prefix="weston-headless-",
                                      suffix=".ini", delete=False)
    cfg.write(f"[output]\nname=pipewire\nmode={STREAM_WIDTH}x{STREAM_HEIGHT}\n")
    cfg.close()

    proc = subprocess.Popen(
        ["weston", "--backend=pipewire", "--renderer=gl",
         f"--config={cfg.name}", f"--socket={WESTON_SOCKET}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log(f"Weston PID: {proc.pid}")

    import time
    for _ in range(20):
        if os.path.exists(socket_path):
            log(f"Weston socket ready: {socket_path}")
            os.environ["WAYLAND_DISPLAY"] = WESTON_SOCKET
            return proc
        time.sleep(0.5)

    log("ERROR: Weston socket did not appear")
    proc.terminate()
    sys.exit(1)


def find_pipewire_video_node():
    """Find the weston PipeWire video node ID."""
    import time
    log("Looking for weston PipeWire video node...")
    for _ in range(20):
        try:
            dump = subprocess.check_output(["pw-dump"], text=True)
            import json as _json
            for obj in _json.loads(dump):
                props = obj.get("info", {}).get("props", {})
                mc = props.get("media.class", "")
                if "Video" in mc and "weston" in props.get("node.name", ""):
                    node_id = str(obj["id"])
                    log(f"Found PipeWire video node: {node_id}")
                    return node_id
        except Exception:
            pass
        time.sleep(0.5)
    log("ERROR: No PipeWire video node found from weston.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# GStreamer WebRTC streaming
# ---------------------------------------------------------------------------
class WebRTCStream:
    """Manages the GStreamer pipeline and WebRTC signaling for one viewer."""

    def __init__(self, capture_method, pipewire_node=None):
        self.capture_method = capture_method
        self.pipewire_node = pipewire_node
        self.pipeline = None
        self.webrtcbin = None
        self.data_channel = None
        self.ws = None
        self.event_loop = None
        self.input_injector = None

    def _build_pipeline_str(self):
        if self.capture_method == "test":
            src = (
                'videotestsrc pattern=ball is-live=true ! '
                f'video/x-raw,width={STREAM_WIDTH},height={STREAM_HEIGHT},'
                f'framerate={FRAMERATE}/1'
            )
        else:
            path_prop = f'path={self.pipewire_node} ' if self.pipewire_node else ''
            src = (
                f'pipewiresrc {path_prop}do-timestamp=true keepalive-time=1000 ! '
                f'queue max-size-buffers=3 leaky=downstream'
            )

        return (
            f'{src} ! '
            'videoconvert ! video/x-raw,format=NV12 ! '
            'v4l2h264enc '
            f'extra-controls="controls,video_bitrate={BITRATE},'
            'video_gop_size=30,video_b_frames=0;" ! '
            'video/x-h264,profile=constrained-baseline,stream-format=byte-stream ! '
            'h264parse config-interval=-1 ! '
            'rtph264pay config-interval=-1 aggregate-mode=zero-latency ! '
            'application/x-rtp,media=video,encoding-name=H264,payload=96 ! '
            'webrtcbin name=webrtc bundle-policy=max-bundle '
            'stun-server=NULL'
        )

    def start(self, ws, loop, input_injector):
        """Create and start the pipeline, wire up signaling."""
        self.ws = ws
        self.event_loop = loop
        self.input_injector = input_injector

        pipeline_str = self._build_pipeline_str()
        log(f"Pipeline: {pipeline_str}")

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.webrtcbin = self.pipeline.get_by_name("webrtc")

        self.webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtcbin.connect("on-ice-candidate", self._on_ice_candidate)

        # Watch for pipeline errors
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::eos", self._on_bus_eos)

        # Pipeline must be at least READY before creating data channels
        self.pipeline.set_state(Gst.State.READY)

        # Create data channel for input (server-initiated)
        opts = Gst.Structure.new_empty("application/x-data-channel-options")
        self.data_channel = self.webrtcbin.emit("create-data-channel", "input", opts)
        if self.data_channel:
            self.data_channel.connect("on-message-data", self._on_data_channel_message)
            self.data_channel.connect("on-open", self._on_data_channel_open)
            self.data_channel.connect("on-close", self._on_data_channel_close)
            log("Data channel 'input' created")
        else:
            log("WARNING: Failed to create data channel")

        self.pipeline.set_state(Gst.State.PLAYING)
        log("Pipeline started")

    def stop(self):
        """Tear down the pipeline."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.webrtcbin = None
            self.data_channel = None
            log("Pipeline stopped")

    def handle_sdp_answer(self, sdp_text):
        """Process the browser's SDP answer."""
        _, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp
        )
        self.webrtcbin.emit("set-remote-description", answer, None)
        log("Set remote description (answer)")

    def handle_ice_candidate(self, sdp_mline_index, candidate):
        """Add an ICE candidate from the browser."""
        self.webrtcbin.emit("add-ice-candidate", sdp_mline_index, candidate)

    # -- GStreamer callbacks (run on GLib main context) --

    def _on_negotiation_needed(self, webrtcbin):
        log("Negotiation needed — creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created)
        webrtcbin.emit("create-offer", None, promise)

    def _on_offer_created(self, promise):
        promise.wait()
        reply = promise.get_reply()
        if reply is None:
            log("ERROR: create-offer returned no reply")
            return
        offer = reply.get_value("offer")
        if offer is None:
            log("ERROR: create-offer reply has no 'offer'")
            return
        self.webrtcbin.emit("set-local-description", offer, None)
        sdp_text = offer.sdp.as_text()
        log(f"Sending SDP offer ({len(sdp_text)} bytes)")
        self._send_json({"type": "offer", "sdp": sdp_text})

    def _on_ice_candidate(self, webrtcbin, sdp_mline_index, candidate):
        self._send_json({
            "type": "ice",
            "candidate": candidate,
            "sdpMLineIndex": sdp_mline_index,
        })

    def _on_data_channel_open(self, channel):
        log("Data channel open")

    def _on_data_channel_close(self, channel):
        log("Data channel closed")

    def _on_data_channel_message(self, channel, data):
        if data is None:
            return
        # data is a GLib.Bytes
        raw = data.get_data()
        if raw is None or len(raw) < EVENT_SIZE:
            return
        offset = 0
        while offset + EVENT_SIZE <= len(raw):
            evt_type, _ts, arg1, arg2, arg3, arg4 = struct.unpack_from(
                EVENT_FMT, raw, offset
            )
            offset += EVENT_SIZE
            if self.input_injector:
                try:
                    self.input_injector.handle_event(evt_type, arg1, arg2, arg3, arg4)
                except Exception as e:
                    log(f"Input injection error: {e}")

    def _on_bus_error(self, bus, msg):
        err, debug = msg.parse_error()
        log(f"Pipeline error: {err.message}")
        if debug:
            log(f"  debug: {debug}")

    def _on_bus_eos(self, bus, msg):
        log("Pipeline EOS")

    def _send_json(self, obj):
        """Thread-safe send via the asyncio event loop."""
        if self.ws and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._async_send(json.dumps(obj)), self.event_loop
            )

    async def _async_send(self, text):
        try:
            await self.ws.send(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server: HTTP + WebSocket on a single port
# ---------------------------------------------------------------------------
class StreamServer:
    def __init__(self, args):
        self.args = args
        self.capture_method = args.capture_method
        self.pipewire_node = None
        self.weston_proc = None
        self.stream = None
        self.viewer_ws = None
        self.input_injector = None

        self.html = (
            HTML_PAGE
            .replace("__STREAM_W__", str(STREAM_WIDTH))
            .replace("__STREAM_H__", str(STREAM_HEIGHT))
        )

    async def run(self):
        log("=== WebRTC Game Stream Server ===")
        log(f"Capture: {self.capture_method}")
        log(f"Encode:  H.264 constrained-baseline, {BITRATE // 1000} kbps, "
            f"{FRAMERATE} fps, GOP=30, B=0")
        log(f"Port:    {self.args.port}")

        # Initialize GStreamer
        Gst.init(None)

        # Set up capture source
        if self.capture_method == "headless":
            self.weston_proc = start_weston_headless()
            # Restart waydroid session so it connects to new compositor
            subprocess.run(["waydroid", "session", "stop"],
                           capture_output=True, timeout=10)
            await asyncio.sleep(2)
            self.pipewire_node = find_pipewire_video_node()

        elif self.capture_method != "test":
            # For PipeWire direct capture, try to find the node
            try:
                self.pipewire_node = find_pipewire_video_node()
            except SystemExit:
                log("No PipeWire node found, will use pipewiresrc defaults")
                self.pipewire_node = None

        # Create uinput device
        try:
            self.input_injector = InputInjector()
        except Exception as e:
            log(f"WARNING: Could not create uinput device: {e}")
            log("  Input injection will be disabled")

        # Start GLib main context pump
        glib_task = asyncio.ensure_future(self._glib_pump())

        # Start WebSocket server with HTTP handler
        async with websockets.serve(
            self._ws_handler,
            "0.0.0.0",
            self.args.port,
            process_request=self._http_handler,
            ping_interval=20,
            ping_timeout=60,
        ):
            log(f"HTTP  -> http://0.0.0.0:{self.args.port}")
            log(f"WS    -> ws://0.0.0.0:{self.args.port}/ws")
            log("Waiting for browser connection...")
            await asyncio.Future()  # run forever

    async def _glib_pump(self):
        """Iterate the GLib main context from asyncio at ~200Hz."""
        ctx = GLib.MainContext.default()
        while True:
            while ctx.pending():
                ctx.iteration(False)
            await asyncio.sleep(0.005)

    def _http_handler(self, connection, request):
        """Serve HTML for non-WebSocket requests (websockets 14 API)."""
        if request.path == "/ws":
            return None  # let websockets handle the upgrade
        return Response(
            200, "OK",
            Headers([("Content-Type", "text/html; charset=utf-8"),
                     ("Cache-Control", "no-cache")]),
            self.html.encode(),
        )

    async def _ws_handler(self, websocket):
        """Handle one WebSocket connection (single viewer)."""
        if self.viewer_ws is not None:
            log("Rejecting additional viewer (single viewer only)")
            await websocket.close(1013, "Only one viewer allowed")
            return

        addr = getattr(websocket, "remote_address",
                       getattr(websocket, "origin", "?"))
        log(f"Browser connected: {addr}")
        self.viewer_ws = websocket

        # Create and start the pipeline
        self.stream = WebRTCStream(self.capture_method, self.pipewire_node)
        loop = asyncio.get_event_loop()
        try:
            self.stream.start(websocket, loop, self.input_injector)
        except Exception as e:
            log(f"Pipeline failed to start: {e}")
            if self.stream:
                self.stream.stop()
                self.stream = None
            self.viewer_ws = None
            await websocket.close(1011, "Pipeline failed")
            return

        try:
            async for msg in websocket:
                if isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue

                    if data.get("type") == "answer" and self.stream:
                        self.stream.handle_sdp_answer(data["sdp"])
                    elif data.get("type") == "ice" and self.stream:
                        self.stream.handle_ice_candidate(
                            data.get("sdpMLineIndex", 0),
                            data.get("candidate", ""),
                        )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            log(f"Browser disconnected: {addr}")
            if self.stream:
                self.stream.stop()
                self.stream = None
            self.viewer_ws = None

    def cleanup(self):
        if self.stream:
            self.stream.stop()
        if self.input_injector:
            self.input_injector.close()
        if self.weston_proc and self.weston_proc.poll() is None:
            self.weston_proc.terminate()
            self.weston_proc.wait()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="WebRTC game stream server")
    ap.add_argument("--port", type=int, default=8080,
                    help="HTTP/WebSocket port (default: 8080)")
    ap.add_argument("--capture-method", default=None,
                    help="Capture method: test, headless, pipewire "
                         "(default: env CAPTURE_METHOD or test)")
    args = ap.parse_args()

    if args.capture_method is None:
        args.capture_method = os.environ.get("CAPTURE_METHOD", "test")

    server = StreamServer(args)

    def on_signal(sig, _frame):
        log("Shutting down.")
        server.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        server.cleanup()
        log("Shutting down.")


if __name__ == "__main__":
    main()
