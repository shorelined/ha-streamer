import sys
import json
import socket
import argparse
import subprocess
import threading
import logging
import click
import queue
import webbrowser
import simple_websocket
import pyaudiowpatch as pyaudio
import tkinter as tk
from tkinter import font as tkfont
from array import array
from flask import Flask, render_template_string
from flask_sock import Sock

logging.getLogger("werkzeug").setLevel(logging.ERROR)
click.echo = lambda *args, **kwargs: None  # suppress Flask startup messages

TARGET_CHUNK_SECONDS = 0.02
STREAM_SAMPLE_RATE = 48000
STREAM_CHANNELS = 2

SILENCE_NONZERO_LSB = 1  # 0 disables; 1 is 1/32768 full-scale (very low)
SILENCE_NONZERO_PERIOD_FRAMES = 4  # inject every N frames when the chunk is all-zero

app = Flask(__name__)
sock = Sock(app)

clients: list[queue.Queue[bytes]] = []
clients_lock = threading.Lock()
client_limit: int | None = 1
device_info = {}
device_ready = threading.Event()

# Catpuccin Mocha colorscheme
class Color:
    rosewater = '#f5e0dc'
    flamingo  = '#f2cdcd'
    pink      = '#f5c2e7'
    mauve     = '#cba6f7'
    red       = '#f38ba8'
    maroon    = '#eba0ac'
    peach     = '#fab387'
    yellow    = '#f9e2af'
    green     = '#a6e3a1'
    teal      = '#94e2d5'
    sky       = '#89dceb'
    sapphire  = '#74c7ec'
    blue      = '#89b4fa'
    lavender  = '#b4befe'
    text      = '#cdd6f4'
    subtext1  = '#bac2de'
    subtext0  = '#a6adc8'
    overlay2  = '#9399b2'
    overlay1  = '#7f849c'
    overlay0  = '#6c7086'
    surface2  = '#585b70'
    surface1  = '#45475a'
    surface0  = '#313244'
    base      = '#1e1e2e'
    mantle    = '#181825'
    crust     = '#11111b'

log_queue: queue.Queue = queue.Queue()

def build_sparse_dither_silence(frames: int, channels: int, period_frames: int, lsb: int) -> bytes:
    if frames <= 0 or channels <= 0 or period_frames <= 0 or lsb <= 0:
        return b""
    payload = bytearray(frames * channels * 2)  # int16le
    sign = 1
    for frame in range(0, frames, period_frames):
        val = lsb if sign > 0 else -lsb
        sign *= -1
        for ch in range(channels):
            idx = (frame * channels + ch) * 2
            payload[idx : idx + 2] = int(val).to_bytes(2, byteorder="little", signed=True)
    return bytes(payload)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Audio Stream</title>
    <style>
        /* Catppuccin Mocha */
        :root {
            --rosewater: #f5e0dc;
            --flamingo: #f2cdcd;
            --pink: #f5c2e7;
            --mauve: #cba6f7;
            --red: #f38ba8;
            --maroon: #eba0ac;
            --peach: #fab387;
            --yellow: #f9e2af;
            --green: #a6e3a1;
            --teal: #94e2d5;
            --sky: #89dceb;
            --sapphire: #74c7ec;
            --blue: #89b4fa;
            --lavender: #b4befe;

            --text: #cdd6f4;
            --subtext1: #bac2de;
            --subtext0: #a6adc8;
            --overlay2: #9399b2;
            --overlay1: #7f849c;
            --overlay0: #6c7086;
            --surface2: #585b70;
            --surface1: #45475a;
            --surface0: #313244;
            --base: #1e1e2e;
            --mantle: #181825;
            --crust: #11111b;

            --accent: var(--mauve);
            --ok: var(--green);
            --warn: var(--yellow);
            --muted: var(--overlay0);
            --card: var(--mantle);
            --border: var(--surface0);
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { height: 100%; overflow: hidden; }
        body {
            display: grid;
            place-items: center;
            min-height: 100vh;
            background: var(--base);
            color: var(--text);
            font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji", "Segoe UI Emoji";
        }

        .panel {
            width: min(440px, calc(100vw - 24px));
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 20px 18px;
            box-shadow: 0 18px 70px rgba(0,0,0,.42);
            backdrop-filter: blur(8px);
        }

        header {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
        }

        h1 {
            font-size: 14px;
            font-weight: 650;
            letter-spacing: 0.3px;
            color: var(--subtext1);
        }

        .pill {
            font-size: 12px;
            padding: 6px 10px;
            border-radius: 999px;
            border: 1px solid var(--border);
            color: var(--subtext0);
            background: var(--surface0);
        }
        .pill.live {
            color: var(--ok);
            border-color: rgba(166,227,161,0.35);
            background: rgba(166,227,161,0.10);
        }

        .meta {
            font-size: 12px;
            line-height: 1.25;
            color: var(--muted);
            margin-bottom: 10px;
        }
        .latency {
            font-size: 12px;
            line-height: 1.25;
            color: var(--muted);
            font-variant-numeric: tabular-nums;
            margin-bottom: 14px;
        }
        .latency span { color: var(--ok); font-weight: 650; }

        .controls {
            display: grid;
            grid-template-columns: 1fr;
            gap: 12px;
            margin-top: 6px;
        }

        button {
            appearance: none;
            width: 100%;
            border: 1px solid rgba(203,166,247,0.45);
            background: var(--accent);
            color: var(--crust);
            padding: 12px 14px;
            border-radius: 14px;
            font-size: 14px;
            font-weight: 700;
            letter-spacing: 0.2px;
            cursor: pointer;
            transition: transform .08s ease, filter .15s ease, background .15s ease;
        }
        button:hover { filter: brightness(1.05); }
        button:active { transform: translateY(1px); }
        button:disabled {
            cursor: default;
            color: var(--overlay2);
            background: var(--surface0);
            border-color: var(--border);
            filter: none;
            transform: none;
        }

        .sliderBlock {
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 12px 12px 10px;
            background: var(--base);
        }
        .sliderTop {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 10px;
        }
        .sliderTop label {
            font-size: 12px;
            color: var(--subtext1);
            font-weight: 650;
        }
        .value {
            font-size: 12px;
            color: var(--overlay2);
            font-variant-numeric: tabular-nums;
        }

        input[type="range"] {
            --fill: 100%;
            --track: var(--surface1);
            -webkit-appearance: none;
            appearance: none;
            width: 100%;
            height: 8px;
            border-radius: 999px;
            background: linear-gradient(90deg, var(--accent) 0%, var(--accent) var(--fill), var(--track) var(--fill), var(--track) 100%);
            outline: none;
        }
        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 18px;
            height: 18px;
            border-radius: 999px;
            background: var(--text);
            border: 2px solid color-mix(in srgb, var(--accent) 75%, var(--text));
            box-shadow: 0 8px 20px rgba(0,0,0,.35);
        }
        input[type="range"]::-moz-range-thumb {
            width: 18px;
            height: 18px;
            border-radius: 999px;
            background: var(--text);
            border: 2px solid color-mix(in srgb, var(--accent) 75%, var(--text));
            box-shadow: 0 8px 20px rgba(0,0,0,.35);
        }
        input[type="range"]::-moz-range-track {
            height: 8px;
            border-radius: 999px;
            background: transparent;
        }
    </style>
</head>
<body>
    <main class="panel">
        <header>
            <h1>HA/CI Audio Streamer</h1>
            <span class="pill" id="status">Tap Play</span>
        </header>

        <div class="meta" id="device"></div>
        <div class="latency" id="latency"></div>

        <div class="controls">
            <button id="btn" onclick="toggle()">Play</button>

            <div class="sliderBlock">
                <div class="sliderTop">
                    <label for="vol">Volume</label>
                    <span class="value" id="volVal">100%</span>
                </div>
                <input type="range" id="vol" min="0" max="1" step="0.01" value="1">
            </div>

            <div class="sliderBlock">
                <div class="sliderTop">
                    <label for="buf">Buffer Target</label>
                    <span class="value" id="bufVal">30 ms</span>
                </div>
                <input type="range" id="buf" min="10" max="500" step="1" value="30">
            </div>
        </div>
    </main>
    <!-- silent audio element for background playback -->
    <audio id="bgaudio" loop playsinline></audio>
<script>
let ctx, gainNode, ws, nextTime = 0, playing = false;
let sampleRate = 48000, channels = 2;
let hasScheduledAudio = false;
let watchdog = null;

// buffer target (seconds)
let targetBuffer = 0.02;
let lastUnderrunAdjust = 0;
const MAX_TARGET_BUFFER = 0.25; // 250ms
const AUTO_BUFFER_STEP = 0.01;  // 10ms
const AUTO_ADJUST_COOLDOWN_MS = 3000;

let rtt = 0;
let pingInterval = null;
const bgAudio = document.getElementById('bgaudio');
let mediaStreamDest = null;

	// adaptive playback rate control
let playbackRate = 1.0;
const KP = 0.03;                 // proportional gain
const MAX_RATE_DELTA = 0.015;    // +/-1.5% (safe for BT)
const SMOOTHING = 0.7;           // light smoothing
const MAX_RATE_STEP = 0.005;     // max change per update (slew rate)

function setTargetBufferSeconds(sec) {
    targetBuffer = Math.max(0.01, Math.min(MAX_TARGET_BUFFER, sec));
    const ms = Math.round(targetBuffer * 1000);
    const el = document.getElementById('buf');
    const valEl = document.getElementById('bufVal');
    if (el) {
        el.value = String(ms);
        setSliderFill(el);
    }
    if (valEl) valEl.textContent = ms + ' ms';
}

function maybeAutoIncreaseBuffer() {
    const now = performance.now();
    if (now - lastUnderrunAdjust < AUTO_ADJUST_COOLDOWN_MS) return;
    lastUnderrunAdjust = now;
    setTargetBufferSeconds(targetBuffer + AUTO_BUFFER_STEP);
}

// setup media session for lock screen controls
function setupMediaSession() {
    if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: 'PC Audio Stream',
            artist: 'Streaming...',
            album: 'Live'
        });
        navigator.mediaSession.setActionHandler('play', () => { if (!playing) toggle(); });
        navigator.mediaSession.setActionHandler('pause', () => { if (playing) toggle(); });
    }
}

// resume audio when page becomes visible
document.addEventListener('visibilitychange', () => {
    if (playing && ctx && ctx.state === 'suspended') {
        ctx.resume();
    }
});

async function ensureAudioRunning() {
    if (!playing) return;
    if (ctx && ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (e) {}
    }
    if (bgAudio && bgAudio.paused) {
        try { await bgAudio.play(); } catch (e) {}
    }
}

// iOS often needs a fresh user gesture to resume audio after interruptions
document.addEventListener('pointerdown', () => { ensureAudioRunning(); }, { passive: true });

function toggle() {
    if (playing) {
        const status = document.getElementById('status');
        status.textContent = 'Tap Play';
        status.className = 'pill';
        stop();
        return;
    }
    start();
}

function start() {
    // iOS/Safari: create/resume audio in direct response to the user gesture.
    if (!ctx) {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (!gainNode) {
        gainNode = ctx.createGain();
        gainNode.gain.value = document.getElementById('vol').value;
    }
    if (!mediaStreamDest) {
        mediaStreamDest = ctx.createMediaStreamDestination();
        gainNode.connect(mediaStreamDest);
        bgAudio.srcObject = mediaStreamDest.stream;
    }
    // Best-effort; may still require an additional tap after interruptions.
    try { ctx.resume(); } catch (e) {}
    bgAudio.play().catch(() => {});

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/ws');
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
        const status = document.getElementById('status');
        status.textContent = 'Connected';
        status.className = 'pill';
        pingInterval = setInterval(() => {
            if (ws && ws.readyState === 1) {
                ws.send('p' + performance.now());
            }
        }, 500);
    };

    ws.onmessage = (e) => {
        // string messages: config or pong
        if (typeof e.data === 'string') {
            // pong reply
            if (e.data.charAt(0) === 'p') {
                const sent = parseFloat(e.data.slice(1));
                rtt = performance.now() - sent;
                updateLatencyUI();
                return;
            }
            // config
            const cfg = JSON.parse(e.data);
            sampleRate = cfg.sample_rate;
            channels = cfg.channels;
            const outName = cfg.default_output_device || '';
            const loopName = cfg.loopback_device || '';
            const fmt = (cfg.channels && cfg.sample_rate) ? (' \u2022 ' + cfg.channels + 'ch @ ' + cfg.sample_rate + 'Hz') : '';
            const devLine = outName ? ('Output: ' + outName + (loopName ? (' (Loopback: ' + loopName + ')') : '') + fmt) : '';
            document.getElementById('device').textContent = devLine;
            // Audio graph is already created in the user-gesture handler above.
            ensureAudioRunning();

            nextTime = ctx.currentTime + targetBuffer;
            hasScheduledAudio = false;
            // tell server we're ready to receive audio
            ws.send('ready');
            const status = document.getElementById('status');
            status.textContent = 'Live';
            status.className = 'pill live';
            document.getElementById('btn').textContent = 'Stop';
            playing = true;
            setupMediaSession();
            if (!watchdog) {
                watchdog = setInterval(() => { ensureAudioRunning(); }, 1000);
            }
            return;
        }

        if (!ctx) return;

        const now = ctx.currentTime;
        let bufferAhead = nextTime - now;
 
        // hard reset if buffer drifts too far (fallback safety)
        if (bufferAhead < 0) {
            maybeAutoIncreaseBuffer();
            nextTime = now;
            bufferAhead = 0;
            playbackRate = 1.0;
        }
        
        // update adaptive playback rate
        const currentBuffer = nextTime - ctx.currentTime;
        const error = currentBuffer - targetBuffer;

        // proportional correction
        let rate = 1.0 + error * KP;

        // clamp tightly
        rate = Math.min(1 + MAX_RATE_DELTA, Math.max(1 - MAX_RATE_DELTA, rate));

        // slew rate limit: cap how much rate can change per update
        rate = Math.max(playbackRate - MAX_RATE_STEP, Math.min(playbackRate + MAX_RATE_STEP, rate));

        // heavy exponential smoothing
        playbackRate = SMOOTHING * playbackRate + (1 - SMOOTHING) * rate;

        // If we already have too much audio queued, don't try to "rewind" nextTime (you can't unschedule
        // already-started AudioBufferSourceNodes). Drop this chunk and let the buffer drain naturally.
        bufferAhead = nextTime - now;
        if (bufferAhead > targetBuffer + 0.15) {
            if (hasScheduledAudio) {
                return;
            }
            // if we haven't scheduled anything yet, clamp to "now" so playback can start
            nextTime = now;
            bufferAhead = 0;
            playbackRate = 1.0;
        }

        const int16 = new Int16Array(e.data);
        const frames = int16.length / channels;
        const buf = ctx.createBuffer(channels, frames, sampleRate);

        for (let ch = 0; ch < channels; ch++) {
            const out = buf.getChannelData(ch);
            for (let i = 0; i < frames; i++) {
                out[i] = int16[i * channels + ch] / 32768;
            }
        }

        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.playbackRate.value = playbackRate;
 
        src.connect(gainNode);
        src.start(nextTime);
        hasScheduledAudio = true;

        // adjust timing based on playback rate
        nextTime += buf.duration / playbackRate;
     };

    ws.onclose = () => {
        const status = document.getElementById('status');
        status.textContent = 'Disconnected';
        status.className = 'pill';
        stop();
    };
}

function updateLatencyUI() {
    if (!ctx) return;
    const bufMs = Math.max(0, (nextTime - ctx.currentTime) * 1000);
    const totalMs = bufMs + rtt / 2;
    const errMs = (bufMs - targetBuffer * 1000);
    const errSign = errMs >= 0 ? '+' : '';
    const el = document.getElementById('latency');
    el.innerHTML = 'buf <span>' + bufMs.toFixed(0) + 'ms</span> '
                 + 'net <span>' + (rtt/2).toFixed(0) + 'ms</span> '
                 + 'total <span>~' + totalMs.toFixed(0) + 'ms</span> '
                 + 'err <span>' + errSign + errMs.toFixed(0) + 'ms</span> ';
}

function stop() {
    playing = false;
    document.getElementById('btn').textContent = 'Play';
    document.getElementById('latency').innerHTML = '';
    document.getElementById('device').textContent = '';
    if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
    if (watchdog) { clearInterval(watchdog); watchdog = null; }
    if (ws) { ws.close(); ws = null; }
    if (ctx) { ctx.close(); ctx = null; }
    gainNode = null;
    mediaStreamDest = null;
    bgAudio.pause();
    bgAudio.srcObject = null;

    playbackRate = 1.0;
    hasScheduledAudio = false;
}

function setSliderFill(el) {
    const min = parseFloat(el.min || '0');
    const max = parseFloat(el.max || '1');
    const val = parseFloat(el.value || '0');
    const pct = max === min ? 0 : ((val - min) / (max - min)) * 100;
    el.style.setProperty('--fill', pct.toFixed(2) + '%');
}

const volEl = document.getElementById('vol');
const volValEl = document.getElementById('volVal');
volValEl.textContent = Math.round(parseFloat(volEl.value) * 100) + '%';
setSliderFill(volEl);

volEl.addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    volValEl.textContent = Math.round(v * 100) + '%';
    setSliderFill(e.target);
    if (gainNode) gainNode.gain.value = v;
});

const bufEl = document.getElementById('buf');
const bufValEl = document.getElementById('bufVal');
bufValEl.textContent = parseInt(bufEl.value, 10) + ' ms';
setSliderFill(bufEl);

bufEl.addEventListener('input', (e) => {
    const ms = parseInt(e.target.value, 10) || 20;
    bufValEl.textContent = ms + ' ms';
    setSliderFill(e.target);
    targetBuffer = Math.max(0.01, Math.min(MAX_TARGET_BUFFER, ms / 1000.0));
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@sock.route("/ws")
def audio_ws(ws):
    if not device_ready.wait(timeout=5):
        # best-effort: still allow the connection, but the client may fail to init audio
        pass
    # send device config as first message
    ws.send(json.dumps(device_info))

    q: queue.Queue[bytes] = queue.Queue(maxsize=100)
    ping_q: queue.Queue[str] = queue.Queue()
    running = threading.Event()
    running.set()
    client_ready = threading.Event()

    def reader():
        """Read incoming pings and ready signal in separate thread."""
        try:
            while running.is_set():
                try:
                    msg = ws.receive(timeout=30)
                    if msg is None:
                        running.clear()
                        break
                    if isinstance(msg, str):
                        if msg == 'ready':
                            client_ready.set()
                        elif msg.startswith('p'):
                            ping_q.put(msg)
                except simple_websocket.ConnectionClosed:
                    running.clear()
                    break
                except TimeoutError:
                    continue  # timeout is fine, just keep waiting
                except Exception:
                    continue  # ignore other errors, keep trying
        except Exception:
            running.clear()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    # wait for client to signal ready (AudioContext created)
    client_ready.wait(timeout=10)
    if not client_ready.is_set():
        return

    # If WASAPI loopback stalls (or downstream goes idle) during extended silence, send
    # a tiny non-zero PCM pattern so browsers/BT stacks are less likely to disconnect.
    info = device_info if isinstance(device_info, dict) else {}

    cfg_rate = int(info.get("sample_rate", STREAM_SAMPLE_RATE))
    cfg_channels = int(info.get("channels", STREAM_CHANNELS))
    cfg_frames = int(info.get("chunk_frames", max(256, int(round(cfg_rate * TARGET_CHUNK_SECONDS)))))

    # align like capture does (multiple of 64) to match typical chunk sizes
    cfg_frames = int(round(cfg_frames / 64)) * 64
    cfg_frames = max(64, cfg_frames)  # prevent zero after alignment

    silence_payload = build_sparse_dither_silence(
        frames=cfg_frames,
        channels=cfg_channels,
        period_frames=SILENCE_NONZERO_PERIOD_FRAMES,
        lsb=SILENCE_NONZERO_LSB,
    )

    with clients_lock:
        if client_limit is not None and len(clients) >= client_limit:
            ws.close()
            return
        clients.append(q)

    try:
        while running.is_set():
            # echo any pending pings
            while not ping_q.empty():
                try:
                    ws.send(ping_q.get_nowait())
                except Exception:
                    running.clear()
                    break

            try:
                data = q.get(timeout=0.01)

                # If capture produces perfect digital silence, inject a tiny non-zero pattern so
                # BT/iOS pipelines are less likely to idle out during long silence.
                if silence_payload and not any(data):
                    ws.send(silence_payload)
                else:
                    ws.send(data)
            except queue.Empty:
                continue
            except Exception:
                running.clear()
                break
    except Exception:
        pass
    finally:
        running.clear()
        with clients_lock:
            clients.remove(q)

def audio_capture():
    p = pyaudio.PyAudio()

    wasapi_info = None
    for i in range(p.get_host_api_count()):
        info = p.get_host_api_info_by_index(i)
        if info["name"] == "Windows WASAPI":
            wasapi_info = info
            break

    if wasapi_info is None:
        print("Error: WASAPI not available", file=sys.stderr)
        sys.exit(1)

    default_output = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
    device_info["default_output_device"] = default_output["name"]
    
    print(f'Default output device: {default_output["name"]}')

    loopback = None
    for dev in p.get_loopback_device_info_generator():
        if dev["name"].startswith(default_output["name"]):
            loopback = dev
            break

    if loopback is None:
        print("Error: no loopback device found for default output", file=sys.stderr)
        sys.exit(1)

    device_channels = int(loopback["maxInputChannels"])
    device_rate = int(loopback["defaultSampleRate"])

    device_info["loopback_device"] = loopback["name"]

    print(f"Loopback: {loopback['name']}")
    print(f"Device rate: {device_rate}, channels: {device_channels}")

    capture_rate = None
    capture_channels = None
    stream_rate = None
    stream_channels = None
    chunk_frames = None

    def compute_chunk_frames(rate: int) -> int:
        frames = max(256, int(round(rate * TARGET_CHUNK_SECONDS)))
        return int(round(frames / 64)) * 64

    def try_open(rate: int, channels: int):
        nonlocal capture_rate, capture_channels, stream_rate, stream_channels, chunk_frames
        capture_rate = rate
        capture_channels = channels
        stream_rate = rate
        stream_channels = channels
        chunk_frames = compute_chunk_frames(rate)
        return p.open(
            format=pyaudio.paInt16,
            channels=capture_channels,
            rate=capture_rate,
            input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=chunk_frames,
            stream_callback=callback,
        )

    def callback(in_data, frame_count, time_info, status):
        payload = in_data
        if capture_channels and capture_channels > STREAM_CHANNELS:
            try:
                samples = array("h")
                samples.frombytes(in_data)
                out = array("h", [0]) * (frame_count * STREAM_CHANNELS)
                for i in range(frame_count):
                    base = i * capture_channels
                    out[i * 2] = samples[base]
                    out[i * 2 + 1] = samples[base + 1]
                payload = out.tobytes()
            except Exception:
                payload = in_data
        with clients_lock:
            for q in clients:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    # drop oldest chunk to make room
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        pass
        return (None, pyaudio.paContinue)

    stream = None
    open_errors: list[str] = []

    # Prefer 48k stereo for iOS stability; fall back gracefully.
    attempts = [
        (STREAM_SAMPLE_RATE, STREAM_CHANNELS),
        (device_rate, STREAM_CHANNELS),
        (STREAM_SAMPLE_RATE, device_channels),
        (device_rate, device_channels),
    ]

    for rate, channels in attempts:
        try:
            stream = try_open(rate, channels)
            break
        except Exception as e:
            open_errors.append(f"{rate}Hz/{channels}ch: {e!r}")
            stream = None

    if stream is None:
        for line in open_errors:
            print(f"Open failed: {line}", file=sys.stderr)
        raise RuntimeError("Unable to open WASAPI loopback stream")

    if capture_channels and capture_channels > STREAM_CHANNELS:
        stream_channels = STREAM_CHANNELS

    device_info["sample_rate"] = stream_rate
    device_info["channels"] = stream_channels
    device_info["chunk_frames"] = chunk_frames

    if capture_rate != stream_rate or capture_channels != stream_channels:
        print(f"Stream format: {stream_channels}ch @ {stream_rate}Hz (capture {capture_channels}ch @ {capture_rate}Hz)")

    print(f"Chunk: {chunk_frames} frames ({TARGET_CHUNK_SECONDS*1000:.0f}ms)")

    stream.start_stream()
    device_ready.set()

    print("Audio capture running.")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

def run_hidden(cmd, **kwargs):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return subprocess.run(cmd, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)

# get all local IPs with interface info
def get_network_info():
    interfaces = []
    ssid = None

    # get WiFi SSID
    try:
        result = run_hidden(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n"):
            if "SSID" in line and "BSSID" not in line:
                ssid = line.split(":", 1)[1].strip()
                break
    except Exception as e:
        print(f'netsh failed: {e}', file=sys.stderr)

    # parse ipconfig to get adapter names and IPs
    try:
        result = run_hidden(["ipconfig"], capture_output=True, text=True, timeout=5)
        current_adapter = ""

        for line in result.stdout.split("\n"):
            line = line.strip()
            if "adapter" in line.lower() and line.endswith(":"):
                current_adapter = line.lower()
            elif "IPv4" in line and ":" in line:
                ip = line.split(":")[-1].strip()
                if ip.startswith("127."):
                    continue
                # skip virtual adapters
                skip_keywords = ["vmware", "virtualbox", "vethernet", "wsl", "docker", "hyper-v", "loopback"]
                if any(kw in current_adapter for kw in skip_keywords):
                    continue
                # identify interface type (check hotspot IP first)
                if ip.startswith("172.20.") or ip.startswith("172.16."):
                    name = "Hotspot"
                elif "wi-fi" in current_adapter or "wireless" in current_adapter:
                    name = f"WiFi ({ssid})" if ssid else "WiFi"
                elif "ethernet" in current_adapter:
                    name = "Ethernet"
                else:
                    name = "LAN"
                interfaces.append((name, ip))
    except Exception as e:
        print(f'ipconfig failed: {e}', file=sys.stderr)
        # fallback to basic method
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                interfaces.append(("Network", ip))
    interfaces.sort(key=lambda x: 0 if x[0] == 'Hotspot' else 1)
    return interfaces

class Tee:
    def __init__(self, orig, lvl: str):
        self.orig, self.lvl, self.buf = orig, lvl, ''

    def write(self, s: str) -> int:
        try:
            if self.orig is not None:
                self.orig.write(s)
        except Exception:
            pass
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            log_queue.put((self.lvl, line))
        return len(s)

    def flush(self):
        try:
            if self.orig is not None:
                self.orig.flush()
        except Exception:
            pass
        if self.buf.strip():
            log_queue.put((self.lvl, self.buf.strip()))
            self.buf = ''

    def __getattr__(self, n):
        return getattr(self.orig, n)

class MinimalScrollbar(tk.Canvas):
    """Thin canvas scrollbar — no arrows, flat thumb."""
    def __init__(self, parent, command=None, **kw):
        kw.setdefault('width', 6)
        kw.setdefault('bg', Color.mantle)
        super().__init__(parent, highlightthickness=0, bd=0, **kw)
        self.command = command
        self.top = 0.0
        self.bot = 1.0
        self.drag_y = None
        self.create_rectangle(0, 0, 0, 0, fill=Color.surface2, outline='', tags='thumb')
        self.bind('<Configure>',      lambda _: self.redraw())
        self.bind('<ButtonPress-1>',  self.on_press)
        self.bind('<B1-Motion>',      self.on_drag)
        self.bind('<Enter>', lambda _: self.itemconfig('thumb', fill=Color.overlay0))
        self.bind('<Leave>', lambda _: self.itemconfig('thumb', fill=Color.surface2))

    def set(self, top, bottom):
        self.top, self.bot = float(top), float(bottom)
        self.redraw()

    def redraw(self):
        h = self.winfo_height()
        if h < 2:
            return
        w = self.winfo_width()
        y0 = int(h * self.top)
        y1 = max(int(h * self.bot), y0 + 20)
        self.coords('thumb', 1, y0, w - 1, y1)

    def on_press(self, e):
        self.drag_y = e.y
        if self.command:
            span = self.bot - self.top
            self.command('moveto', e.y / self.winfo_height() - span / 2)

    def on_drag(self, e):
        if self.drag_y is None:
            return
        delta = (e.y - self.drag_y) / self.winfo_height()
        self.drag_y = e.y
        if self.command:
            self.command('moveto', self.top + delta)

class StreamerApp:
    pad = 14  # outer window padding

    def __init__(self, root: "tk.Tk", port: int):
        self.root, self.port = root, port
        self.ifaces: list = []
        self.ifaces_displayed: list = []
        self.err: str | None = None
        self.init_window()
        self.build_ui()
        self.start_backend()
        self.poll()

    def init_window(self):
        self.root.title('HA/CI Audio Streamer')
        self.root.configure(bg=Color.base)
        self.root.resizable(True, True)
        self.root.minsize(420, 480)
        try:
            self.root.iconbitmap('icon.ico')
        except Exception:
            pass
        w, h = 480, 600
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    def build_ui(self):
        self.f = {
            'title': tkfont.Font(family='Segoe UI', size=14, weight='bold'),
            'sec':   tkfont.Font(family='Segoe UI', size=8,  weight='bold'),
            'body':  tkfont.Font(family='Segoe UI', size=10),
            'big':   tkfont.Font(family='Segoe UI', size=14, weight='bold'),
            'mono':  tkfont.Font(family='Consolas',  size=9),
            'small': tkfont.Font(family='Segoe UI', size=10),
        }

        # Header
        hdr = tk.Frame(self.root, bg=Color.base)
        hdr.pack(fill='x', padx=self.pad, pady=(self.pad + 2, 4))

        tk.Label(hdr, text='HA/CI Audio Streamer', fg=Color.mauve, bg=Color.base, font=self.f['title']).pack(side='left')
        
        self.dot   = tk.Label(hdr, text='●', fg=Color.yellow, bg=Color.base, font=self.f['small'])
        self.sttxt = tk.Label(hdr, text='Starting…', fg=Color.yellow, bg=Color.base, font=self.f['small'])
        self.dot.pack(side='right', padx=(4, 0))
        self.sttxt.pack(side='right')

        # Divider
        tk.Frame(self.root, bg=Color.surface0, height=1).pack(fill='x', padx=self.pad, pady=(2, 6))

        # Audio device card
        ci = self.card()
        self.sec(ci, 'AUDIO DEVICE')
        self.lout  = self.row(ci, Color.text)
        self.lloop = self.row(ci, Color.subtext0)
        self.lfmt  = self.row(ci, Color.overlay2, font=self.f['mono'])

        # Connected clients card
        ci = self.card()

        hdr = tk.Frame(ci, bg=Color.mantle)
        hdr.pack(fill='x', pady=(0, 6))
        tk.Label(hdr, text='CONNECTED CLIENTS', fg=Color.overlay2, bg=Color.mantle, font=self.f['sec'], anchor='w').pack(side='left')

        self.limit_val = 1
        lim_frame = tk.Frame(hdr, bg=Color.mantle)
        lim_frame.pack(side='right', anchor='center')
        tk.Label(lim_frame, text='limit:', fg=Color.overlay1, bg=Color.mantle, font=self.f['sec']).pack(side='left')
        self.llim = tk.Label(lim_frame, text='1', fg=Color.subtext0, bg=Color.mantle, font=self.f['sec'], width=3, anchor='e')
        self.llim.pack(side='left')
        btn_kw = dict(bg=Color.surface0, fg=Color.subtext1, activebackground=Color.surface1, activeforeground=Color.text,
                      relief='flat', bd=0, cursor='hand2', font=self.f['sec'], padx=3, pady=0)
        tk.Button(lim_frame, text='▲', command=self.limit_up,   **btn_kw).pack(side='left', padx=(4, 1))
        tk.Button(lim_frame, text='▼', command=self.limit_down, **btn_kw).pack(side='left', padx=(1, 0))

        self.lcli = tk.Label(ci, text='0', fg=Color.mauve, bg=Color.mantle, font=self.f['big'], anchor='w')
        self.lcli.pack(fill='x', pady=(0, 2))

        # Network URLs card
        ci = self.card()
        self.sec(ci, 'CONNECT FROM YOUR PHONE')
        self.net_f = tk.Frame(ci, bg=Color.mantle)
        self.net_f.pack(fill='x')

        # Log card (expands to fill remaining space)
        ci = self.card(expand=True)
        self.sec(ci, 'LOG')
        wrap = tk.Frame(ci, bg=Color.crust)
        wrap.pack(fill='both', expand=True)
        self.log = tk.Text(
            wrap, bg=Color.crust, fg=Color.subtext0,
            font=self.f['mono'], relief='flat', bd=0,
            state='disabled', wrap='word',
            padx=8, pady=5, cursor='arrow',
            selectbackground=Color.surface1, selectforeground=Color.text,
        )
        self.log.tag_configure('info',  foreground=Color.subtext0)
        self.log.tag_configure('error', foreground=Color.red)
        vsb = MinimalScrollbar(wrap, command=self.log.yview, bg=Color.crust)
        self.log.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)

    def card(self, expand: bool = False) -> "tk.Frame":
        outer = tk.Frame(self.root, bg=Color.mantle)
        outer.pack(fill='both' if expand else 'x', expand=expand, padx=self.pad, pady=(0, self.pad if expand else 6))

        inner = tk.Frame(outer, bg=Color.mantle)
        inner.pack(fill='both', expand=expand, padx=12, pady=8)

        return inner

    def sec(self, parent: "tk.Frame", text: str):
        tk.Label(parent, text=text, fg=Color.overlay2, bg=Color.mantle, font=self.f['sec'], anchor='w').pack(fill='x', pady=(0, 6))

    def row(self, parent: "tk.Frame", fg: str, font=None) -> "tk.Label":
        lbl = tk.Label(parent, text='', fg=fg, bg=Color.mantle, font=font or self.f['body'], anchor='w')
        lbl.pack(fill='x', pady=1)
        return lbl

    def start_backend(self):
        sys.stdout = Tee(sys.__stdout__, 'info')
        sys.stderr = Tee(sys.__stderr__, 'error')
        threading.Thread(target=self.run_capture, daemon=True).start()
        threading.Thread(target=self.run_flask,   daemon=True).start()
        self.refresh_ifaces()

    def run_capture(self):
        try:
            audio_capture()
        except SystemExit as e:
            self.err = f'Audio init failed (exit {e.code})'
            log_queue.put(('error', self.err))
        except Exception as e:
            self.err = str(e)
            log_queue.put(('error', f'Capture error: {e}'))

    def refresh_ifaces(self):
        """Fetch network interfaces in background; reschedule every second."""
        def fetch():
            self.ifaces = get_network_info()
        threading.Thread(target=fetch, daemon=True).start()
        self.root.after(5000, self.refresh_ifaces)

    def run_flask(self):
        for _ in range(10):
            try:
                app.run(host='0.0.0.0', port=self.port, threaded=True, use_reloader=False, debug=False)
                return
            except (SystemExit, KeyboardInterrupt):
                return
            except OSError:
                self.port += 1
                self.ifaces_displayed = []  # force network card rebuild with new port
            except Exception as e:
                self.err = str(e)
                log_queue.put(('error', f'Flask error: {e}'))
                return
        self.err = 'No available port found'
        log_queue.put(('error', self.err))

    def poll(self):
        # Status indicator
        if self.err:
            c, t = Color.red, 'Error'
        elif device_ready.is_set():
            c, t = Color.green, 'Running'
        else:
            c, t = Color.yellow, 'Starting…'
        self.dot.configure(fg=c)
        self.sttxt.configure(text=t, fg=c)

        # Audio device labels
        d   = device_info
        out = d.get('default_output_device', '')
        if out:
            def trunc(s: str, n: int = 44) -> str:
                return s if len(s) <= n else s[:n - 1] + '…'
            self.lout.configure( text='Output   ' + trunc(out))
            self.lloop.configure(text='Capture  ' + trunc(d.get('loopback_device', ''), 42))
            sr, ch = d.get('sample_rate', ''), d.get('channels', '')
            self.lfmt.configure(text=f'{ch}ch  @  {sr} Hz' if sr else '')
        elif not self.err:
            self.lout.configure( text='Detecting audio device…')
            self.lloop.configure(text='')
            self.lfmt.configure( text='')

        # Connected client count
        with clients_lock:
            n = len(clients)
        self.lcli.configure(text=str(n), fg=Color.green if n else Color.mauve)

        # Network URLs — rebuild whenever the interface list changes
        if self.ifaces != self.ifaces_displayed:
            self.ifaces_displayed = list(self.ifaces)
            for w in self.net_f.winfo_children():
                w.destroy()
            for name, ip in self.ifaces_displayed:
                url = f'http://{ip}:{self.port}'
                row = tk.Frame(self.net_f, bg=Color.mantle)
                row.pack(fill='x', pady=2)

                tk.Label(row, text=f'{name}:', fg=Color.overlay2, bg=Color.mantle, font=self.f['body'], width=16, anchor='w').pack(side='left')
                
                lnk = tk.Label(row, text=url, fg=Color.blue, bg=Color.mantle, font=self.f['mono'], cursor='hand2', anchor='w')
                lnk.pack(side='left')
                lnk.bind('<Button-1>', lambda _, u=url: webbrowser.open(u))
                lnk.bind('<Enter>',    lambda _, l=lnk: l.configure(fg=Color.sapphire))
                lnk.bind('<Leave>',    lambda _, l=lnk: l.configure(fg=Color.blue))

                cpb = tk.Label(row, text='  ⧉', fg=Color.overlay0, bg=Color.mantle, font=self.f['body'], cursor='hand2')
                cpb.pack(side='left')
                cpb.bind('<Button-1>', lambda _, u=url: self.copy(u))
                cpb.bind('<Enter>',    lambda _, b=cpb: b.configure(fg=Color.subtext1))
                cpb.bind('<Leave>',    lambda _, b=cpb: b.configure(fg=Color.overlay0))

        # Drain log queue
        entries = []
        try:
            while True:
                entries.append(log_queue.get_nowait())
        except queue.Empty:
            pass
        if entries:
            self.log.configure(state='normal')
            for lvl, line in entries:
                self.log.insert('end', line + '\n', lvl)
            self.log.configure(state='disabled')
            self.log.see('end')

        self.root.after(500, self.poll)

    def limit_up(self):
        global client_limit
        self.limit_val = min(self.limit_val + 1, 99) if self.limit_val > 0 else 1
        client_limit = self.limit_val
        self.llim.configure(text=str(self.limit_val))

    def limit_down(self):
        global client_limit
        if self.limit_val <= 0:
            return
        self.limit_val -= 1
        client_limit = self.limit_val if self.limit_val > 0 else None
        self.llim.configure(text='∞' if self.limit_val == 0 else str(self.limit_val))

    def copy(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def on_close(self):
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        self.root.destroy()

def main():
    root = tk.Tk()
    StreamerApp(root, port=8000)
    root.mainloop()

if __name__ == "__main__":
    main()
