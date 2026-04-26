"""
Hand Gesture Music Controller - Spotify Edition
================================================
Controls Spotify using real-time hand gesture recognition via
MediaPipe Hands + OpenCV + Spotify Web API (spotipy).

MODES  (switch by holding the posture for ~0.4s)
-------------------------------------------------
  MODE 1 - Play / Pause          [1 finger: index only]
      Open Palm (5) -> Play
      Closed Fist  (0) -> Pause
      Thumb Up  ->  Like / Unlike current track

  MODE 2 - Volume                [2 fingers: index + middle]
      Move hand UP   -> Volume Up
      Move hand DOWN -> Volume Down

  MODE 3 - Seek                  [3 fingers: index + middle + ring]
      Move hand RIGHT -> Seek +10s
      Move hand LEFT  -> Seek -10s

  MODE 4 - Change Song           [4 fingers: all except thumb]
      Move hand RIGHT -> Next Track
      Move hand LEFT  -> Previous Track

  ANY MODE
      Two-hand spread -> Shuffle toggle

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

# -- Spotify -------------------------------------------------------------------
try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    SPOTIPY_AVAILABLE = True
except ImportError:
    SPOTIPY_AVAILABLE = False
    print("[WARN] spotipy not installed. Run: pip install spotipy")

# ===============================================================================
# ##  SPOTIFY CONFIG
# ===============================================================================
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPE         = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-library-modify "
    "user-library-read "
    "playlist-read-private"
)
# ===============================================================================

SYSTEM = platform.system()

if SYSTEM == "Windows":
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices     = AudioUtilities.GetSpeakers()
        interface   = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume_ctrl = cast(interface, POINTER(IAudioEndpointVolume))
        PYCAW_AVAILABLE = True
    except Exception:
        PYCAW_AVAILABLE = False
        volume_ctrl = None
else:
    PYCAW_AVAILABLE = False
    volume_ctrl = None


# -- Constants -----------------------------------------------------------------
WINDOW_NAME = "Gesture Music Controller"
FRAME_W, FRAME_H = 1280, 720
FONT = cv2.FONT_HERSHEY_SIMPLEX

# BGR colors
C_SPOTIFY = (53, 208, 29)
C_TEAL    = (200, 210, 20)
C_GOLD    = (22, 190, 255)
C_WHITE   = (255, 255, 255)
C_GRAY    = (160, 160, 160)
C_BLACK   = (0,   0,   0)
C_RED     = (80,  80, 230)
C_NAVY    = (30,  20,  10)
C_GREEN   = (80, 210,  80)
C_PINK    = (180, 100, 220)
C_ORANGE  = (0,  165, 255)

MODE_COLORS = {0: C_GRAY, 1: C_SPOTIFY, 2: C_GOLD, 3: C_TEAL, 4: C_ORANGE}
MODE_LABELS = {
    0: "No Hand",
    1: "MODE 1: Play / Pause",
    2: "MODE 2: Volume",
    3: "MODE 3: Seek",
    4: "MODE 4: Change Song",
}

# Tuning
GESTURE_COOLDOWN  = 0.65
MODE_LOCK_FRAMES  = 12      # frames (~0.4s @30fps) to hold before mode activates
HOLD_FRAMES       = 10      # frames static gesture must be held in mode 1
SWIPE_WINDOW      = 20      # palm history frames for movement detection
SWIPE_DIST_THRESH = 0.09    # normalised displacement to trigger swipe
SWIPE_COOLDOWN    = 0.80    # s between movement-triggered actions
VOLUME_STEP       = 8       # % volume per swipe
SEEK_MS           = 10000   # ms per seek gesture
SPOTIFY_POLL_MS   = 3000

# -- MediaPipe -----------------------------------------------------------------
mp_hands  = mp.solutions.hands
mp_draw   = mp.solutions.drawing_utils
mp_styles = mp.solutions.drawing_styles


# ==============================================================================
#  SPOTIFY CLIENT
# ==============================================================================

class SpotifyClient:
    """Thread-safe spotipy wrapper. All API calls run in a background thread."""

    def __init__(self):
        self.sp          = None
        self.ready       = False
        self.error       = None
        self.track_name  = "---"
        self.artist_name = "---"
        self.album_name  = "---"
        self.is_playing  = False
        self.volume_pct  = 50
        self.progress_ms = 0
        self.duration_ms = 1
        self.shuffle_on  = False
        self.is_liked    = False
        self.device_name = "---"
        self._lock       = threading.Lock()
        self._cmd_queue  = deque()
        self._last_poll  = 0

        if SPOTIPY_AVAILABLE:
            threading.Thread(target=self._init_thread, daemon=True).start()
        else:
            self.error = "spotipy not installed"

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
            print("[Spotify] Connected OK")
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
                item  = pb["item"]
                liked = False
                try:
                    res   = self.sp.current_user_saved_tracks_contains([item["id"]])
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
                if "429" in str(e):
                    self._cmd_queue.clear()
                    print("[Spotify] Rate limited - clearing queue, backing off 1s")
                    time.sleep(1.0)
                else:
                    print(f"[Spotify] Command error: {e}")

    def _enqueue(self, fn, *args, **kwargs):
        self._cmd_queue.append((fn, args, kwargs))

    # -- Public API (non-blocking) ---------------------------------------------
    def play(self):
        if self.sp: self._enqueue(self.sp.start_playback)

    def pause(self):
        if self.sp: self._enqueue(self.sp.pause_playback)

    def next_track(self):
        if self.sp:
            self._enqueue(self.sp.next_track)
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
        pct = max(0, min(100, pct))
        now = time.time()
        with self._lock:
            last_pct  = self.volume_pct
            last_sent = getattr(self, "_vol_last_sent", 0)
        if abs(pct - last_pct) >= 2 and (now - last_sent) >= 0.3:
            if self.sp: self._enqueue(self.sp.volume, pct)
            with self._lock:
                self.volume_pct     = pct
                self._vol_last_sent = now

    def seek(self, offset_ms: int):
        if self.sp:
            with self._lock:
                pos = self.progress_ms
                dur = self.duration_ms
            new_pos = max(0, min(pos + offset_ms, dur - 1000))
            self._enqueue(self.sp.seek_track, new_pos)

    def toggle_shuffle(self):
        if self.sp:
            with self._lock:
                new_state       = not self.shuffle_on
                self.shuffle_on = new_state
            self._enqueue(self.sp.shuffle, new_state)

    def toggle_like(self):
        if self.sp:
            with self._lock:
                liked = self.is_liked
            self._enqueue(self._remove_like if liked else self._add_like)
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
                "track":    self.track_name,
                "artist":   self.artist_name,
                "album":    self.album_name,
                "playing":  self.is_playing,
                "volume":   self.volume_pct,
                "progress": self.progress_ms,
                "duration": self.duration_ms,
                "shuffle":  self.shuffle_on,
                "liked":    self.is_liked,
                "device":   self.device_name,
            }


# ==============================================================================
#  OS FALLBACK
# ==============================================================================

def _os_media_key(action):
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
        if sc: subprocess.run(["osascript", "-e", sc], capture_output=True)
    elif SYSTEM == "Linux":
        keys = {"play_pause": "XF86AudioPlay", "next": "XF86AudioNext", "prev": "XF86AudioPrev"}
        k = keys.get(action)
        if k: subprocess.run(["xdotool", "key", k], capture_output=True)


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
        r   = volume_ctrl.GetVolumeRange()
        cur = volume_ctrl.GetMasterVolumeLevel()
        return (cur - r[0]) / (r[1] - r[0])
    elif SYSTEM == "Darwin":
        r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                           capture_output=True, text=True)
        try:    return int(r.stdout.strip()) / 100
        except: return 0.5
    elif SYSTEM == "Linux":
        r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                           capture_output=True, text=True)
        try:    return int(r.stdout.split("/")[1].strip().rstrip("%")) / 100
        except: return 0.5
    return 0.5


# ==============================================================================
#  HAND HELPERS
# ==============================================================================

def finger_extended(lm, tip_id, pip_id):
    """True when fingertip is above its PIP joint (lower y = higher on screen)."""
    return lm[tip_id].y < lm[pip_id].y

def count_fingers(lm):
    fingers = 0
    if lm[4].x < lm[3].x:   # thumb (mirrored)
        fingers += 1
    for tip, pip in [(8, 6), (12, 10), (16, 14), (20, 18)]:
        if finger_extended(lm, tip, pip):
            fingers += 1
    return fingers

def palm_center(lm):
    pts = [lm[i] for i in [0, 5, 9, 13, 17]]
    return np.mean([p.x for p in pts]), np.mean([p.y for p in pts])


# -- Posture detectors ---------------------------------------------------------

def is_open_palm(lm):
    return count_fingers(lm) == 5

def is_fist(lm):
    return count_fingers(lm) == 0

def is_thumb_up(lm):
    """Thumb pointing up, all other fingers curled."""
    thumb_up   = lm[4].y < lm[3].y < lm[2].y
    index_down = not finger_extended(lm, 8,  6)
    mid_down   = not finger_extended(lm, 12, 10)
    ring_down  = not finger_extended(lm, 16, 14)
    pinky_down = not finger_extended(lm, 20, 18)
    return thumb_up and index_down and mid_down and ring_down and pinky_down

def is_one_finger(lm):
    """Only index finger up."""
    return (finger_extended(lm, 8, 6)
            and not finger_extended(lm, 12, 10)
            and not finger_extended(lm, 16, 14)
            and not finger_extended(lm, 20, 18))

def is_two_fingers(lm):
    """Index + middle up, ring + pinky down."""
    return (finger_extended(lm, 8, 6)
            and finger_extended(lm, 12, 10)
            and not finger_extended(lm, 16, 14)
            and not finger_extended(lm, 20, 18))

def is_three_fingers(lm):
    """Index + middle + ring up, pinky down."""
    return (finger_extended(lm, 8, 6)
            and finger_extended(lm, 12, 10)
            and finger_extended(lm, 16, 14)
            and not finger_extended(lm, 20, 18))

def is_four_fingers(lm):
    """
    All four fingers (index/middle/ring/pinky) clearly extended.
    Thumb state is intentionally ignored - it's unreliable on a mirrored
    feed and MediaPipe often mis-classifies it when fingers are spread.
    We rely on is_open_palm being checked first (5-finger) to separate the
    two cases - if we land here, thumb is not contributing a 5th finger.
    """
    return (finger_extended(lm, 8,  6)   # index
            and finger_extended(lm, 12, 10)  # middle
            and finger_extended(lm, 16, 14)  # ring
            and finger_extended(lm, 20, 18)) # pinky

def detect_posture(lm):
    """Classify current hand posture. Priority order matters."""
    # Thumb-up must come before four-finger so a tucked-thumb hand
    # with all four fingers up is caught correctly.
    if is_fist(lm):          return "FIST"
    if is_thumb_up(lm):      return "THUMB_UP"
    if is_open_palm(lm):     return "OPEN_PALM"    # 5 fingers (thumb included)
    if is_four_fingers(lm):  return "FOUR_FINGERS"  # 4 fingers, thumb tucked/side
    if is_three_fingers(lm): return "THREE_FINGERS"
    if is_two_fingers(lm):   return "TWO_FINGERS"
    if is_one_finger(lm):    return "ONE_FINGER"
    return "UNKNOWN"


# ==============================================================================
#  MOVEMENT DETECTOR
#  Compares palm position now vs SWIPE_WINDOW frames ago.
#  Far more reliable than per-frame velocity - captures deliberate movements.
# ==============================================================================

class MovementDetector:
    def __init__(self, window=SWIPE_WINDOW):
        self.history = deque(maxlen=window + 2)
        self.window  = window

    def update(self, cx, cy):
        self.history.append((cx, cy))

    def detect(self, threshold=SWIPE_DIST_THRESH):
        """Return 'LEFT'|'RIGHT'|'UP'|'DOWN' or None."""
        if len(self.history) < self.window:
            return None
        ox, oy = self.history[0]
        cx, cy = self.history[-1]
        dx = cx - ox
        dy = cy - oy
        if abs(dx) >= abs(dy):
            if dx >  threshold: return "RIGHT"
            if dx < -threshold: return "LEFT"
        else:
            if dy >  threshold: return "DOWN"
            if dy < -threshold: return "UP"
        return None

    def reset(self):
        self.history.clear()


# ==============================================================================
#  GESTURE CONTROLLER
# ==============================================================================

class GestureController:
    """
    Manages mode switching and routes gestures to action functions.

    Modes
    -----
      0  idle / no hand
      1  Play / Pause / Like    (1 finger held to enter)
      2  Volume up / down       (2 fingers held to enter)
      3  Seek fwd / bwd         (3 fingers held to enter)
      4  Next / Prev track      (4 fingers held to enter)
    """

    def __init__(self, spotify: SpotifyClient):
        self.spotify = spotify

        # Playback mirrors
        self.playing        = False
        self.current_volume = 0.5
        self.volume_target  = 0.5

        # Mode FSM
        self.gesture_mode     = 0
        self.mode_candidate   = 0
        self.mode_hold_frames = 0

        # Static gesture debounce (mode 1)
        self.last_posture = None
        self.posture_hold = 0

        # Movement
        self.mover = MovementDetector()

        # Cooldowns
        self.last_action_time = 0
        self.last_swipe_time  = 0

        # Two-hand shuffle
        self.two_hand_spread_ref = None

        # Log
        self.action_log = deque(maxlen=5)

        self._init_volume()

    # -- Init ------------------------------------------------------------------

    def _init_volume(self):
        if self.spotify.ready:
            self.current_volume = self.spotify.get_state()["volume"] / 100
        else:
            self.current_volume = _os_get_volume()
        self.volume_target = self.current_volume
        self.playing = self.spotify.get_state()["playing"] if self.spotify.ready else False

    def _log(self, msg):
        # Store (message, timestamp) so overlay can show relative "Xs ago"
        self.action_log.appendleft((msg, time.time()))
        print(f"[Gesture] {msg}")

    # ==========================================================================
    #  ACTION FUNCTIONS - one per logical action
    # ==========================================================================

    def action_play(self):
        if not self.playing:
            sp = self.spotify
            sp.play() if sp.ready else _os_media_key("play_pause")
            self.playing = True
            self._log("Play")
            return "PLAY"
        return None

    def action_pause(self):
        if self.playing:
            sp = self.spotify
            sp.pause() if sp.ready else _os_media_key("play_pause")
            self.playing = False
            self._log("Pause")
            return "PAUSE"
        return None

    def action_like(self):
        sp = self.spotify
        sp.toggle_like()
        liked = sp.get_state()["liked"]
        label = "Liked!" if liked else "Unliked"
        self._log(label)
        return "LIKE" if liked else "UNLIKE"

    def action_volume_up(self):
        new_vol = min(100, int(self.current_volume * 100) + VOLUME_STEP)
        self.current_volume = new_vol / 100
        self.volume_target  = self.current_volume
        sp = self.spotify
        sp.set_volume(new_vol) if sp.ready else _os_set_volume(self.current_volume)
        self._log(f"Vol Up -> {new_vol}%")
        return "VOL_UP"

    def action_volume_down(self):
        new_vol = max(0, int(self.current_volume * 100) - VOLUME_STEP)
        self.current_volume = new_vol / 100
        self.volume_target  = self.current_volume
        sp = self.spotify
        sp.set_volume(new_vol) if sp.ready else _os_set_volume(self.current_volume)
        self._log(f"Vol Down -> {new_vol}%")
        return "VOL_DOWN"

    def action_seek_forward(self):
        self.spotify.seek(SEEK_MS) if self.spotify.ready else None
        self._log(f"Seek +{SEEK_MS // 1000}s")
        return "SEEK_FWD"

    def action_seek_backward(self):
        self.spotify.seek(-SEEK_MS) if self.spotify.ready else None
        self._log(f"Seek -{SEEK_MS // 1000}s")
        return "SEEK_BWD"

    def action_next_track(self):
        sp = self.spotify
        sp.next_track() if sp.ready else _os_media_key("next")
        self._log("Next Track")
        return "NEXT"

    def action_prev_track(self):
        sp = self.spotify
        sp.prev_track() if sp.ready else _os_media_key("prev")
        self._log("Prev Track")
        return "PREV"

    def action_shuffle(self):
        sp = self.spotify
        if sp.ready:
            sp.toggle_shuffle()
            on = sp.get_state()["shuffle"]
            self._log(f"Shuffle {'ON' if on else 'OFF'}")
            return "SHUFFLE"
        return None

    # ==========================================================================
    #  CLASSIFY (used by overlay to label current posture)
    # ==========================================================================

    def classify_single(self, lm):
        posture = detect_posture(lm)
        cx, cy  = palm_center(lm)
        # Debug: print raw finger count + posture every ~30 frames so you can
        # confirm detection without flooding the terminal.
        if not hasattr(self, '_dbg_frame'): self._dbg_frame = 0
        self._dbg_frame += 1
        if self._dbg_frame % 30 == 0:
            n = count_fingers(lm)
            print(f"[Debug] fingers={n}  posture={posture}  mode={self.gesture_mode}")
        return posture, cx, cy, 0.0

    # ==========================================================================
    #  MAIN PROCESS  called every frame
    # ==========================================================================

    def process(self, hands_lm):
        now = time.time()

        if not hands_lm:
            self._reset_state()
            # Mode is intentionally preserved - user may have briefly lowered hand
            return None

        lm0     = hands_lm[0]
        posture = detect_posture(lm0)
        cx, cy  = palm_center(lm0)
        self.mover.update(cx, cy)

        on_cooldown       = (now - self.last_action_time) < GESTURE_COOLDOWN
        swipe_on_cooldown = (now - self.last_swipe_time)  < SWIPE_COOLDOWN

        # Two-hand shuffle (any mode)
        result = self._check_shuffle(hands_lm, lm0, now, on_cooldown)
        if result:
            return result

        # Update mode from current posture
        self._update_mode(posture, now)

        # Route to active mode handler
        mode = self.gesture_mode
        if mode == 1:
            return self._mode1_play_pause(posture, now, on_cooldown)
        if mode == 2:
            return self._mode2_volume(now, swipe_on_cooldown)
        if mode == 3:
            return self._mode3_seek(now, swipe_on_cooldown)
        if mode == 4:
            return self._mode4_change_song(now, swipe_on_cooldown)

        return None

    # -- State reset -----------------------------------------------------------

    def _reset_state(self):
        # Called when no hand is visible.
        # IMPORTANT: We intentionally do NOT reset gesture_mode here.
        # The mode stays locked until the user deliberately enters a new one.
        # Only transient per-frame state is cleared.
        self.mode_candidate   = 0
        self.mode_hold_frames = 0
        self.posture_hold     = 0
        self.last_posture     = None
        self.two_hand_spread_ref = None
        # Do NOT reset self.mover - keeps history so a swipe right before
        # the hand briefly disappears still registers.

    # -- Mode FSM --------------------------------------------------------------

    def _posture_to_mode(self, posture):
        return {
            "ONE_FINGER":    1,
            "TWO_FINGERS":   2,
            "THREE_FINGERS": 3,
            "FOUR_FINGERS":  4,
            # These static gestures all live inside mode 1
            "FIST":          1,
            "OPEN_PALM":     1,
            "THUMB_UP":      1,
        }.get(posture, 0)

    def _update_mode(self, posture, now):
        target = self._posture_to_mode(posture)

        if target == 0:
            # UNKNOWN or unmapped posture: freeze everything.
            # Do NOT touch gesture_mode - stay in current mode.
            # Reset the candidate counter so a single bad frame
            # doesn't accumulate toward an accidental mode switch.
            self.mode_candidate   = 0
            self.mode_hold_frames = 0
            return

        # Already in this mode - just keep counting the candidate so the
        # progress bar stays full, but don't re-announce.
        if target == self.gesture_mode:
            self.mode_candidate   = target
            self.mode_hold_frames = MODE_LOCK_FRAMES  # stay "saturated"
            return

        # Counting toward a *different* mode
        if target == self.mode_candidate:
            self.mode_hold_frames += 1
        else:
            # First frame of a new candidate - reset counter
            self.mode_candidate   = target
            self.mode_hold_frames = 1

        # Switch only once we've held the new posture long enough
        if self.mode_hold_frames >= MODE_LOCK_FRAMES:
            self.gesture_mode     = target
            self.mode_candidate   = target
            self.mode_hold_frames = MODE_LOCK_FRAMES
            self.mover.reset()
            self._log(f"-> {MODE_LABELS[target]}")

    # -- Shuffle helper --------------------------------------------------------

    def _check_shuffle(self, hands_lm, lm0, now, on_cooldown):
        if len(hands_lm) == 2:
            c0   = palm_center(lm0)
            c1   = palm_center(hands_lm[1])
            dist = math.dist(c0, c1)
            if self.two_hand_spread_ref is None:
                self.two_hand_spread_ref = dist
            else:
                if dist - self.two_hand_spread_ref > 0.25 and not on_cooldown:
                    result = self.action_shuffle()
                    self.two_hand_spread_ref = dist
                    self.last_action_time    = now
                    return result
            return None
        else:
            self.two_hand_spread_ref = None
            return None

    # ==========================================================================
    #  MODE HANDLERS
    # ==========================================================================

    def _mode1_play_pause(self, posture, now, on_cooldown):
        """Static gestures: Open Palm -> Play, Fist -> Pause, Thumb Up -> Like."""
        if posture == self.last_posture:
            self.posture_hold += 1
        else:
            self.posture_hold = 0
            self.last_posture = posture

        if self.posture_hold < HOLD_FRAMES or on_cooldown:
            return None

        result = None
        if posture == "OPEN_PALM": result = self.action_play()
        elif posture == "FIST":    result = self.action_pause()
        elif posture == "THUMB_UP": result = self.action_like()

        if result:
            self.last_action_time = now
            self.posture_hold     = 0
        return result

    def _mode2_volume(self, now, swipe_on_cooldown):
        """Hand UP -> Vol Up, Hand DOWN -> Vol Down."""
        if swipe_on_cooldown:
            return None
        direction = self.mover.detect()
        result = None
        if direction == "UP":
            result = self.action_volume_up()
        elif direction == "DOWN":
            result = self.action_volume_down()
        if result:
            self.last_swipe_time  = now
            self.last_action_time = now
            self.mover.reset()
        return result

    def _mode3_seek(self, now, swipe_on_cooldown):
        """Hand RIGHT -> Seek +10s, Hand LEFT -> Seek -10s."""
        if swipe_on_cooldown:
            return None
        direction = self.mover.detect()
        result = None
        if direction == "RIGHT":
            result = self.action_seek_forward()
        elif direction == "LEFT":
            result = self.action_seek_backward()
        if result:
            self.last_swipe_time  = now
            self.last_action_time = now
            self.mover.reset()
        return result

    def _mode4_change_song(self, now, swipe_on_cooldown):
        """Hand RIGHT -> Next Track, Hand LEFT -> Prev Track."""
        if swipe_on_cooldown:
            return None
        direction = self.mover.detect()
        result = None
        if direction == "RIGHT":
            result = self.action_next_track()
        elif direction == "LEFT":
            result = self.action_prev_track()
        if result:
            self.last_swipe_time  = now
            self.last_action_time = now
            self.mover.reset()
        return result


# ==============================================================================
#  OVERLAY DRAWING
# ==============================================================================

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
    s = ms // 1000
    m = s // 60
    return f"{m}:{s % 60:02d}"


_last_fps_t = [time.time()]


def draw_overlay(frame, controller: GestureController, posture, action, show_help):
    h, w = frame.shape[:2]
    sp   = controller.spotify
    st   = sp.get_state()
    mode = controller.gesture_mode

    # -- Left sidebar ----------------------------------------------------------
    sidebar = frame.copy()
    cv2.rectangle(sidebar, (0, 0), (290, h), (10, 18, 30), -1)
    cv2.addWeighted(sidebar, 0.75, frame, 0.25, 0, frame)

    cv2.putText(frame, "GESTURE MUSIC", (10, 32), FONT, 0.65, C_SPOTIFY, 2, cv2.LINE_AA)
    cv2.putText(frame, "CONTROLLER",   (10, 54), FONT, 0.65, C_SPOTIFY, 2, cv2.LINE_AA)
    cv2.line(frame, (10, 62), (278, 62), C_SPOTIFY, 1)

    # Volume bar
    vol = controller.current_volume
    bx, by, bh = 14, 80, 150
    cv2.rectangle(frame, (bx, by), (bx + 16, by + bh), (50, 50, 50), -1)
    filled_v = int(bh * vol)
    cv2.rectangle(frame, (bx, by + bh - filled_v), (bx + 16, by + bh), C_SPOTIFY, -1)
    cv2.putText(frame, "VOL", (bx, by + bh + 16), FONT, 0.38, C_GRAY, 1)
    cv2.putText(frame, f"{int(vol*100)}%", (bx, by + bh + 30), FONT, 0.44, C_WHITE, 1)

    # Current posture
    p_color = C_SPOTIFY if posture not in ("UNKNOWN", None) else C_GRAY
    cv2.putText(frame, "Posture:", (42, 94),  FONT, 0.42, C_GRAY, 1)
    cv2.putText(frame, str(posture or "-"), (42, 112), FONT, 0.48, p_color, 1, cv2.LINE_AA)

    # Playback state
    state_str = "Playing" if controller.playing else "Paused"
    cv2.putText(frame, state_str, (42, 132), FONT, 0.44,
                C_GREEN if controller.playing else C_GOLD, 1)

    # Active mode badge
    mode_color = MODE_COLORS.get(mode, C_GRAY)
    mode_label = MODE_LABELS.get(mode, "---")
    _text_bg(frame, mode_label, (8, 154), scale=0.42, color=mode_color,
             bg=(15, 25, 35), pad=4)

    # Mode-lock progress bar (filling while holding finger count)
    if (controller.mode_hold_frames > 0
            and controller.gesture_mode != controller.mode_candidate
            and controller.mode_candidate != 0):
        prog  = min(1.0, controller.mode_hold_frames / MODE_LOCK_FRAMES)
        cand  = controller.mode_candidate
        ccol  = MODE_COLORS.get(cand, C_GRAY)
        _progress_bar(frame, 8, 168, 270, 5, prog, ccol, (30, 30, 30))
        cv2.putText(frame, f"-> {MODE_LABELS.get(cand,'')}", (8, 183),
                    FONT, 0.34, ccol, 1)

    # Action flash
    if action:
        _text_bg(frame, f"> {action}", (8, 200), scale=0.56, color=C_GOLD,
                 bg=(30, 30, 10), pad=5)

    # Action log
    cv2.putText(frame, "Log:", (10, 222), FONT, 0.38, C_GRAY, 1)
    now_t2 = time.time()
    for i, (entry, ts) in enumerate(controller.action_log):
        elapsed = int(now_t2 - ts)
        if elapsed < 60:
            age = f"{elapsed}s ago"
        else:
            age = f"{elapsed // 60}m ago"
        line = f"{entry}  ({age})"
        cv2.putText(frame, line, (10, 237 + i * 16), FONT, 0.32, C_WHITE, 1)

    # FPS
    now_t = time.time()
    fps   = int(1 / max(0.001, now_t - _last_fps_t[0]))
    _last_fps_t[0] = now_t
    cv2.putText(frame, f"FPS {fps}", (10, h - 12), FONT, 0.38, C_GRAY, 1)

    # -- Spotify now-playing panel ----------------------------------------------
    panel_y    = h - 90
    np_overlay = frame.copy()
    cv2.rectangle(np_overlay, (290, panel_y), (w, h), (8, 15, 25), -1)
    cv2.addWeighted(np_overlay, 0.80, frame, 0.20, 0, frame)
    cv2.line(frame, (290, panel_y), (w, panel_y), C_SPOTIFY, 1)

    if sp.ready:
        track_txt  = st["track"][:45]  + ("..." if len(st["track"])  > 45 else "")
        artist_txt = st["artist"][:50] + ("..." if len(st["artist"]) > 50 else "")
        cv2.putText(frame, track_txt,  (300, panel_y + 22), FONT, 0.58, C_WHITE, 1, cv2.LINE_AA)
        cv2.putText(frame, artist_txt, (300, panel_y + 42), FONT, 0.46, C_GRAY,  1, cv2.LINE_AA)

        liked_txt   = "[Liked]" if st["liked"] else "[Like]"
        liked_color = C_PINK   if st["liked"] else C_GRAY
        cv2.putText(frame, liked_txt, (w - 130, panel_y + 22), FONT, 0.44, liked_color, 1)
        if st["shuffle"]:
            cv2.putText(frame, "SHUFFLE ON", (w - 130, panel_y + 42), FONT, 0.40, C_SPOTIFY, 1)

        dur  = st["duration"]
        prog = st["progress"] / dur if dur > 0 else 0
        bar_x, bar_w = 300, w - 310
        _progress_bar(frame, bar_x, panel_y + 56, bar_w, 6, prog, C_SPOTIFY)
        cv2.putText(frame, _fmt_ms(st["progress"]), (bar_x, panel_y + 78),
                    FONT, 0.40, C_GRAY, 1)
        dur_txt = _fmt_ms(dur)
        (dw, _), _ = cv2.getTextSize(dur_txt, FONT, 0.40, 1)
        cv2.putText(frame, dur_txt, (bar_x + bar_w - dw, panel_y + 78),
                    FONT, 0.40, C_GRAY, 1)
        cv2.putText(frame, f"Device: {st['device']}", (bar_x, h - 8),
                    FONT, 0.36, C_GRAY, 1)
        cv2.putText(frame, "spotify", (w - 75, h - 8), FONT, 0.52, C_SPOTIFY, 1)
    else:
        err = sp.error or "Connecting..."
        cv2.putText(frame, f"Spotify: {err}", (300, panel_y + 35), FONT, 0.50, C_GOLD, 1)
        cv2.putText(frame, "OS media keys active", (300, panel_y + 58), FONT, 0.44, C_GRAY, 1)

    # -- Help overlay ----------------------------------------------------------
    if show_help:
        mc = MODE_COLORS
        help_lines = [
            ("GESTURE GUIDE",                   C_SPOTIFY),
            ("------------------------------",   C_GRAY),
            ("Hold finger count to switch mode", C_GRAY),
            ("",                                 C_GRAY),
            (" 1 finger  ->  MODE 1: Play/Pause",  mc[1]),
            ("   Open Palm  ->  Play",            C_WHITE),
            ("   Fist       ->  Pause",           C_WHITE),
            ("   Thumb Up   ->  Like / Unlike",   C_PINK),
            ("",                                  C_GRAY),
            (" 2 fingers ->  MODE 2: Volume",     mc[2]),
            ("   Hand UP    ->  Vol Up",           C_WHITE),
            ("   Hand DOWN  ->  Vol Down",         C_WHITE),
            ("",                                  C_GRAY),
            (" 3 fingers ->  MODE 3: Seek",        mc[3]),
            ("   Hand RIGHT ->  Seek +10s",        C_WHITE),
            ("   Hand LEFT  ->  Seek -10s",        C_WHITE),
            ("",                                  C_GRAY),
            (" 4 fingers ->  MODE 4: Change Song", mc[4]),
            ("   Hand RIGHT ->  Next Track",       C_WHITE),
            ("   Hand LEFT  ->  Prev Track",       C_WHITE),
            ("",                                  C_GRAY),
            (" ANY MODE:",                         C_GRAY),
            ("   Two hands spread -> Shuffle",     C_WHITE),
            ("------------------------------",     C_GRAY),
            (" h -> hide  |  q -> quit",           C_GRAY),
        ]
        box_w = 320
        box_h = len(help_lines) * 19 + 16
        ox = w - box_w - 12
        oy = 12
        cv2.rectangle(frame, (ox - 8, oy - 8), (ox + box_w, oy + box_h), (18, 18, 18), -1)
        cv2.rectangle(frame, (ox - 8, oy - 8), (ox + box_w, oy + box_h), C_SPOTIFY, 1)
        for i, (line, color) in enumerate(help_lines):
            cv2.putText(frame, line, (ox, oy + i * 19 + 16), FONT, 0.39, color, 1)


# ==============================================================================
#  MAIN
# ==============================================================================

def main():
    print("=" * 60)
    print("  Hand Gesture Music Controller  -- Spotify Edition")
    print("=" * 60)

    creds_set = (
        SPOTIFY_CLIENT_ID     != "YOUR_CLIENT_ID_HERE" and
        SPOTIFY_CLIENT_SECRET != "YOUR_CLIENT_SECRET_HERE"
    )
    if not creds_set:
        print("\n  [!] Spotify credentials not configured.")
        print("  Set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET env vars.")
        print("  Get credentials: https://developer.spotify.com/dashboard")
        print("  Redirect URI: http://127.0.0.1:8888/callback")
        print("  Running in OS media key fallback mode.\n")
    else:
        print("\n  Spotify credentials found. Opening browser for login...")
        print("  (This only happens once - token is cached after.)\n")
    print("=" * 60)

    spotify    = SpotifyClient()
    controller = GestureController(spotify)

    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)

    if not cap.isOpened():
        print("[ERROR] Cannot open webcam (index 0). Try VideoCapture(1).")
        return

    show_help      = True
    last_action    = None
    action_clear_t = 0

    with mp_hands.Hands(
        model_complexity=0,
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

            posture = None
            if hands_lm:
                posture, *_ = controller.classify_single(hands_lm[0])

            action = controller.process(hands_lm)
            if action:
                last_action    = action
                action_clear_t = time.time() + 2.0
            if time.time() > action_clear_t:
                last_action = None

            draw_overlay(frame, controller, posture, last_action, show_help)
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
