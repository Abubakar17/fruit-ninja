#!/usr/bin/env python3
"""
Phone-as-mouse server for playing Fruit Ninja (browser version) on your laptop,
using your phone's touchscreen as an absolute trackpad.

--------------------------------------------------------------------------------
INSTALL (exact pip commands):

    python -m pip install aiohttp
    python -m pip install pyautogui
    python -m pip install qrcode

  ...or all at once:

    python -m pip install aiohttp pyautogui qrcode

RUN:

    python server.py

Then scan the QR code shown in the terminal with your phone (same Wi-Fi network),
or open the printed http://<lan-ip>:8765/ URL in your phone's browser.
--------------------------------------------------------------------------------

EMERGENCY STOP (FAILSAFE): slam the mouse cursor into the TOP-LEFT corner of the
screen. pyautogui.FAILSAFE is left ON deliberately -- it aborts the current mouse
action so you can regain control if the phone goes haywire.

macOS note: you must grant your terminal app "Accessibility" permission
(System Settings -> Privacy & Security -> Accessibility) or pyautogui cannot
move the mouse. See README.md.
"""

import asyncio
import json
import os
import socket
import sys
import time

from aiohttp import WSCloseCode, WSMsgType, web

import pyautogui
import qrcode

# --------------------------------------------------------------------------- #
# Configuration / pyautogui tuning
# --------------------------------------------------------------------------- #
PORT = 8765

# Performance: no artificial pause between pyautogui calls (critical for slicing).
pyautogui.PAUSE = 0
# Keep the failsafe ON -- cursor to top-left corner = emergency stop.
pyautogui.FAILSAFE = True

# Cache the screen size once (recomputed cheaply is possible, but the resolution
# does not change mid-game).
SCREEN_W, SCREEN_H = pyautogui.size()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROLLER_HTML = os.path.join(BASE_DIR, "controller.html")

# --------------------------------------------------------------------------- #
# Shared server state
# --------------------------------------------------------------------------- #
active_ws = None      # the one controller allowed at a time
mouse_down = False    # is the (left) mouse button currently held?
event_count = 0       # messages received in the current 1s window


# --------------------------------------------------------------------------- #
# LAN IP auto-detection
# --------------------------------------------------------------------------- #
def get_lan_ip():
    """Best-effort detection of this machine's LAN IP address.

    Primary trick: open a UDP socket "to" a public address (no packets are
    actually sent for UDP connect) and read back the local address the OS chose.
    Falls back to gethostbyname, then to loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# HTTP + WebSocket handlers
# --------------------------------------------------------------------------- #
async def index(request):
    """Serve the self-contained controller page."""
    if not os.path.exists(CONTROLLER_HTML):
        return web.Response(
            status=500,
            text="controller.html not found next to server.py",
        )
    return web.FileResponse(CONTROLLER_HTML)


async def websocket_handler(request):
    """One WebSocket controller at a time. A new connection replaces the old.

    Incoming JSON messages:
        {"type": "move", "x": 0..1, "y": 0..1}
        {"type": "down"}
        {"type": "up"}
        {"type": "ping", "t": <client-timestamp>}  -> {"type": "pong", "t": ...}

    Stale 'move' events are coalesced/dropped (only the latest position matters),
    but every 'down' and 'up' is preserved in order so the game never gets stuck
    mid-slice.
    """
    global active_ws, mouse_down, event_count

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    peer = request.remote

    # Only one active controller: replace any existing one.
    if active_ws is not None and not active_ws.closed:
        print(f"[server] new controller {peer} replacing existing controller")
        try:
            await active_ws.close(
                code=WSCloseCode.GOING_AWAY, message=b"replaced by new controller"
            )
        except Exception:
            pass
    active_ws = ws
    print(f"[server] controller connected: {peer}")

    loop = asyncio.get_event_loop()

    # pending is a small ordered buffer of actions to apply. Consecutive 'move'
    # actions are coalesced (latest wins) -> stale moves are dropped, while
    # 'down'/'up' are always kept and never reordered.
    pending = []
    work_event = asyncio.Event()
    stopping = False

    def run_batch(batch):
        """Apply a batch of actions on the executor thread. Blocking pyautogui
        calls live here so the event loop keeps receiving (and coalescing) moves.
        """
        global mouse_down
        try:
            for item in batch:
                kind = item[0]
                if kind == "move":
                    px = int(item[1] * SCREEN_W)
                    py = int(item[2] * SCREEN_H)
                    pyautogui.moveTo(px, py, _pause=False)
                elif kind == "down":
                    pyautogui.mouseDown(_pause=False)
                    mouse_down = True
                elif kind == "up":
                    pyautogui.mouseUp(_pause=False)
                    mouse_down = False
        except pyautogui.FailSafeException:
            # Emergency stop was triggered (cursor slammed into a corner).
            if mouse_down:
                try:
                    pyautogui.mouseUp(_pause=False)
                except Exception:
                    pass
                mouse_down = False
            print("[server] !! FAILSAFE triggered (cursor in corner) -- released button")

    async def applier():
        """Drains the pending buffer and applies actions off the event loop."""
        while True:
            await work_event.wait()
            work_event.clear()
            if not pending:
                if stopping:
                    return
                continue
            batch = pending[:]
            pending.clear()
            await loop.run_in_executor(None, run_batch, batch)

    applier_task = asyncio.ensure_future(applier())

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                event_count += 1
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                mtype = data.get("type")
                if mtype == "move":
                    try:
                        x = float(data.get("x", 0.0))
                        y = float(data.get("y", 0.0))
                    except (TypeError, ValueError):
                        continue
                    x = 0.0 if x < 0.0 else 1.0 if x > 1.0 else x
                    y = 0.0 if y < 0.0 else 1.0 if y > 1.0 else y
                    # Coalesce with a trailing move -> drop the stale one.
                    if pending and pending[-1][0] == "move":
                        pending[-1] = ("move", x, y)
                    else:
                        pending.append(("move", x, y))
                    work_event.set()
                elif mtype == "down":
                    pending.append(("down",))
                    work_event.set()
                elif mtype == "up":
                    pending.append(("up",))
                    work_event.set()
                elif mtype == "ping":
                    # Echo the client's timestamp so the phone can measure RTT.
                    await ws.send_str(
                        json.dumps({"type": "pong", "t": data.get("t")})
                    )
            elif msg.type == WSMsgType.ERROR:
                print(f"[server] ws error: {ws.exception()}")
                break
    finally:
        stopping = True
        work_event.set()
        applier_task.cancel()
        try:
            await applier_task
        except asyncio.CancelledError:
            pass

        # Safety: if the phone vanished mid-slice, release the button.
        if mouse_down:
            try:
                pyautogui.mouseUp(_pause=False)
            except Exception:
                pass
            mouse_down = False
            print("[server] released held mouse button on disconnect")

        if active_ws is ws:
            active_ws = None
        print(f"[server] controller disconnected: {peer}")

    return ws


# --------------------------------------------------------------------------- #
# Background: once-per-second event-rate counter
# --------------------------------------------------------------------------- #
async def rate_reporter():
    global event_count
    try:
        while True:
            await asyncio.sleep(1.0)
            n = event_count
            event_count = 0
            if n:
                print(f"[server] events/s: {n}")
    except asyncio.CancelledError:
        pass


async def on_startup(app):
    app["rate_task"] = asyncio.ensure_future(rate_reporter())


async def on_cleanup(app):
    task = app.get("rate_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Final safety net on shutdown.
    global mouse_down
    if mouse_down:
        try:
            pyautogui.mouseUp(_pause=False)
        except Exception:
            pass
        mouse_down = False


# --------------------------------------------------------------------------- #
# Startup banner + QR
# --------------------------------------------------------------------------- #
def print_banner(url):
    print()
    print("=" * 60)
    print("  FRUIT NINJA -- phone-as-mouse controller server")
    print("=" * 60)
    print(f"  Screen resolution : {SCREEN_W} x {SCREEN_H}")
    print(f"  Controller URL    : {url}")
    print()
    print("  Scan this QR with your phone (same Wi-Fi network):")
    print()

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print()
    print("  EMERGENCY STOP (FAILSAFE): slam the mouse cursor into the")
    print("  TOP-LEFT corner of the screen to abort. (Keep it enabled!)")
    print()
    print("  Press Ctrl+C to quit.")
    print("=" * 60)
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ip = get_lan_ip()
    url = f"http://{ip}:{PORT}/"

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print_banner(url)

    try:
        # print=None: we already printed our own banner.
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[server] shutting down. bye!")


if __name__ == "__main__":
    main()
