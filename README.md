<p align="center">
  <img src="src/wyze_print_monitor/assets/icon.png" width="128" alt="Wyze Print Monitor logo">
</p>

<h1 align="center">Wyze Print Monitor</h1>

<p align="center">
  Watch and control your 3D printer's <b>Wyze Cam V3</b> — right inside <b>eufyMake Studio</b>.
</p>

<p align="center">
  <a href="https://github.com/ZOGWORK/wyze-print-monitor/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/ZOGWORK/wyze-print-monitor?label=download&color=62d361"></a>
  <a href="https://github.com/ZOGWORK/wyze-print-monitor/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ZOGWORK/wyze-print-monitor/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Windows 10/11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-lightgrey"></a>
</p>

<p align="center">
  <a href="https://github.com/ZOGWORK/wyze-print-monitor/releases/latest"><b>⬇ Download the latest installer</b></a>
</p>

---

eufyMake Studio's Device tab can only show a printer's *built-in* camera.
Wyze Print Monitor puts the live feed from a Wyze Cam V3 mounted on your
printer **into that tab** — plus full camera controls, snapshots, recordings,
print timelapses, picture adjustments, and a local **AI print watch** that
alerts you to failed prints (and pauses them when it's sure).

![The Wyze feed and controls docked in eufyMake Studio](docs/screenshots/studio-dock.png)

## Features

- **Live view inside eufyMake Studio:** the Wyze feed and camera controls
  sit right in the Device tab, for the printer that has the camera. You can
  also use a full window or fullscreen.
- **Smooth, low-delay video:** Auto quality steps down from 1080p to 720p to
  360p when your PC is busy or the network is slow, then steps back up later.
  You can also pick a quality by hand.
- **Camera controls:** power, restart, siren, spotlight, motion detection and
  notifications, through your Wyze account.
- **Snapshots, recordings and print timelapses**, with recent captures one
  click away.
- **Picture adjustments**, shown live beside the video:
  - **Auto lighting**, which keeps the picture well exposed as the room's
    light changes;
  - brightness, contrast, exposure, gamma, saturation, warmth and sharpness;
  - rotate, flip, and digital zoom with drag-to-pan.
- **AI print watch** (local, free, optional), using Obico's open
  failure-detection model on your PC:
  - **alerts** you to spaghetti, blobs and knocked-loose parts with a banner,
    a Windows notification, a beep and an outlined snapshot;
  - **auto-pauses** through Studio only when it's very sure;
  - **never stops** a print without asking you.
- **Unusual-sound alerts:** it learns how your print normally sounds and
  flags grinding, clicking or a loud bang.
- **Light on your PC:** about 1% CPU for the AI watch, and it only runs while
  a print is running.
- **Easy install and updates:** a Windows installer, a Start-menu entry, and
  in-app updates that keep your settings.
- **Private:** passwords stay in Windows Credential Manager. Video and AI
  never leave your network.

## Install (Windows 10 / 11)

1. Download **`WyzePrintMonitor-<version>-setup.exe`** from the
   [latest release](https://github.com/ZOGWORK/wyze-print-monitor/releases/latest).
2. Run it. If Windows shows **"Windows protected your PC"**, click
   **More info → Run anyway** — the installer isn't code-signed, so
   SmartScreen doesn't know it yet.
3. Follow the wizard (no admin rights needed). Tick *Create a desktop icon*
   if you want one.
4. Open **Wyze Print Monitor** from the Start menu — or just press the
   Windows key and type **"Wyze"**.

Uninstall any time from **Settings → Apps → Installed apps**.

## First-time setup

Open the app's **Settings** (gear icon) and fill in two things:

**1. The camera's video stream (RTSP)**
In the Wyze phone app: your camera → **Settings → Advanced Settings → RTSP**
→ turn it on, choose a username and password, then **Generate URL**. Paste
the URL into *RTSP URL*, with your RTSP username and password in it:

```
rtsps://USERNAME:PASSWORD@192.168.1.50:322/stream0
```

> If the RTSP password contains `@`, `:`, `/`, `#` or `%`, write them as
> `%40`, `%3A`, `%2F`, `%23` and `%25`. Keep the `s` in `rtsps` if Wyze gave
> it to you — that stream is encrypted.

**2. Wyze account access (for the camera controls)**
Create an **API key ID** and **API key** at the
[Wyze developer console](https://developer-api-console.wyze.com/) (sign in
with your normal Wyze account → *API Key* → *Create*), then enter your Wyze
email, password, key ID and key in Settings and click
**Connect & list cameras** → **Save**. Two-factor codes are asked for when
needed.

Everything secret (RTSP password, Wyze password, API keys) is stored in
**Windows Credential Manager**, never in a file.

Your settings live in `%APPDATA%\wyze-print-monitor\settings.json` and your
secrets in Credential Manager — outside the app's folder — so they're kept
through updates (and even an uninstall / reinstall).

## Using it

The same two pieces appear inside eufyMake Studio and in the app's own
window:

**The video** fills its space edge to edge. Status floats over the top
(*LIVE · resolution · fps*, a *REC* timer, timelapse progress, zoom level),
and a floating toolbar sits over the bottom — it fades away while you're
not moving the mouse:

| | |
| --- | --- |
| 📷 Snapshot · ⏺ Record · ⏱ Timelapse | capture (see below) |
| ☀ Picture | brightness, exposure, rotate, zoom… |
| ⛶ Fit / Fill | show the whole picture, or fill the space (crops the edges) |
| 📁 Folders · ⤢ Fullscreen · ⧉ Full window · ⚙ Settings | |

**Scroll** over the video to zoom in (great for watching the nozzle),
**drag** to move around while zoomed, **double-click** for fullscreen
(Esc to leave). Click the zoom chip to reset.

**The camera panel** has big toggles for the **siren**, **spotlight**,
**motion detection** and **alerts** (push notifications), power on/off and
restart, your **recent captures** (click one to open it), quick
**brightness / exposure / zoom** sliders, and the camera's details. If the
Wyze connection needs attention, a banner at the top says why — with a
Connect button.

### Inside eufyMake Studio

With Studio open on the **Device** tab and your camera's printer selected,
the video replaces Studio's **Monitor** and the camera panel replaces its
empty **Others** card. Studio's own Control, Progress / Start Printing and
printer list are left alone. The panels follow Studio's window and
disappear on the Slice tab. They only show while the printer with the
camera is selected — pick which one in Settings → **Printer with camera**
(row 1 by default).

### The full window

Open it from the tray icon, the Start menu, or the full-window button on
the docked toolbar. Closing it while Studio is open just hides it; quit
from **File → Exit** or the tray icon.

![The full window](docs/screenshots/main-window.png)

### Video quality

The quality chip on the video toolbar (or Settings → *Video quality*):

- **Auto** (recommended) — full HD while everything keeps up. If fewer than
  10 frames a second reach the screen for a few seconds (busy PC or slow
  network), it drops to **720p**, then to the camera's **360p** stream if
  needed, and tries the next level up again later.
- **1080p** — always full HD.
- **720p** — the HD stream scaled down right after decoding: smooth on
  slower PCs, smaller recordings.
- **360p** — the camera's low-bandwidth stream.

All video work (decoding, scaling, adjustments, recording) happens off the
UI thread, and the picture always shows the newest frame — it never falls
behind the camera when the PC is busy.

### Picture adjustments

The ☀ button (or *All adjustments…*) turns the camera panel into the
picture controls — beside the video, so you see every change live; *Done*
goes back. Brightness,
contrast, exposure, gamma, saturation, warmth, sharpness, rotate 90°, flip,
and digital zoom with pan. Adjustments apply to the live view *and* to
snapshots, recordings and timelapses, and are remembered. Double-click a
value to reset it, or **Reset all**.

**Auto lighting** keeps the picture well exposed as the room gets brighter
or darker (it eases changes in, so the image doesn't pump); the sliders
still fine-tune on top.

### Print watch (AI failure detection)

Tick **Download the AI print-failure model** in the installer (≈200 MB, once),
or later in Settings → **Print watch** → *Download model*. Turn it on or off
any time with the **Print watch (AI)** tile in the camera panel, the
*Settings* menu, or Settings → Print watch (which also has auto-pause,
sensitivity and sound alerts). It runs entirely on your PC — no account, no
cloud, no subscription — using [Obico](https://www.obico.io)'s open
failure-detection model.

- **Alerts** when something *might* be wrong (spaghetti, a blob, a part
  knocked loose): a banner on the video, a Windows notification, a beep, and
  a snapshot with the suspect area outlined (saved with your snapshots).
- **Pauses** the print through Studio's own Pause button only when it's
  *very* sure — the failure signal has to hold for over 30 seconds. You can
  turn auto-pause off.
- **Never stops** a print by itself: *Stop print…* is a button on the alert,
  and it asks first.
- **Unusual sounds** (optional): it learns how the current print normally
  sounds from the camera's microphone and alerts on grinding, clicking or a
  loud bang. Sound only ever alerts.

**How you're told:** a banner on the video (with *Pause*, *Stop print…*,
*Looks fine* and *Silence*), a **pop-up alert card** in the corner of the
screen that stays on top of Studio and other apps until you respond, a
Windows notification, and a sound — by
default an **alarm that repeats until you respond** for likely failures,
and a short chime for "take a look". Settings → Print watch lets you choose
what alerts you (possible problems, or only likely failures), the sound
(alarm / chime / silent), notifications, how long a failure must last
before it pauses (30 s / 1 min / 2 min) and how often it checks (10 / 20 /
30 s); *Test alert* shows exactly what you'll get.

It only runs while Studio shows a print running — about 1% of a modern CPU
at the default 10-second checks. If Studio isn't open it waits, unless you
turn on *Also watch when Studio isn't showing a print* (for prints started
some other way; it may then alert with nothing printing). *Mark the print
bed…* lets you drag a box around the bed on the camera picture, and anything
the model sees outside it (shelves, cables, the floor) is ignored. *Looks fine* on an alert keeps it quiet for 30 minutes **and teaches it**:
on an AI alert, what the camera saw counts more as normal for your setup
(a cable or purge line that fooled it alerts less next time; this fades by
itself over many hours if the scene changes); on a sound alert, the rest of
that print needs a louder noise to alert again.

### Snapshots, recordings, timelapses

Saved to `Pictures\wyze-print-monitor` and `Videos\wyze-print-monitor` by
default (change in Settings) and listed under *Recent captures*. A
timelapse grabs a frame every N seconds (Settings → *Timelapse interval*)
and is compiled into an MP4 when you stop it.

## Updates

The app checks for new releases at startup, every hour, and when you open
Settings — no restart needed. When one is
out, an **Update** button appears (status bar, docked toolbar, and a tray
notification). Click it and the app downloads the new installer, verifies
it, installs it and reopens — your settings and credentials are kept.
Check any time with **Settings → General → Check for updates now** (works
from the Studio dock too), the tray icon's **Check for updates…**, or
**Help → Check for updates…**; turn automatic checks off in Settings.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Video says *Camera stream unavailable* / keeps reconnecting | Check the RTSP URL (username, password, `rtsps`). If the Wyze app shows the camera as offline, unplug it for 10 seconds. |
| *Wyze is temporarily limiting logins* | Too many login attempts. The app retries by itself after 1, 5 and 15 minutes; avoid clicking Connect repeatedly. |
| *Forbidden control character* on login | Fixed in 1.0 — re-paste the API key if you're on an older build. |
| Panels don't show inside Studio | Studio must be on the **Device** tab with the camera's printer selected, and its UI in English. Check Settings → *Show inside eufyMake Studio's Device tab*. |
| *Windows protected your PC* | Click **More info → Run anyway** (unsigned installer). |

## For developers

Python 3.10+ with PySide6, OpenCV, wyzeapy and keyring.

```bash
pip install -e ".[dev]"
wyze-print-monitor            # run from source
PYTHONPATH=src pytest tests/  # tests (Qt-free)
```

**Build the Windows app + installer** (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer):

```bash
pip install -r packaging/requirements-build.txt && pip install --no-deps .
python packaging/build_windows.py
build\dist\WyzePrintMonitor\WyzePrintMonitor.exe --self-test report.txt
```

**Release:** bump `__version__` in `src/wyze_print_monitor/__init__.py`, merge,
then push a tag `vX.Y.Z`. The *Release* workflow builds and self-tests the
installer and publishes the GitHub release that installed apps update from.

Layout: `app.py` (entry, tray, single instance), `ui/main_window.py` (the
camera / capture logic and the controls that own it), `ui/views.py` (the
video view and camera panel you see), `ui/binder.py` (binds views to the
main window's controls), `ui/studio_dock.py` + `studio_win32.py` (embedding
into Studio), `adjust.py` (picture adjustments), `capture.py` /
`timelapse.py` / `captures.py` (stream, recording, recent captures),
`wyze_control.py` (Wyze cloud), `updater.py` + `ui/update_ui.py` (updates),
`installer/` + `packaging/` (Windows build; `packaging/make_artwork.py`
regenerates icons and installer art from the logo).

## License

MIT — see [LICENSE](LICENSE).

The print-watch model is not part of this app: it's downloaded separately,
from Obico, and is licensed by Obico under
[AGPL-3.0](https://github.com/TheSpaghettiDetective/obico-server/blob/release/LICENSE).
