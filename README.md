# NINJA PAD — phone as laptop mouse for Fruit Ninja

Use your phone's touchscreen as an **absolute trackpad** to control the mouse on
your laptop, so you can slice fruit in the browser version of Fruit Ninja by
swiping on your phone. The phone screen maps **1:1** to the laptop screen —
touch the top-right of the phone, the cursor jumps to the top-right of the
laptop.

Two pieces:

- **`server.py`** — runs on the laptop (aiohttp HTTP + WebSocket on one port,
  drives the mouse with pyautogui).
- **`controller.html`** — a single self-contained page served to the phone. No
  build step, no CDNs, works fully offline on your LAN.

Both devices must be on the **same Wi-Fi network**.

---

## Install

```
python -m pip install aiohttp pyautogui qrcode
```

(Individually: `python -m pip install aiohttp`,
`python -m pip install pyautogui`, `python -m pip install qrcode`.)

## Run

1. On the laptop:

   ```
   python server.py
   ```

2. The terminal prints the controller URL (e.g. `http://192.168.1.42:8765/`) and
   an ASCII **QR code**. Scan the QR with your phone's camera, or type the URL
   into the phone's browser.

3. **Rotate the phone to landscape.** A "rotate your phone" overlay appears in
   portrait — landscape is the intended mode.

4. Open Fruit Ninja (browser version) on the laptop and click into it so it has
   focus. Swipe on the phone to slice.

The status bar on the phone shows a connection dot (green = connected,
amber = connecting/reconnecting, red = offline) and the round-trip latency.

## How to play

- **Swipe** → move + slice.
- **Press and drag** → continuous slice (button held while you drag).
- **Lift your finger** → release.
- Close the phone browser mid-slice → the laptop releases the mouse button
  automatically, so nothing gets stuck.

---

## ⚠️ Emergency stop (FAILSAFE)

pyautogui's failsafe is left **ON** on purpose. If the cursor ever misbehaves,
**slam the mouse into the very TOP-LEFT corner of the screen** — this aborts the
current mouse action and releases the button. It's your panic button.

---

## Troubleshooting

**Can't reach the URL / QR page won't load from the phone**

- **Firewall** is the usual culprit. Allow inbound connections to Python on port
  **8765** (TCP). On Windows the first run often pops a "Windows Defender
  Firewall" prompt — choose **Allow access** on **Private networks**. On macOS,
  System Settings → Network → Firewall → allow Python. On Linux, open the port
  (e.g. `sudo ufw allow 8765/tcp`).
- Confirm both devices are on the **same network**. Many home routers put a
  "Guest" Wi-Fi on an **isolated VLAN** that can't reach your laptop — join the
  main network on both devices. Corporate/campus Wi-Fi often has "client
  isolation" (AP isolation) that blocks phone→laptop traffic entirely; use a
  personal hotspot or a home router instead.
- Verify the printed IP is really the laptop's LAN address. If it shows
  `127.0.0.1`, IP auto-detection failed — connect to your Wi-Fi first, or find
  the address manually (`ipconfig` on Windows, `ifconfig`/`ip addr` elsewhere)
  and browse to `http://<that-ip>:8765/`.

**A VPN is on** (on the phone or the laptop)

- VPNs frequently route or block LAN traffic and break same-network discovery.
  **Turn the VPN off on both devices** while playing.

**The page loads but the cursor doesn't move**

- **macOS Accessibility permission is required.** pyautogui can't control the
  mouse until you grant it: System Settings → Privacy & Security →
  **Accessibility** → enable your terminal app (Terminal, iTerm, VS Code, etc.).
  Quit and relaunch the terminal after granting, then re-run `server.py`.
- Make sure the Fruit Ninja browser window/tab is **focused** on the laptop —
  the mouse moves globally, but the game only reacts when it has focus.

**Laggy / jittery slicing**

- Weak Wi-Fi signal or a congested 2.4 GHz band. Move closer to the router or
  switch to 5 GHz. The status bar's latency readout should stay low (single/low
  double-digit ms on a healthy LAN).

**Phone screen dims or sleeps mid-game**

- The page requests a screen wake lock, but some browsers/OSes deny it. Bump
  your phone's auto-lock/screen-timeout setting up as a fallback.

---

## Notes / internals

- One controller at a time — a new phone connection **replaces** the old one.
- `move` events are coalesced (stale positions dropped, latest wins) for
  low-latency slicing, but every `down`/`up` is preserved in order so a slice
  never gets stuck half-open.
- `pyautogui.PAUSE = 0` for maximum responsiveness; `FAILSAFE = True` as the
  emergency stop described above.
- The server logs connects/disconnects and a once-per-second events/second
  counter so you can see it's alive. `Ctrl+C` shuts it down cleanly (and
  releases the mouse button if it was held).
