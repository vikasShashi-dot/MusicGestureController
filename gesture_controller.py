"""
Hand Gesture Music Controller — with Spotify Integration
=========================================================
Controls Spotify playback using real-time hand gesture recognition
via MediaPipe Hands + OpenCV + Spotify Web API (spotipy).

Setup:
  1. Create a Spotify app at https://developer.spotify.com/dashboard
  2. Set Redirect URI to: http://localhost:8888/callback
  3. Copy your Client ID and Client Secret into the CONFIG block below
  4. pip install -r requirements.txt spotipy

Gestures:
  Open Palm (5 fingers)       → Play
  Closed Fist                  → Pause
  Swipe Right                  → Next Track
  Swipe Left                   → Previous Track
  Palm Height (relative)       → Volume Control
  Clockwise Wrist Rotation     → Seek Forward 5s
  Counter-Clockwise Rotation   → Seek Backward 5s
  Two-Hand Spread              → Shuffle Toggle
  Index + Middle fingers (V)   → Like / Save current track

Press 'q' to quit, 'h' to toggle help overlay.
"""

import cv2
import mediapipe as mp
import numpy as np
import time
import math
import platform
import subprocess
import threading
import os
from collections import deque

# ── Spotify ───────────────────────────────────────────────────────────────────
try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    SPOTIPY_AVAILABLE = True
except ImportError:
    SPOTIPY_AVAILABLE = False
    print("[WARN] spotipy not installed. Run: pip install spotipy")
    print("       Falling back to OS media keys only.")

# ═══════════════════════════════════════════════════════════════════════════════
# ██  SPOTIFY CONFIG — fill these in  ██████████████████████████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "#Client ID here#")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "#Client Secret here#")
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPE         = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-library-modify "
    "user-library-read "
    "playlist-read-private"
)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Optional OS-level audio libs ──────────────────────────────────────────────
SYSTEM = platform.system()

if SYSTEM == "Windows":
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices    = AudioUtilities.GetSpeakers()
        interface  = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume_ctrl = cast(interface, POINTER(IAudioEndpointVolume))
        PYCAW_AVAILABLE = True
    except Exception:
        PYCAW_AVAILABLE = False
        volume_ctrl = None
else:
    PYCAW_AVAILABLE = False
    volume_ctrl = None


# ── Constants ─────────────────────────────────────────────────────────────────
WINDOW_NAME = "Gesture Music Controller"
FRAME_W, FRAME_H = 1280, 720
FONT = cv2.FONT_HERSHEY_SIMPLEX

# BGR colors
C_SPOTIFY = (53, 208, 29)    # Spotify green
C_TEAL    = (200, 210, 20)
C_GOLD    = (22, 190, 255)
C_WHITE   = (255, 255, 255)
C_GRAY    = (160, 160, 160)
C_BLACK   = (0,   0,   0)
C_RED     = (80,  80, 230)
C_NAVY    = (30,  20,  10)
C_GREEN   = (80, 210,  80)
C_PINK    = (180, 100, 220)

GESTURE_COOLDOWN  = 0.7    # seconds between consecutive gesture actions
SWIPE_VEL_THRESH  = 0.25   # normalized units/frame
SWIPE_FRAMES      = 6      # frames to measure swipe velocity over
ROTATION_THRESH   = 30     # degrees delta for seek
HOLD_FRAMES       = 8      # frames gesture must be stable before firing
VOLUME_SMOOTH     = 0.15   # EMA alpha for volume
SPOTIFY_POLL_MS   = 3000   # ms between Spotify state refresh

# ── MediaPipe setup ───────────────────────────────────────────────────────────
mp_hands  = mp.solutions.hands
mp_draw   = mp.solutions.drawing_utils
mp_styles = mp.solutions.drawing_styles


# ══════════════════════════════════════════════════════════════════════════════
#  SPOTIFY CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class SpotifyClient:
    """
    Thread-safe wrapper around spotipy.  All Spotify calls are made from a
    background thread so they never block the video loop.
    """
    def __init__(self):
        self.sp       = None
        self.ready    = False
        self.error    = None

        # Cached playback state (updated by _poll_loop)
        self.track_name   = "---"
        self.artist_name  = "---"
        self.album_name   = "---"
        self.is_playing   = False
        self.volume_pct   = 50          # 0-100
        self.progress_ms  = 0
        self.duration_ms  = 1
        self.shuffle_on   = False
        self.is_liked     = False
        self.device_name  = "---"

        self._lock        = threading.Lock()
        self._cmd_queue   = deque()      # (method_name, args, kwargs)
        self._last_poll   = 0

        if SPOTIPY_AVAILABLE:
            t = threading.Thread(target=self._init_thread, daemon=True)
            t.start()
        else:
            self.error = "spotipy not installed"

    # ── Init in background so OAuth browser pop-up doesn't freeze camera ──────
    def _init_thread(self):
        try:
            auth = SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri=SPOTIFY_REDIRECT_URI,
                scope=SPOTIFY_SCOPE,
                open_browser=True,
                cache_path=".spotify_cache",
            )
            self.sp    = spotipy.Spotify(auth_manager=auth, requests_timeout=5)
            self.ready = True
            print("[Spotify] Connected ✓")
            self._poll_state()
            while True:
                self._process_queue()
                now = time.time() * 1000
                if now - self._last_poll > SPOTIFY_POLL_MS:
                    self._poll_state()
                    self._last_poll = now
                time.sleep(0.1)
        except Exception as e:
            self.error = str(e)
            print(f"[Spotify] Auth error: {e}")

    def _poll_state(self):
        if not self.sp:
            return
        try:
            pb = self.sp.current_playback()
            if pb and pb.get("item"):
                item = pb["item"]
                liked = False
                try:
                    res = self.sp.current_user_saved_tracks_contains([item["id"]])
                    liked = res[0] if res else False
                except Exception:
                    pass
                with self._lock:
                    self.track_name  = item["name"]
                    self.artist_name = ", ".join(a["name"] for a in item["artists"])
                    self.album_name  = item["album"]["name"]
                    self.is_playing  = pb.get("is_playing", False)
                    self.volume_pct  = pb.get("device", {}).get("volume_percent", 50) or 50
                    self.progress_ms = pb.get("progress_ms", 0) or 0
                    self.duration_ms = item.get("duration_ms", 1) or 1
                    self.shuffle_on  = pb.get("shuffle_state", False)
                    self.device_name = pb.get("device", {}).get("name", "---") or "---"
                    self.is_liked    = liked
        except Exception as e:
            print(f"[Spotify] Poll error: {e}")

    def _process_queue(self):
        while self._cmd_queue:
            try:
                fn, args, kwargs = self._cmd_queue.popleft()
                fn(*args, **kwargs)
                time.sleep(0.15)
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    # Rate limited — clear remaining queue so stale volume
                    # calls don't keep firing, then back off for 1 second
                    self._cmd_queue.clear()
                    print(f"[Spotify] Rate limited, clearing queue and backing off 1s")
                    time.sleep(1.0)
                else:
                    print(f"[Spotify] Command error: {e}")

    def _enqueue(self, fn, *args, **kwargs):
        self._cmd_queue.append((fn, args, kwargs))

    # ── Public control methods (non-blocking) ─────────────────────────────────
    def play(self):
        if self.sp:
            self._enqueue(self.sp.start_playback)

    def pause(self):
        if self.sp:
            self._enqueue(self.sp.pause_playback)

    def next_track(self):
        if self.sp:
            self._enqueue(self.sp.next_track)
            # Optimistically update UI
            with self._lock:
                self.track_name  = "Loading..."
                self.artist_name = ""
                self.is_playing  = True

    def prev_track(self):
        if self.sp:
            self._enqueue(self.sp.previous_track)
            with self._lock:
                self.track_name  = "Loading..."
                self.artist_name = ""

    def set_volume(self, pct: int):
        """pct: 0-100. Rate-limited: only sends if value changed enough and enough time has passed."""
        pct = max(0, min(100, pct))
        now = time.time()
        with self._lock:
            last_pct  = self.volume_pct
            last_sent = getattr(self, "_vol_last_sent", 0)
        # Only fire if: value changed by >=2 AND at least 300ms since last send
        if abs(pct - last_pct) >= 2 and (now - last_sent) >= 0.3:
            if self.sp:
                self._enqueue(self.sp.volume, pct)
            with self._lock:
                self.volume_pct = pct
                self._vol_last_sent = now

    def seek(self, offset_ms: int):
        """Seek relative to current position."""
        if self.sp:
            with self._lock:
                pos = self.progress_ms
                dur = self.duration_ms
            new_pos = max(0, min(pos + offset_ms, dur - 1000))
            self._enqueue(self.sp.seek_track, new_pos)

    def toggle_shuffle(self):
        if self.sp:
            with self._lock:
                new_state = not self.shuffle_on
                self.shuffle_on = new_state
            self._enqueue(self.sp.shuffle, new_state)

    def toggle_like(self):
        if self.sp:
            with self._lock:
                liked = self.is_liked
            if liked:
                self._enqueue(self._remove_like)
            else:
                self._enqueue(self._add_like)
            with self._lock:
                self.is_liked = not liked

    def _add_like(self):
        pb = self.sp.current_playback()
        if pb and pb.get("item"):
            self.sp.current_user_saved_tracks_add([pb["item"]["id"]])

    def _remove_like(self):
        pb = self.sp.current_playback()
        if pb and pb.get("item"):
            self.sp.current_user_saved_tracks_delete([pb["item"]["id"]])

    def get_state(self):
        with self._lock:
            return {
                "track":      self.track_name,
                "artist":     self.artist_name,
                "album":      self.album_name,
                "playing":    self.is_playing,
                "volume":     self.volume_pct,
                "progress":   self.progress_ms,
                "duration":   self.duration_ms,
                "shuffle":    self.shuffle_on,
                "liked":      self.is_liked,
                "device":     self.device_name,
            }


# ══════════════════════════════════════════════════════════════════════════════
#  OS FALLBACK (when Spotify is not available)
# ══════════════════════════════════════════════════════════════════════════════

def _os_media_key(action):
    """Send OS-level media key. Fallback when Spotify client unavailable."""
    print(f"[OS Media] {action}")
    if SYSTEM == "Windows":
        try:
            import ctypes
            VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1}
            key = VK.get(action)
            if key:
                ctypes.windll.user32.keybd_event(key, 0, 1, 0)
                ctypes.windll.user32.keybd_event(key, 0, 2, 0)
        except Exception as e:
            print(f"  [warn] {e}")
    elif SYSTEM == "Darwin":
        scripts = {
            "play_pause": 'tell application "Music" to playpause',
            "next":       'tell application "Music" to next track',
            "prev":       'tell application "Music" to previous track',
        }
        sc = scripts.get(action, "")
        if sc:
            subprocess.run(["osascript", "-e", sc], capture_output=True)
    elif SYSTEM == "Linux":
        keys = {"play_pause": "XF86AudioPlay", "next": "XF86AudioNext", "prev": "XF86AudioPrev"}
        k = keys.get(action)
        if k:
            subprocess.run(["xdotool", "key", k], capture_output=True)


def _os_set_volume(level_0_1):
    level = max(0.0, min(1.0, level_0_1))
    if SYSTEM == "Windows" and PYCAW_AVAILABLE and volume_ctrl:
        r = volume_ctrl.GetVolumeRange()
        volume_ctrl.SetMasterVolumeLevel(r[0] + level * (r[1] - r[0]), None)
    elif SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {int(level * 100)}"],
                       capture_output=True)
    elif SYSTEM == "Linux":
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(level * 100)}%"],
                       capture_output=True)


def _os_get_volume():
    if SYSTEM == "Windows" and PYCAW_AVAILABLE and volume_ctrl:
        r = volume_ctrl.GetVolumeRange()
        cur = volume_ctrl.GetMasterVolumeLevel()
        return (cur - r[0]) / (r[1] - r[0])
    elif SYSTEM == "Darwin":
        r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                           capture_output=True, text=True)
        try:
            return int(r.stdout.strip()) / 100
        except Exception:
            return 0.5
    elif SYSTEM == "Linux":
        r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                           capture_output=True, text=True)
        try:
            return int(r.stdout.split("/")[1].strip().rstrip("%")) / 100
        except Exception:
            return 0.5
    return 0.5


# ══════════════════════════════════════════════════════════════════════════════
#  HAND HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def finger_extended(lm, tip_id, pip_id):
    return lm[tip_id].y < lm[pip_id].y

def count_fingers(lm):
    fingers = 0
    if lm[4].x < lm[3].x:   # thumb
        fingers += 1
    for tip, pip in [(8, 6), (12, 10), (16, 14), (20, 18)]:
        if finger_extended(lm, tip, pip):
            fingers += 1
    return fingers

def wrist_roll_angle(lm):
    idx  = np.array([lm[5].x,  lm[5].y])
    pnky = np.array([lm[17].x, lm[17].y])
    vec  = pnky - idx
    return math.degrees(math.atan2(vec[1], vec[0]))

def palm_center(lm):
    pts = [lm[i] for i in [0, 5, 9, 13, 17]]
    return np.mean([p.x for p in pts]), np.mean([p.y for p in pts])

def is_victory_sign(lm):
    """Index + middle extended, ring + pinky + thumb curled."""
    index_up  = finger_extended(lm, 8,  6)
    middle_up = finger_extended(lm, 12, 10)
    ring_down = not finger_extended(lm, 16, 14)
    pinky_down = not finger_extended(lm, 20, 18)
    return index_up and middle_up and ring_down and pinky_down

def is_thumb_up(lm):
    """Only thumb extended upward, all other fingers curled."""
    thumb_up   = lm[4].y < lm[3].y < lm[2].y   # thumb tip above joints
    index_down = not finger_extended(lm, 8,  6)
    mid_down   = not finger_extended(lm, 12, 10)
    ring_down  = not finger_extended(lm, 16, 14)
    pinky_down = not finger_extended(lm, 20, 18)
    return thumb_up and index_down and mid_down and ring_down and pinky_down

def is_index_only(lm):
    """Only index finger extended (Mode 1 — swipe mode)."""
    index_up   = finger_extended(lm, 8,  6)
    mid_down   = not finger_extended(lm, 12, 10)
    ring_down  = not finger_extended(lm, 16, 14)
    pinky_down = not finger_extended(lm, 20, 18)
    thumb_down = lm[4].x > lm[3].x   # thumb roughly tucked (mirrored)
    return index_up and mid_down and ring_down and pinky_down

def is_index_middle_only(lm):
    """Index + middle extended, ring + pinky curled — but NOT a wide V (Mode 2 — seek mode).
    Distinguishes from VICTORY by keeping fingers close together."""
    index_up   = finger_extended(lm, 8,  6)
    middle_up  = finger_extended(lm, 12, 10)
    ring_down  = not finger_extended(lm, 16, 14)
    pinky_down = not finger_extended(lm, 20, 18)
    # Fingers close together (not a spread V-sign)
    finger_gap = abs(lm[8].x - lm[12].x)
    return index_up and middle_up and ring_down and pinky_down and finger_gap < 0.08


# ══════════════════════════════════════════════════════════════════════════════
#  GESTURE CONTROLLER
# ══════════════════════════════════════════════════════════════════════════════

class GestureController:
    def __init__(self, spotify: SpotifyClient):
        self.spotify          = spotify
        self.last_action_time = 0
        self.last_gesture     = None
        self.gesture_hold     = 0
        self.palm_history     = deque(maxlen=SWIPE_FRAMES + 2)
        self.angle_history    = deque(maxlen=10)
        self.volume_target    = 0.5
        self.current_volume   = 0.5
        self.volume_mode      = False   # True when THUMB_UP is held
        self.playing          = False
        self.action_log       = deque(maxlen=5)
        self.two_hand_spread_ref = None
        # Mode system:
        #   0 = Normal  (play/pause/volume/like/shuffle)
        #   1 = Swipe   (index finger only  → prev/next track)
        #   2 = Seek    (index+middle close → seek ±5s by wrist rotation)
        self.gesture_mode     = 0
        self.mode_hold        = 0       # frames mode gesture has been stable
        self._init_volume()

    def _init_volume(self):
        if self.spotify.ready:
            st = self.spotify.get_state()
            self.current_volume = st["volume"] / 100
        else:
            self.current_volume = _os_get_volume()
        self.volume_target = self.current_volume
        self.playing = self.spotify.get_state()["playing"] if self.spotify.ready else True

    def classify_single(self, lm):
        n  = count_fingers(lm)
        cx, cy = palm_center(lm)
        angle  = wrist_roll_angle(lm)
        self.palm_history.append((cx, cy))
        self.angle_history.append(angle)

        if n == 5:
            return "OPEN_PALM", cx, cy, angle
        if n == 0:
            return "FIST", cx, cy, angle
        if is_thumb_up(lm):
            return "THUMB_UP", cx, cy, angle
        if is_victory_sign(lm):
            return "VICTORY", cx, cy, angle

        # Mode-trigger postures (checked before swipe/rotation so they take priority)
        if is_index_only(lm):
            return "MODE1_INDEX", cx, cy, angle      # → Swipe mode
        if is_index_middle_only(lm):
            return "MODE2_TWO", cx, cy, angle        # → Seek mode

        # Swipe (only meaningful in Mode 1 — but classified here; gating is in process())
        if len(self.palm_history) >= SWIPE_FRAMES:
            old_x = self.palm_history[-SWIPE_FRAMES][0]
            vel_x = (cx - old_x) / SWIPE_FRAMES
            if vel_x >  SWIPE_VEL_THRESH:
                return "SWIPE_RIGHT", cx, cy, angle
            if vel_x < -SWIPE_VEL_THRESH:
                return "SWIPE_LEFT", cx, cy, angle

        # Rotation (only meaningful in Mode 2)
        if len(self.angle_history) >= 6:
            delta = angle - self.angle_history[-6]
            if delta >  ROTATION_THRESH:
                return "ROT_CW", cx, cy, angle
            if delta < -ROTATION_THRESH:
                return "ROT_CCW", cx, cy, angle

        return "UNKNOWN", cx, cy, angle

    def _log(self, msg):
        self.action_log.appendleft(f"{msg}  [{time.strftime('%H:%M:%S')}]")
        print(f"[Gesture] {msg}")

    def _do_action(self, gesture):
        sp = self.spotify
        if gesture == "FIST":
            if self.playing:
                sp.pause() if sp.ready else _os_media_key("play_pause")
                self.playing = False
                self._log("Pause")
                return "PAUSE"

        elif gesture == "OPEN_PALM":
            if not self.playing:
                sp.play() if sp.ready else _os_media_key("play_pause")
                self.playing = True
                self._log("Play")
                return "PLAY"

        elif gesture == "VICTORY":
            sp.toggle_like()
            liked = sp.get_state()["liked"]
            self._log("Liked <3" if liked else "Unliked")
            return "LIKE"

        return None

    def process(self, hands_lm):
        now         = time.time()
        on_cooldown = (now - self.last_action_time) < GESTURE_COOLDOWN
        action_fired = None

        if not hands_lm:
            self.gesture_hold = 0
            self.volume_mode  = False
            self.gesture_mode = 0
            self.mode_hold    = 0
            return None

        lm0 = hands_lm[0]
        gesture, cx, cy, angle = self.classify_single(lm0)

        # ── Mode switching ────────────────────────────────────────────────────
        # Holding MODE1_INDEX for a couple frames locks into Mode 1 (swipe).
        # Holding MODE2_TWO locks into Mode 2 (seek).
        # Any other gesture (or no hand) resets to Mode 0.
        if gesture == "MODE1_INDEX":
            self.mode_hold += 1
            if self.mode_hold >= 3:
                if self.gesture_mode != 1:
                    self.gesture_mode = 1
                    self._log("Mode 1: SWIPE (point finger)")
            # In Mode 1, look for swipe in palm history
            if self.gesture_mode == 1 and not on_cooldown:
                if len(self.palm_history) >= SWIPE_FRAMES:
                    old_x = self.palm_history[-SWIPE_FRAMES][0]
                    vel_x = (cx - old_x) / SWIPE_FRAMES
                    if vel_x > SWIPE_VEL_THRESH:
                        self.spotify.next_track() if self.spotify.ready else _os_media_key("next")
                        self._log("Next Track >>")
                        self.last_action_time = now
                        return "NEXT"
                    if vel_x < -SWIPE_VEL_THRESH:
                        self.spotify.prev_track() if self.spotify.ready else _os_media_key("prev")
                        self._log("Prev Track <<")
                        self.last_action_time = now
                        return "PREV"
            return None

        elif gesture == "MODE2_TWO":
            self.mode_hold += 1
            if self.mode_hold >= 3:
                if self.gesture_mode != 2:
                    self.gesture_mode = 2
                    self._log("Mode 2: SEEK (two fingers)")
            # In Mode 2, look for wrist rotation
            if self.gesture_mode == 2 and not on_cooldown:
                if len(self.angle_history) >= 6:
                    delta = angle - self.angle_history[-6]
                    if delta > ROTATION_THRESH:
                        self.spotify.seek(5000) if self.spotify.ready else None
                        self._log("Seek +5s >>")
                        self.last_action_time = now
                        return "SEEK_FWD"
                    if delta < -ROTATION_THRESH:
                        self.spotify.seek(-5000) if self.spotify.ready else None
                        self._log("Seek -5s <<")
                        self.last_action_time = now
                        return "SEEK_BWD"
            return None

        else:
            # Any non-mode gesture resets back to Mode 0
            if gesture not in ("SWIPE_RIGHT", "SWIPE_LEFT", "ROT_CW", "ROT_CCW"):
                if self.gesture_mode != 0:
                    self.gesture_mode = 0
                self.mode_hold = 0

        # ── Volume mode (Mode 0 only) ─────────────────────────────────────────
        if gesture == "THUMB_UP":
            self.volume_mode = True
            thumb_y = lm0[4].y               # 0=top of frame, 1=bottom
            vol_from_thumb = 1.0 - thumb_y   # invert so raising thumb = louder
            self.volume_target = (
                (1 - VOLUME_SMOOTH) * self.volume_target
                + VOLUME_SMOOTH * vol_from_thumb
            )
            self.current_volume = self.volume_target
            if self.spotify.ready:
                self.spotify.set_volume(int(self.current_volume * 100))
            else:
                _os_set_volume(self.current_volume)
            return "VOL"
        else:
            self.volume_mode = False

        # ── Two-hand spread → shuffle (Mode 0 only) ──────────────────────────
        if len(hands_lm) == 2:
            c0 = palm_center(lm0)
            c1 = palm_center(hands_lm[1])
            dist = math.dist(c0, c1)
            if self.two_hand_spread_ref is None:
                self.two_hand_spread_ref = dist
            else:
                spread = dist - self.two_hand_spread_ref
                if spread > 0.25 and not on_cooldown:
                    self.spotify.toggle_shuffle() if self.spotify.ready else None
                    st = self.spotify.get_state()
                    state_str = "ON" if st["shuffle"] else "OFF"
                    self._log(f"Shuffle {state_str}")
                    self.two_hand_spread_ref = dist
                    self.last_action_time = now
                    return "SHUFFLE"
            return None
        else:
            self.two_hand_spread_ref = None

        # ── Mode 0 standard gestures ──────────────────────────────────────────
        if gesture == self.last_gesture:
            self.gesture_hold += 1
        else:
            self.gesture_hold = 0
            self.last_gesture = gesture

        if self.gesture_hold < HOLD_FRAMES or on_cooldown:
            return None

        action_fired = self._do_action(gesture)
        if action_fired:
            self.last_action_time = now
            self.gesture_hold = 0

        return action_fired


# ══════════════════════════════════════════════════════════════════════════════
#  OVERLAY DRAWING
# ══════════════════════════════════════════════════════════════════════════════

def _text_bg(img, text, pos, scale=0.6, thick=1, color=C_WHITE, bg=C_NAVY, pad=5):
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thick)
    x, y = pos
    cv2.rectangle(img, (x - pad, y - th - pad), (x + tw + pad, y + base + pad), bg, -1)
    cv2.putText(img, text, (x, y), FONT, scale, color, thick, cv2.LINE_AA)


def _progress_bar(frame, x, y, w, h, pct, fg, bg=(60, 60, 60)):
    cv2.rectangle(frame, (x, y), (x + w, y + h), bg, -1)
    filled = int(w * max(0.0, min(1.0, pct)))
    if filled > 0:
        cv2.rectangle(frame, (x, y), (x + filled, y + h), fg, -1)


def _fmt_ms(ms):
    s  = ms // 1000
    m  = s // 60
    return f"{m}:{s % 60:02d}"


_last_fps_t = [time.time()]


def draw_overlay(frame, controller: GestureController, gesture, action, show_help):
    h, w = frame.shape[:2]
    sp   = controller.spotify
    st   = sp.get_state()

    # ── Left sidebar ──────────────────────────────────────────────────────────
    sidebar = frame.copy()
    cv2.rectangle(sidebar, (0, 0), (290, h), (10, 18, 30), -1)
    cv2.addWeighted(sidebar, 0.75, frame, 0.25, 0, frame)

    # Title
    cv2.putText(frame, "GESTURE MUSIC", (10, 32), FONT, 0.65, C_SPOTIFY, 2, cv2.LINE_AA)
    cv2.putText(frame, "CONTROLLER",    (10, 54), FONT, 0.65, C_SPOTIFY, 2, cv2.LINE_AA)
    cv2.line(frame, (10, 62), (278, 62), C_SPOTIFY, 1)

    # Volume bar (vertical, left side)
    vol      = controller.current_volume
    bx, by, bh = 14, 80, 180
    cv2.rectangle(frame, (bx, by), (bx + 16, by + bh), (50, 50, 50), -1)
    filled = int(bh * vol)
    cv2.rectangle(frame, (bx, by + bh - filled), (bx + 16, by + bh), C_SPOTIFY, -1)
    cv2.putText(frame, "VOL", (bx, by + bh + 16), FONT, 0.40, C_GRAY,  1)
    cv2.putText(frame, f"{int(vol * 100)}%", (bx, by + bh + 32), FONT, 0.46, C_WHITE, 1)

    # Gesture + playback state
    g_color = C_SPOTIFY if gesture not in ("UNKNOWN", None) else C_GRAY
    cv2.putText(frame, "Gesture:", (42, 96), FONT, 0.46, C_GRAY, 1)
    cv2.putText(frame, str(gesture or "-"), (42, 116), FONT, 0.52, g_color, 1, cv2.LINE_AA)

    state_str = "[PLAY] Playing" if controller.playing else "[PAUSE] Paused"
    cv2.putText(frame, state_str, (42, 140), FONT, 0.48,
                C_GREEN if controller.playing else C_GOLD, 1)

    # Volume mode indicator (shown when THUMB_UP active)
    if controller.volume_mode:
        cv2.putText(frame, "VOL MODE", (42, 162), FONT, 0.46, C_SPOTIFY, 1)

    # Mode indicator
    mode_labels = {0: "MODE 0: Normal", 1: "MODE 1: Swipe", 2: "MODE 2: Seek"}
    mode_colors = {0: C_GRAY, 1: C_GOLD, 2: C_TEAL}
    m = controller.gesture_mode
    cv2.putText(frame, mode_labels[m], (42, 182), FONT, 0.44, mode_colors[m], 1)

    # Action flash
    if action and action != "VOL":
        _text_bg(frame, f"> {action}", (42, 202), scale=0.62, color=C_GOLD, bg=(30,30,10), pad=6)

    # Action log
    cv2.putText(frame, "Log:", (10, 224), FONT, 0.42, C_GRAY, 1)
    for i, entry in enumerate(controller.action_log):
        cv2.putText(frame, entry, (10, 242 + i * 18), FONT, 0.36, C_WHITE, 1)

    # FPS
    now   = time.time()
    fps   = int(1 / max(0.001, now - _last_fps_t[0]))
    _last_fps_t[0] = now
    cv2.putText(frame, f"FPS {fps}", (10, h - 12), FONT, 0.42, C_GRAY, 1)

    # ── Spotify Now Playing panel (bottom of frame) ───────────────────────────
    panel_y = h - 90
    np_overlay = frame.copy()
    cv2.rectangle(np_overlay, (290, panel_y), (w, h), (8, 15, 25), -1)
    cv2.addWeighted(np_overlay, 0.80, frame, 0.20, 0, frame)
    cv2.line(frame, (290, panel_y), (w, panel_y), C_SPOTIFY, 1)

    if sp.ready:
        # Track info
        track_txt  = st["track"][:45] + ("..." if len(st["track"]) > 45 else "")
        artist_txt = st["artist"][:50] + ("..." if len(st["artist"]) > 50 else "")
        cv2.putText(frame, track_txt,  (300, panel_y + 22), FONT, 0.58, C_WHITE,   1, cv2.LINE_AA)
        cv2.putText(frame, artist_txt, (300, panel_y + 42), FONT, 0.46, C_GRAY,    1, cv2.LINE_AA)

        # Liked heart
        heart = "[Liked]" if st["liked"] else "[Like]"
        heart_color = C_PINK if st["liked"] else C_GRAY
        cv2.putText(frame, heart, (w - 130, panel_y + 22), FONT, 0.46, heart_color, 1)

        # Shuffle indicator
        if st["shuffle"]:
            cv2.putText(frame, "SHUFFLE ON", (w - 130, panel_y + 42), FONT, 0.4, C_SPOTIFY, 1)

        # Progress bar + timestamps
        dur = st["duration"]
        prog = st["progress"] / dur if dur > 0 else 0
        bar_x, bar_w = 300, w - 310
        _progress_bar(frame, bar_x, panel_y + 56, bar_w, 6, prog, C_SPOTIFY)
        cv2.putText(frame, _fmt_ms(st["progress"]), (bar_x, panel_y + 78),
                    FONT, 0.40, C_GRAY, 1)
        dur_txt = _fmt_ms(dur)
        (dw, _), _ = cv2.getTextSize(dur_txt, FONT, 0.40, 1)
        cv2.putText(frame, dur_txt, (bar_x + bar_w - dw, panel_y + 78),
                    FONT, 0.40, C_GRAY, 1)

        # Device
        cv2.putText(frame, f"Device: {st['device']}", (bar_x, h - 8),
                    FONT, 0.36, C_GRAY, 1)

        # Spotify logo text
        cv2.putText(frame, "spotify", (w - 75, h - 8), FONT, 0.52, C_SPOTIFY, 1)

    else:
        err = sp.error or "Connecting..."
        cv2.putText(frame, f"Spotify: {err}", (300, panel_y + 35), FONT, 0.50, C_GOLD, 1)
        cv2.putText(frame, "OS media keys active", (300, panel_y + 58), FONT, 0.44, C_GRAY, 1)

    # ── Help overlay ─────────────────────────────────────────────────────────
    if show_help:
        help_lines = [
            ("GESTURE GUIDE", C_SPOTIFY),
            ("-----------------------", C_GRAY),
            ("── MODE 0  (default) ──", C_GRAY),
            ("Open Palm    ->  Play", C_WHITE),
            ("Closed Fist  ->  Pause", C_WHITE),
            ("Thumb Up     ->  Volume (move up/down)", C_SPOTIFY),
            ("V-Sign       ->  Like", C_PINK),
            ("Two Hands    ->  Shuffle", C_WHITE),
            ("── MODE 1  (☝ index) ──", C_GOLD),
            ("Point finger ->  Enter mode 1", C_GOLD),
            ("Swipe Right  ->  Next Track", C_GOLD),
            ("Swipe Left   ->  Prev Track", C_GOLD),
            ("── MODE 2  (✌ two close) ──", C_TEAL),
            ("Two fingers  ->  Enter mode 2", C_TEAL),
            ("Rotate CW    ->  Seek +5s", C_TEAL),
            ("Rotate CCW   ->  Seek -5s", C_TEAL),
            ("-----------------------", C_GRAY),
            ("h  ->  hide this panel", C_GRAY),
            ("q  ->  quit", C_GRAY),
        ]
        box_w = 290
        box_h = len(help_lines) * 22 + 16
        ox = w - box_w - 12
        oy = 12
        cv2.rectangle(frame, (ox - 8, oy - 8), (ox + box_w, oy + box_h), (18, 18, 18), -1)
        cv2.rectangle(frame, (ox - 8, oy - 8), (ox + box_w, oy + box_h), C_SPOTIFY, 1)
        for i, (line, color) in enumerate(help_lines):
            cv2.putText(frame, line, (ox, oy + i * 22 + 16), FONT, 0.42, color, 1)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  Hand Gesture Music Controller  -- Spotify Edition")
    print("=" * 60)

    creds_set = (
        SPOTIFY_CLIENT_ID     != "YOUR_CLIENT_ID_HERE" and
        SPOTIFY_CLIENT_SECRET != "YOUR_CLIENT_SECRET_HERE"
    )
    if not creds_set:
        print()
        print("  [!] Spotify credentials not configured.")
        print("      To enable Spotify control, do ONE of the following:")
        print()
        print("  Option A: Edit this file directly")
        print("    Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET")
        print("    near the top of gesture_controller.py")
        print()
        print("  Option B: Use environment variables (recommended)")
        print("    export SPOTIFY_CLIENT_ID=your_id_here")
        print("    export SPOTIFY_CLIENT_SECRET=your_secret_here")
        print("    python gesture_controller.py")
        print()
        print("  Get credentials at: https://developer.spotify.com/dashboard")
        print("  Set redirect URI to: http://127.0.0.1:8888/callback")
        print()
        print("  Running in OS media key fallback mode for now.")
        print("=" * 60)
    else:
        print()
        print("  Spotify credentials found. Opening browser for login...")
        print("  (This only happens once -- token is cached after.)")
        print("=" * 60)

    # Start Spotify client (auth may open browser for first-time login)
    spotify = SpotifyClient()

    controller = GestureController(spotify)

    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)

    if not cap.isOpened():
        print("[ERROR] Cannot open webcam (index 0). Try changing VideoCapture(0) → (1).")
        return

    show_help        = True
    last_action      = None
    action_clear_t   = 0

    with mp_hands.Hands(
        model_complexity=0,       # 0 = lite model, ~2x faster, good enough for gestures
        max_num_hands=2,
        min_detection_confidence=0.70,
        min_tracking_confidence=0.60,
    ) as hands:

        print("[Camera] Running. Press 'h' for help, 'q' to quit.")

        while True:
            ret, frame = cap.read()
            if not ret:
                print("[ERROR] Webcam read failed.")
                break

            frame  = cv2.flip(frame, 1)
            rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)

            hands_lm = []
            if result.multi_hand_landmarks:
                for hlm in result.multi_hand_landmarks:
                    mp_draw.draw_landmarks(
                        frame, hlm,
                        mp_hands.HAND_CONNECTIONS,
                        mp_styles.get_default_hand_landmarks_style(),
                        mp_styles.get_default_hand_connections_style(),
                    )
                    hands_lm.append(hlm.landmark)

            gesture = None
            if hands_lm:
                gesture, *_ = controller.classify_single(hands_lm[0])

            action = controller.process(hands_lm)
            if action:
                last_action    = action
                action_clear_t = time.time() + 1.8

            if time.time() > action_clear_t:
                last_action = None

            draw_overlay(frame, controller, gesture, last_action, show_help)
            cv2.imshow(WINDOW_NAME, frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('h'):
                show_help = not show_help

    cap.release()
    cv2.destroyAllWindows()
    print("[Done] Goodbye.")


if __name__ == "__main__":
    main()
