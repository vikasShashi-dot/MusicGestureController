# MusicGestureController
# Hand Gesture Music Controller — Spotify Edition

Control Spotify hands-free using real-time hand gesture recognition via your webcam. Built with MediaPipe, OpenCV, and the Spotify Web API.

---

## Demo

| Mode | Gesture | Action |
|------|---------|--------|
| Mode 1 | Open Palm | Play |
| Mode 1 | Closed Fist | Pause |
| Mode 1 | Thumb Up | Like / Unlike |
| Mode 2 | Hand Up | Volume Up |
| Mode 2 | Hand Down | Volume Down |
| Mode 3 | Hand Right | Seek +10s |
| Mode 3 | Hand Left | Seek -10s |
| Mode 4 | Hand Right | Next Track |
| Mode 4 | Hand Left | Previous Track |
| Any | Two Hands Spread | Toggle Shuffle |

---

## Requirements

- Python 3.8+
- A webcam
- A Spotify Premium account
- A Spotify Developer app (free to create)

---

## Installation

**1. Clone the repo**

```bash
git clone https://github.com/yourname/gesture-music-controller.git
cd gesture-music-controller
```

**2. Create and activate a virtual environment (recommended)**

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

**3. Install dependencies**

```bash
pip install opencv-python mediapipe spotipy numpy
```

On Windows, also install `pycaw` for native volume control:

```bash
pip install pycaw comtypes
```

---

## Spotify Setup

**1. Create a Spotify Developer app**

- Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
- Click **Create app**
- Set the **Redirect URI** to exactly: `http://127.0.0.1:8888/callback`
- Note your **Client ID** and **Client Secret**

**2. Set environment variables**

```bash
# macOS / Linux
export SPOTIFY_CLIENT_ID="your_client_id_here"
export SPOTIFY_CLIENT_SECRET="your_client_secret_here"

# Windows (Command Prompt)
set SPOTIFY_CLIENT_ID=your_client_id_here
set SPOTIFY_CLIENT_SECRET=your_client_secret_here

# Windows (PowerShell)
$env:SPOTIFY_CLIENT_ID="your_client_id_here"
$env:SPOTIFY_CLIENT_SECRET="your_client_secret_here"
```

Alternatively, paste your credentials directly into the script at the top of `gesture_controller.py`:

```python
SPOTIFY_CLIENT_ID     = "your_client_id_here"
SPOTIFY_CLIENT_SECRET = "your_client_secret_here"
```

> **First run only:** A browser window will open asking you to log in to Spotify and grant permissions. The token is cached in `.spotify_cache` after that — you won't be asked again.

---

## Usage

```bash
python gesture_controller.py
```

Press **`h`** to toggle the in-window help overlay.  
Press **`q`** to quit.

---

## How Modes Work

Modes are switched by **holding a finger count** for ~0.4 seconds. Once locked, the mode stays active until you deliberately hold a different finger count — no accidental drops.

```
No hand visible  ->  previous mode is preserved

1 finger held    ->  MODE 1: Play / Pause / Like
2 fingers held   ->  MODE 2: Volume
3 fingers held   ->  MODE 3: Seek
4 fingers held   ->  MODE 4: Change Song
```

A thin progress bar appears in the sidebar as you hold a new finger count, showing how close you are to switching modes.

### Mode 1 — Play / Pause / Like
Enter by holding up only your **index finger**.

| Gesture | Action |
|---------|--------|
| Open Palm (all 5 fingers up) | Play |
| Closed Fist | Pause |
| Thumb Up (thumb only, all others curled) | Like / Unlike current track |

### Mode 2 — Volume
Enter by holding up your **index + middle fingers**.

| Movement | Action |
|----------|--------|
| Hand moves UP | Volume +8% |
| Hand moves DOWN | Volume -8% |

### Mode 3 — Seek
Enter by holding up your **index + middle + ring fingers**.

| Movement | Action |
|----------|--------|
| Hand moves RIGHT | Seek forward 10 seconds |
| Hand moves LEFT | Seek backward 10 seconds |

### Mode 4 — Change Song
Enter by holding up **all four fingers** (index, middle, ring, pinky — thumb tucked).

| Movement | Action |
|----------|--------|
| Hand moves RIGHT | Next Track |
| Hand moves LEFT | Previous Track |

### Any Mode — Shuffle
Bring a **second hand** into frame and spread both hands apart to toggle shuffle on or off.

---

## Overlay

The left sidebar shows:
- Current hand posture being detected
- Playback state (Playing / Paused)
- Active mode with colour indicator
- Mode-switch progress bar (fills as you hold a new finger count)
- Last action fired
- Action log with relative timestamps (e.g. `3s ago`, `1m ago`)
- Volume bar

The bottom panel shows:
- Current track name and artist
- Like status and shuffle state
- Playback progress bar with timestamps
- Active Spotify device name

---

## Tuning Constants

You can adjust these at the top of `gesture_controller.py` to suit your environment:

| Constant | Default | Description |
|----------|---------|-------------|
| `MODE_LOCK_FRAMES` | `12` | Frames to hold finger count before mode activates (~0.4s at 30fps) |
| `HOLD_FRAMES` | `10` | Frames a static gesture must be held in Mode 1 before firing |
| `SWIPE_WINDOW` | `20` | Palm history frames used to measure movement |
| `SWIPE_DIST_THRESH` | `0.09` | Normalised displacement to trigger a swipe (fraction of frame width) |
| `SWIPE_COOLDOWN` | `0.80` | Seconds between movement-triggered actions |
| `GESTURE_COOLDOWN` | `0.65` | Seconds between static gesture actions |
| `VOLUME_STEP` | `8` | Percentage volume change per swipe in Mode 2 |
| `SEEK_MS` | `10000` | Milliseconds to seek per gesture in Mode 3 |

---

## OS Fallback

If Spotify is not connected (no credentials set, or API error), the controller automatically falls back to **OS-level media keys** so basic play/pause/next/prev still works:

| Platform | Method |
|----------|--------|
| macOS | AppleScript (`osascript`) |
| Windows | Virtual key codes via `ctypes` |
| Linux | `xdotool` |

Volume fallback uses `osascript` (macOS), `pactl` (Linux), or `pycaw` (Windows).

---

## Troubleshooting

**Webcam not opening**
Try changing `VideoCapture(0)` to `VideoCapture(1)` in `main()` if you have multiple cameras.

**4-finger gesture not detected**
The debug line printed every second in the terminal shows the raw finger count and detected posture:
```
[Debug] fingers=4  posture=FOUR_FINGERS  mode=4
```
If `fingers=4` but `posture` is something else, the thumb may be counted — try tucking it more firmly.

**Spotify returns 403 on shuffle**
Shuffle requires Spotify Premium. The 403 error is expected on free accounts and is caught silently.

**Mode switches accidentally**
Increase `MODE_LOCK_FRAMES` (e.g. to `20`) to require a longer hold before a mode switch is confirmed.

**Swipe not registering**
Decrease `SWIPE_DIST_THRESH` (e.g. to `0.07`) if your movements are small, or increase `SWIPE_WINDOW` to give more time to complete the gesture.

---

## Project Structure

```
gesture-music-controller/
    gesture_controller.py   # Main script — everything in one file
    .spotify_cache          # Auto-generated after first login (gitignore this)
    README.md
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `opencv-python` | Webcam capture and overlay rendering |
| `mediapipe` | Real-time hand landmark detection |
| `spotipy` | Spotify Web API client |
| `numpy` | Palm centre calculations |
| `pycaw` *(Windows only)* | Native system volume control |

---

