#!/usr/bin/env python3
"""web_receiver.py — Web-based game stream receiver.

Accepts the sender's MPEGTS/H.264 stream over TCP, remuxes to fragmented MP4,
and serves it to browsers via WebSocket + Media Source Extensions. Touch and
mouse input from the browser is forwarded to the input server via UDP.

Requirements:
    pip install websockets
    ffmpeg must be installed

Usage:
    python3 web_receiver.py --sender-host 192.168.86.33
    # Open http://<this-machine-ip>:8080 on your phone
"""

import argparse
import asyncio
import http.server
import socket
import struct
import sys
import threading

try:
    import websockets
except ImportError:
    print("Required: pip install websockets", file=sys.stderr)
    sys.exit(1)

STREAM_WIDTH = 1280
STREAM_HEIGHT = 720

EVENT_FMT = "<BIhhhh"
EVENT_SIZE = struct.calcsize(EVENT_FMT)


def log(msg):
    print(f"[web] {msg}", flush=True)


def find_moof_offset(data):
    """Return byte offset of the first 'moof' box in fMP4 data, or -1."""
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8]
        if size < 8:
            return -1
        if box_type == b"moof":
            return offset
        offset += size
    return -1


# ---------------------------------------------------------------------------
# Inline HTML — served to the browser
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Game Stream</title>
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
let ws,sourceBuffer,queue=[],touching=false;

function info(m){hud.textContent=m}

function mkEvt(type,x,y){
  const b=new ArrayBuffer(13),d=new DataView(b);
  d.setUint8(0,type);
  d.setUint32(1,(performance.now()|0)&0xFFFFFFFF,true);
  d.setInt16(5,x,true);d.setInt16(7,y,true);
  d.setInt16(9,0,true);d.setInt16(11,0,true);
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

function send(e){if(ws&&ws.readyState===1)ws.send(e)}

/* --- Touch --- */
video.addEventListener('touchstart',e=>{
  e.preventDefault();touching=true;
  const t=e.touches[0],[x,y]=mapXY(t.clientX,t.clientY);
  send(mkEvt(EVT_DOWN,x,y));
},{passive:false});
video.addEventListener('touchmove',e=>{
  e.preventDefault();if(!touching)return;
  const t=e.touches[0],[x,y]=mapXY(t.clientX,t.clientY);
  send(mkEvt(EVT_MOVE,x,y));
},{passive:false});
video.addEventListener('touchend',e=>{
  e.preventDefault();touching=false;
  const t=e.changedTouches[0],[x,y]=mapXY(t.clientX,t.clientY);
  send(mkEvt(EVT_UP,x,y));
},{passive:false});
video.addEventListener('touchcancel',e=>{
  e.preventDefault();touching=false;
  const t=e.changedTouches[0],[x,y]=mapXY(t.clientX,t.clientY);
  send(mkEvt(EVT_UP,x,y));
},{passive:false});

/* --- Mouse (desktop testing) --- */
video.addEventListener('mousedown',e=>{
  if(e.button)return;touching=true;
  const[x,y]=mapXY(e.clientX,e.clientY);send(mkEvt(EVT_DOWN,x,y));
});
video.addEventListener('mousemove',e=>{
  if(!touching)return;
  const[x,y]=mapXY(e.clientX,e.clientY);send(mkEvt(EVT_MOVE,x,y));
});
video.addEventListener('mouseup',e=>{
  if(e.button)return;touching=false;
  const[x,y]=mapXY(e.clientX,e.clientY);send(mkEvt(EVT_UP,x,y));
});

/* --- MSE video --- */
function tryAppend(){
  while(sourceBuffer&&!sourceBuffer.updating&&queue.length){
    try{sourceBuffer.appendBuffer(queue.shift());return}
    catch(e){console.warn('append:',e);queue.length=0}
  }
}

function connect(){
  const url='ws://'+location.hostname+':__WS_PORT__';
  info('Connecting to '+url+'…');
  ws=new WebSocket(url);
  ws.binaryType='arraybuffer';

  ws.onopen=()=>{
    info('Connected — waiting for video…');
    queue=[];sourceBuffer=null;
    const ms=new MediaSource();
    video.src=URL.createObjectURL(ms);
    ms.addEventListener('sourceopen',()=>{
      const types=[
        'video/mp4; codecs="avc1.42E01F"',
        'video/mp4; codecs="avc1.42001F"',
        'video/mp4; codecs="avc1.640029"',
        'video/mp4; codecs="avc1.4D401F"',
      ];
      const mime=types.find(t=>MediaSource.isTypeSupported(t))||types[0];
      sourceBuffer=ms.addSourceBuffer(mime);
      sourceBuffer.mode='segments';
      sourceBuffer.addEventListener('updateend',()=>{
        tryAppend();
        /* stay near live edge */
        if(video.buffered.length){
          const end=video.buffered.end(video.buffered.length-1);
          if(end-video.currentTime>1.5)video.currentTime=end-0.1;
        }
      });
      info('Buffering…');
    });
  };

  ws.onmessage=e=>{
    if(e.data instanceof ArrayBuffer){
      if(sourceBuffer&&!sourceBuffer.updating){
        try{sourceBuffer.appendBuffer(e.data)}
        catch(_){queue.push(e.data)}
      }else{queue.push(e.data)}
      if(video.paused)video.play().catch(()=>{});
      info('▶ Playing');
    }
  };

  ws.onclose=()=>{info('Disconnected — retrying…');setTimeout(connect,2000)};
  ws.onerror=()=>ws.close();
}

if(!window.MediaSource){info('ERROR: MediaSource not supported')}
else{connect()}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class StreamServer:
    def __init__(self, args):
        self.args = args
        self.clients = set()
        self.init_segment = None
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender_addr = (
            (args.sender_host, args.input_port) if args.sender_host else None
        )

    # ---- WebSocket ----
    async def ws_handler(self, websocket, path=None):
        addr = getattr(websocket, "remote_address", "?")
        log(f"Browser connected: {addr}")
        if self.init_segment:
            try:
                await websocket.send(self.init_segment)
            except Exception:
                return
        self.clients.add(websocket)
        try:
            async for msg in websocket:
                if (isinstance(msg, bytes) and self.sender_addr
                        and len(msg) >= EVENT_SIZE):
                    self.udp_sock.sendto(msg, self.sender_addr)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log(f"Browser disconnected: {addr}")

    async def broadcast(self, data):
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(data)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    # ---- TCP from sender → ffmpeg remux → WebSocket push ----
    async def tcp_handler(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log(f"Sender connected: {peer}")
        self.init_segment = None

        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "100000",
            "-probesize", "32768",
            "-i", "pipe:0",
            "-c:v", "copy", "-an",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def feed():
            total = 0
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    total += len(data)
                    ffmpeg.stdin.write(data)
                    await ffmpeg.stdin.drain()
            except Exception as e:
                log(f"Feed error: {e}")
            finally:
                try:
                    ffmpeg.stdin.close()
                except Exception:
                    pass
                log(f"Sender done ({total / 1048576:.1f} MB)")

        async def push():
            buf = bytearray()
            init_done = False
            try:
                while True:
                    chunk = await ffmpeg.stdout.read(32768)
                    if not chunk:
                        break
                    if not init_done:
                        buf.extend(chunk)
                        pos = find_moof_offset(bytes(buf))
                        if pos < 0:
                            continue  # still accumulating init segment
                        self.init_segment = bytes(buf[:pos])
                        remainder = bytes(buf[pos:])
                        init_done = True
                        log(f"Init segment: {len(self.init_segment)} bytes")
                        await self.broadcast(self.init_segment + remainder)
                        continue
                    await self.broadcast(chunk)
            except Exception as e:
                log(f"Push error: {e}")

        await asyncio.gather(feed(), push())
        try:
            ffmpeg.terminate()
            await ffmpeg.wait()
        except Exception:
            pass
        writer.close()
        log("Ready for next sender connection.")

    # ---- Run everything ----
    async def run(self):
        html = (
            HTML_PAGE
            .replace("__WS_PORT__", str(self.args.ws_port))
            .replace("__STREAM_W__", str(STREAM_WIDTH))
            .replace("__STREAM_H__", str(STREAM_HEIGHT))
        )

        handler_cls = _make_http_handler(html)
        httpd = http.server.HTTPServer(("0.0.0.0", self.args.http_port), handler_cls)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log(f"HTTP  → http://0.0.0.0:{self.args.http_port}")

        ws_kw = dict(ping_interval=20, ping_timeout=60)
        ws_srv = await websockets.serve(
            self.ws_handler, "0.0.0.0", self.args.ws_port, **ws_kw,
        )
        log(f"WS    → ws://0.0.0.0:{self.args.ws_port}")

        tcp_srv = await asyncio.start_server(
            self.tcp_handler, "0.0.0.0", self.args.port,
        )
        log(f"TCP   → 0.0.0.0:{self.args.port}  (waiting for sender)")

        if self.sender_addr:
            log(f"Input → UDP {self.sender_addr[0]}:{self.sender_addr[1]}")
        else:
            log("Input forwarding: off (use --sender-host to enable)")

        await tcp_srv.serve_forever()


def _make_http_handler(html_text):
    body = html_text.encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description="Web-based game stream receiver")
    ap.add_argument("--port", type=int, default=9000,
                    help="TCP port for sender stream (default: 9000)")
    ap.add_argument("--http-port", type=int, default=8080,
                    help="HTTP port for web UI (default: 8080)")
    ap.add_argument("--ws-port", type=int, default=8081,
                    help="WebSocket port for video+input (default: 8081)")
    ap.add_argument("--sender-host", default=None,
                    help="Sender IP for input forwarding (UDP)")
    ap.add_argument("--input-port", type=int, default=9001,
                    help="UDP port for input forwarding (default: 9001)")
    args = ap.parse_args()

    log("=== Web Game Stream Receiver ===")
    server = StreamServer(args)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log("Shutting down.")


if __name__ == "__main__":
    main()
