#!/usr/bin/env python3
"""
FRUIT NINJA -- one-command launcher.

Runs a server that:
  * serves the game to your LAPTOP browser (auto-opened on startup),
  * shows a big QR code on the laptop screen,
  * turns your PHONE into an absolute-trackpad mouse controller,
  * and the moment the phone scans in, flashes "WELCOME" and starts the game.

Phone drives the real mouse via pyautogui, so you slice fruit on the laptop by
swiping on the phone. Both devices must be on the same Wi-Fi network.

--------------------------------------------------------------------------------
INSTALL (exact pip commands):

    python -m pip install aiohttp
    python -m pip install pyautogui
    python -m pip install qrcode

  ...or all at once:

    python -m pip install aiohttp pyautogui qrcode

RUN (that's it):

    python server.py
--------------------------------------------------------------------------------

EMERGENCY STOP (FAILSAFE): slam the mouse cursor into the TOP-LEFT corner of the
screen. pyautogui.FAILSAFE is left ON deliberately -- it aborts the current mouse
action so you can regain control if the phone goes haywire.

macOS note: grant your terminal app "Accessibility" permission
(System Settings -> Privacy & Security -> Accessibility) or pyautogui cannot
move the mouse. See README.md.
"""

import asyncio
import json
import os
import socket
import threading
import time
import webbrowser

from aiohttp import WSCloseCode, WSMsgType, web

import pyautogui
import qrcode

# --------------------------------------------------------------------------- #
# Configuration / pyautogui tuning
# --------------------------------------------------------------------------- #
PORT = 8765

pyautogui.PAUSE = 0          # no artificial delay (critical for slicing)
pyautogui.FAILSAFE = True    # cursor -> top-left corner = emergency stop

SCREEN_W, SCREEN_H = pyautogui.size()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROLLER_HTML = os.path.join(BASE_DIR, "controller.html")
HOST_HTML = os.path.join(BASE_DIR, "host.html")
GAME_HTML = os.path.join(BASE_DIR, "game.html")

# --------------------------------------------------------------------------- #
# Shared server state
# --------------------------------------------------------------------------- #
active_ws = None      # the one phone controller allowed at a time
mouse_down = False    # is the (left) mouse button currently held?
event_count = 0       # controller messages received in the current 1s window
displays = set()      # laptop "display" sockets (the host page) to notify

LAN_IP = "127.0.0.1"
PLAY_URL = ""         # URL the phone opens (encoded in the QR)


# --------------------------------------------------------------------------- #
# LAN IP auto-detection
# --------------------------------------------------------------------------- #
def get_lan_ip():
    """Best-effort detection of this machine's LAN IP address."""
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


def qr_matrix(data):
    """Return the QR code as a matrix of booleans (no image libs needed)."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


# --------------------------------------------------------------------------- #
# Notify laptop display page(s) when the phone connects / disconnects
# --------------------------------------------------------------------------- #
async def notify_displays(connected):
    msg = json.dumps({"type": "controller", "connected": connected})
    for d in list(displays):
        try:
            await d.send_str(msg)
        except Exception:
            displays.discard(d)


# --------------------------------------------------------------------------- #
# Static routes
# --------------------------------------------------------------------------- #
def _file(path, missing):
    if not os.path.exists(path):
        return web.Response(status=500, text=missing)
    return web.FileResponse(path)


async def host_page(request):
    return _file(HOST_HTML, "host.html not found next to server.py")


async def controller_page(request):
    return _file(CONTROLLER_HTML, "controller.html not found next to server.py")


async def game_page(request):
    return _file(GAME_HTML, "game.html not found next to server.py")


async def api_info(request):
    return web.json_response({
        "url": PLAY_URL,
        "matrix": qr_matrix(PLAY_URL),
    })


# --------------------------------------------------------------------------- #
# WebSocket: role=controller (phone, default) or role=display (laptop host page)
# --------------------------------------------------------------------------- #
async def websocket_handler(request):
    role = request.query.get("role", "controller")
    if role == "display":
        return await display_socket(request)
    return await controller_socket(request)


async def display_socket(request):
    """Laptop host page: passive listener told when the phone joins/leaves."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    displays.add(ws)
    # Tell it the current status right away.
    try:
        await ws.send_str(json.dumps({
            "type": "controller",
            "connected": active_ws is not None and not active_ws.closed,
        }))
    except Exception:
        pass
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    d = json.loads(msg.data)
                except Exception:
                    continue
                if d.get("type") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "t": d.get("t")}))
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        displays.discard(ws)
    return ws


async def controller_socket(request):
    """Phone controller. One at a time; a new connection replaces the old.

    Incoming JSON:
        {"type":"move","x":0..1,"y":0..1} / {"type":"down"} / {"type":"up"}
        {"type":"ping","t":<client-ts>} -> {"type":"pong","t":...}

    Stale 'move' events coalesce (latest wins); every 'down'/'up' is preserved.
    """
    global active_ws, mouse_down, event_count

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    peer = request.remote

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
    await notify_displays(True)

    loop = asyncio.get_event_loop()
    pending = []
    work_event = asyncio.Event()
    stopping = False

    def run_batch(batch):
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
            if mouse_down:
                try:
                    pyautogui.mouseUp(_pause=False)
                except Exception:
                    pass
                mouse_down = False
            print("[server] !! FAILSAFE triggered (cursor in corner) -- released button")

    async def applier():
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
                    await ws.send_str(json.dumps({"type": "pong", "t": data.get("t")}))
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

        if mouse_down:
            try:
                pyautogui.mouseUp(_pause=False)
            except Exception:
                pass
            mouse_down = False
            print("[server] released held mouse button on disconnect")

        if active_ws is ws:
            active_ws = None
            await notify_displays(False)
        print(f"[server] controller disconnected: {peer}")

    return ws


# --------------------------------------------------------------------------- #
# Background tasks
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
    # Auto-open the game on the laptop shortly after the server is listening.
    loop = asyncio.get_event_loop()

    def _open():
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        except Exception:
            pass

    loop.call_later(0.8, _open)


async def on_cleanup(app):
    task = app.get("rate_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    global mouse_down
    if mouse_down:
        try:
            pyautogui.mouseUp(_pause=False)
        except Exception:
            pass
        mouse_down = False


# --------------------------------------------------------------------------- #
# Startup banner + terminal QR (fallback if you'd rather scan from here)
# --------------------------------------------------------------------------- #
def print_banner():
    print()
    print("=" * 60)
    print("  FRUIT NINJA -- phone-controlled")
    print("=" * 60)
    print(f"  Screen resolution : {SCREEN_W} x {SCREEN_H}")
    print(f"  Laptop (game)     : http://127.0.0.1:{PORT}/   (opening now...)")
    print(f"  Phone (controller): {PLAY_URL}")
    print()
    print("  A big QR code is showing on the laptop screen -- scan it with")
    print("  your phone. Or scan this one from the terminal:")
    print()

    qr = qrcode.QRCode(border=2)
    qr.add_data(PLAY_URL)
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
    global LAN_IP, PLAY_URL
    LAN_IP = get_lan_ip()
    PLAY_URL = f"http://{LAN_IP}:{PORT}/play"

    app = web.Application()
    app.router.add_get("/", host_page)             # laptop: QR lobby + game
    app.router.add_get("/play", controller_page)   # phone: the controller
    app.router.add_get("/game", game_page)          # the game itself
    app.router.add_get("/api/info", api_info)       # QR data for the host page
    app.router.add_get("/ws", websocket_handler)    # ?role=display | controller
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print_banner()

    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[server] shutting down. bye!")


if __name__ == "__main__":
    main()
