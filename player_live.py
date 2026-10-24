#!/usr/bin/env python3
"""
Our Notes LIVE player for Windows (v0.7) — MuMu emulator edition.

Single-file live version of player.py (v0.3). It reuses the same
DualBandTracker logic (probe band -> predicted press, judge band ->
confirm / hold upgrade / slide following, rolling-median fall time),
with three live-specific changes:

  * Capture: the MuMu emulator window's client area, grabbed with dxcam
    (preferred) or mss (fallback). The window is located by title
    substring via ctypes/user32 (EnumWindows + GetClientRect +
    ClientToScreen).
  * Geometry is resolution-independent: all bands and lane boundaries
    are frame ratios, converted from the 2340x1080 calibration:
        judge band  y 0.785-0.819     probe band  y 0.639-0.672
        lane edges at y=0.803: [0.174 0.283 0.391 0.500 0.609 0.718 0.828]
        lane edges at y=1.000: [0.094 0.231 0.365 0.502 0.635 0.769 0.904]
    (linear interpolation in y; 6 lanes). The client area is assumed to
    be exactly the game screen.
  * Output: keyboard via ctypes SendInput (scancode mode). Lanes 1-6 map
    to keys D F G H J K — set the same mapping in MuMu's key-mapping
    (鍵位映射) so each key taps the corresponding lane. A tap is down for
    ~30 ms; if the judge band stays occupied the key is held (long note)
    and, when the occupied centroid shifts lane, the held key switches to
    the new lane's key (slide-following approximation). Every exit path
    releases all keys.
  * Flick notes: each track samples the median hue of its pixels when
    created. Gold/red/green hues mark a FLICK note (a static tap cannot
    trigger one). Default output (--flick-mode mouse) is a real mouse
    gesture: press at the lane's judge-line point, swipe up ~20% of
    the window height within ~100 ms in 8 steps, release — MuMu reads
    it as a touch swipe, so no MuMu widget is needed. (The v0.6
    gesture, 45 ms / 14% / 5 steps, was executed on the real machine
    but the game accepted only ~2 of 25 flicks — too fast/short, most
    likely coalesced by MuMu's mouse-to-touch sampling — hence the
    slower, longer default; all three numbers are tunable without
    editing code via --flick-duration-ms / --flick-dist /
    --flick-steps.) This replaces the
    Q W E R T Y widget approach after manual testing proved that even
    hand-pressed Q-Y keys never trigger a flick: the break was in
    MuMu's swipe-widget layer, not in timing. --flick-mode key keeps
    the old behaviour (separate key per lane, Q W E R T Y, held
    ~40 ms, for MuMu "swipe up" widgets) as a fallback. Any swipe
    direction counts in this game, so all swipes go up.
    The tracker's detection parameters are unchanged from v0.5/v0.4:
    a full A/B sweep of probe-band height, re-trigger rules,
    slide-body fallback suppression and a flick refractory all failed
    to beat the v0.4 baseline on replay (see PLAYER.md).
  * Capture optimisation: only the horizontal strip y 0.60-0.86 (which
    contains both bands) is grabbed and pasted into a black full-size
    canvas, so mss stays fast on machines whose graphics driver cannot
    do DXGI duplication for dxcam (--full-capture disables this). The
    process also declares per-monitor DPI awareness so window-rect and
    capture coordinates agree under Windows display scaling.

  * Input backend (--input, v0.7): the default "key" backend above is
    unchanged. "--input maatouch" instead injects real multi-touch
    events on the device through MaaTouch (the MAA project's Java
    touch server: pushed to /data/local/tmp, started with
    app_process, spoken to over the adb shell session's own stdio
    stream with the minitouch line protocol). Taps/holds are one
    contact per lane,
    slides move the held contact in small steps, flicks are ~7-step
    upward swipes on the lane's contact — no MuMu key mapping, no
    mouse, no window focus needed for the touches themselves (the
    window must still be visible for capture). See the MaaTouch
    constants block below for the verification status of the server
    launch strings, and PLAYER.md for the full v0.7 notes.

Modes / safety:
  --dry-run   log touches to scheduled_touches_live.csv, send NO keys.
  --preview   OpenCV window with lane boxes and trigger flashes
              (default ON in dry-run, OFF in live; --no-preview forces off).
  F8          global stop key (polled with GetAsyncKeyState). Ctrl+C too.
  --lead-ms   latency compensation: presses fire this many ms early.

Platform guard: importing this file never touches Windows APIs — dxcam /
mss / ctypes.windll are all loaded lazily at run time, so the file can be
imported and py-compiled on any OS. Actually running it requires Windows.

Usage:
  python player_live.py --dry-run            # calibrate / observe only
  python player_live.py                      # live play (sends keys)
  python player_live.py --lead-ms 25 --window-title MuMu
"""
import argparse, csv, os, queue, socket, subprocess, sys, threading, time
from collections import deque

import numpy as np

# cv2 is only needed at run time, but importing it here is harmless on
# every platform (it is not a Windows API).
try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# ------------------------------------------------------------- geometry
# Lane boundary x ratios at two reference heights (from 2340x1080 cal.)
EDGE_ROWS = {
    0.803: [0.174, 0.283, 0.391, 0.500, 0.609, 0.718, 0.828],
    1.000: [0.094, 0.231, 0.365, 0.502, 0.635, 0.769, 0.904],
}
N_LANES = 6
PROBE_Y0R, PROBE_Y1R = 0.639, 0.672      # probe band (frame height ratios)
JUDGE_Y0R, JUDGE_Y1R = 0.785, 0.819      # judge band
TOUCH_YR = 0.803                         # height at which lane centres
                                         # are reported in the CSV

VAL_MIN, SAT_MIN = 170, 25               # note-colour mask (as detect2)
FRAC_ON = 0.30                           # per-lane coverage threshold
INSET = 0.12                             # per-lane horizontal inset

TAP_HOLD_S = 0.03                        # tap press duration (~30 ms)
GRACE_S = 0.15           # v0.9 hold-upgrade hysteresis: if the
                         # conditional release comes due while the
                         # judge band has NOT been seen completely
                         # empty, the press is parked in a "grace"
                         # state for up to this long (from
                         # pred_arrival); process() upgrades it the
                         # first frame the band reads occupied again,
                         # or releases it once the band is truly
                         # empty. Live-machine evidence (diag1 frame
                         # dump, live6): a hold head's judge-band
                         # coverage on the 489-px window pixels dips
                         # to 0.0-0.27 for 1-4 frames right behind
                         # the head -- the single-frame test at
                         # pred+30 ms sampled exactly that dip (317
                         # arrived releases whose decisive-frame
                         # coverage median was 0.0, yet coverage
                         # returned to >= 0.30 a median 67 ms after
                         # arrival, max 134 ms).
GRACE_OFF_FRAMES = 3     # consecutive fully-empty judge frames that
                         # confirm a real band exit inside grace
                         # (mirrors the absent>=2 edge debounce)
STALE_S = 0.35                           # approaching-track expiry window

LANE_KEYS = ["D", "F", "G", "H", "J", "K"]           # lanes 1..6
LANE_SCANS = [0x20, 0x21, 0x22, 0x23, 0x24, 0x25]    # DIK scancodes D..K
FLICK_KEYS = ["Q", "W", "E", "R", "T", "Y"]          # lanes 1..6 (flick)
FLICK_SCANS = [0x10, 0x11, 0x12, 0x13, 0x14, 0x15]   # DIK scancodes Q..Y
FLICK_HOLD_S = 0.04                # flick key press duration (~40 ms)
JUDGE_CENTRE_R = (JUDGE_Y0R + JUDGE_Y1R) / 2.0   # 0.802: mouse flicks
                                   # start at the judge band's centre
FLICK_SWIPE_R = 0.20               # mouse flick travel, as a fraction of
                                   # the window height (--flick-dist)
FLICK_DURATION_MS = 100.0          # mouse flick duration (--flick-duration-ms)
FLICK_STEPS = 8                    # mouse flick steps (--flick-steps)
VK_F8 = 0x77

# ------------------------------------------------------------- MaaTouch
# Constants for the --input maatouch backend (v0.7; transport corrected
# in v0.7.1). Verified 2026-10-24:
#  * The bundled server file is the official MaaTouch release asset
#    "maatouch" v1.1.0 from github.com/MaaAssistantArknights/MaaTouch
#    (SHA-256 in maatouch/README.md). It is an Android dex package run
#    with app_process; it reads the minitouch line protocol from its
#    stdin and prints the banner "^ <max-contacts> <max-x> <max-y>
#    <max-pressure>" on stdout (protocol per the official repo
#    README; commands d/m/u/c/w/r).
#  * The main class and the stdio transport (NO socket, NO adb
#    forward) are taken from the working open-source implementation
#    in NakanoSanku/mtc (mtc-maatouch), which drives this same server
#    on MuMu at 127.0.0.1:16384:
#      CLASSPATH=/data/local/tmp/maatouch app_process / \
#          com.shxyke.MaaTouch.App
DEVICE_W, DEVICE_H = 2340, 1080          # MuMu device resolution
MAATOUCH_REMOTE_PATH = "/data/local/tmp/maatouch"
MAATOUCH_MAIN_CLASS = "com.shxyke.MaaTouch.App"   # verified (see above)
MAATOUCH_PRESSURE = 100                  # clamped to the banner maximum
MAATOUCH_FLICK_STEPS = 7                 # ~7 moves per flick gesture
MAATOUCH_FLICK_STEP_MS = 8               # ~8 ms per step (~60 ms total)
MAATOUCH_MOVE_STEPS = 3                  # slide-follow interpolation steps
MAATOUCH_MOVE_MS = 30                    # ... spread over ~30 ms

# Flick-note hue classification (degrees, 0-360; OpenCV hue is 0-179
# and is doubled). Gold/yellow bars, plus rarer red and green variants.
FLICK_GOLD_DEG = (25.0, 75.0)
FLICK_GREEN_DEG = (75.0, 165.0)
FLICK_RED_DEG = ((345.0, 360.0), (0.0, 15.0))
FLICK_DEDUPE_S = 0.0     # per-lane flick dedupe window: DISABLED
                         # everywhere (v0.8.1). v0.8 enabled 120 ms
                         # here on the live4 double-fire observation,
                         # but live5 (Be, visual) showed flick hits
                         # DROPPED with dedupe on, and the offline
                         # attribution in player.py had already priced
                         # it at -43 matched events: the "re-fires"
                         # are mostly distinct truth events (wide-bar
                         # edges / follow notes), not one object firing
                         # twice. Both evidence lines veto the dedupe;
                         # the logic stays in the tracker, inert, in
                         # case a judgment-level truth ever justifies
                         # revisiting it. See PLAYER.md v0.8.1.


def edges_at_ratio(yr):
    """Lane boundary x ratios at height ratio yr (linear interpolation)."""
    (y0, e0), (y1, e1) = sorted(EDGE_ROWS.items())
    a = (yr - y0) / (y1 - y0)     # linear; extrapolates beyond the two
                                  # reference rows (as player.py does)
    return [e0[i] + (e1[i] - e0[i]) * a for i in range(N_LANES + 1)]


def classify_hue_deg(hue_deg):
    """Flick colour name for a hue in degrees, or None for normal notes
    (light-blue taps ~190-205 deg, blue slides ~230-250 deg)."""
    if hue_deg is None:
        return None
    if FLICK_GOLD_DEG[0] <= hue_deg < FLICK_GOLD_DEG[1]:
        return "gold"
    if FLICK_GREEN_DEG[0] <= hue_deg < FLICK_GREEN_DEG[1]:
        return "green"
    for lo, hi in FLICK_RED_DEG:
        if lo <= hue_deg < hi:
            return "red"
    return None


def median_hue_deg(hues_cv):
    """Median of OpenCV hue samples (0-179), returned in degrees
    (0-360). Red samples straddle the 0/360 seam, so when samples sit
    on both sides of it they are rotated by 180 degrees first and the
    median is rotated back."""
    if hues_cv is None or len(hues_cv) == 0:
        return None
    d = hues_cv.astype(np.float64) * 2.0
    if d.min() < FLICK_RED_DEG[1][1] and d.max() >= FLICK_RED_DEG[0][0]:
        d = (d + 180.0) % 360.0
        return float((np.median(d) - 180.0) % 360.0)
    return float(np.median(d))


# ------------------------------------------------------- band sampling
class RatioBandSampler:
    """Per-lane coverage for one horizontal band, in pixel space built
    from the ratio geometry of a concrete frame size. Same recipe as
    player.py's BandSampler (bincount over a precomputed lane map,
    12% inset, mean of per-row fractions)."""

    def __init__(self, y0_px, y1_px, width, height):
        self.y0, self.rows = y0_px, max(y1_px - y0_px, 1)
        idx, denom = [], np.zeros((self.rows, N_LANES))
        for r in range(self.rows):
            e = [v * width for v in edges_at_ratio((y0_px + r) / height)]
            lane_map = np.full(width, -1, dtype=np.int32)
            for lane in range(N_LANES):
                w = e[lane + 1] - e[lane]
                a = int(e[lane] + w * INSET)
                b = int(e[lane + 1] - w * INSET)
                lane_map[a:b] = lane
                denom[r, lane] = max(b - a, 1)
            idx.append(np.where(lane_map >= 0, lane_map + r * N_LANES, -1))
        self.idx = np.stack(idx)
        self.flat_idx = self.idx.ravel()
        self.valid = self.flat_idx >= 0
        self.flat_valid = self.flat_idx[self.valid]
        self.denom = denom
        # flat pixel positions belonging to each lane's inset segment,
        # for per-object hue sampling
        self.lane_pos = [np.flatnonzero((self.flat_idx >= 0) &
                                        (self.flat_idx % N_LANES == lane))
                         for lane in range(N_LANES)]

    def coverage(self, mask_band):
        counts = np.bincount(self.flat_valid,
                             weights=mask_band.ravel()[self.valid].astype(float),
                             minlength=self.rows * N_LANES)
        frac_rows = counts.reshape(self.rows, N_LANES) / self.denom
        return frac_rows.mean(axis=0)

    def lane_median_hue(self, mask_band, hue_band):
        """Median hue (degrees, 0-360) of note-masked pixels per lane,
        over the same inset segments used for coverage; None where a
        lane has no masked pixels in this band right now."""
        mflat = mask_band.ravel()
        hflat = hue_band.ravel()
        out = []
        for lane in range(N_LANES):
            pos = self.lane_pos[lane]
            sel = pos[mflat[pos]]
            out.append(median_hue_deg(hflat[sel]) if len(sel) else None)
        return out


# ------------------------------------------------------- output: keys
_input_types = None


def _make_input_types():
    """Build the ctypes INPUT structures (lazily; Windows run time only)."""
    global _input_types
    if _input_types is not None:
        return _input_types
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_ulonglong))

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_ulonglong))

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD))

    class _U(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT),
                    ("hi", HARDWAREINPUT))

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD), ("u", _U))

    _input_types = (ctypes, INPUT)
    return _input_types


class KeySender:
    """SendInput (scancode mode) keyboard output for the 6 lane keys."""

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    def __init__(self):
        self.ctypes, self.INPUT = _make_input_types()
        self.user32 = self.ctypes.windll.user32
        self.pressed = set()          # lane indexes currently held down
        self.flick_pressed = set()    # lane indexes with a flick key down
        self._lock = threading.Lock()
        self._mouse_down = False      # mouse left button currently held
        self._gesture_active = False  # a mouse flick gesture is running
        self._gesture_cancel = threading.Event()

    def _send(self, scan, up):
        ctypes, INPUT = self.ctypes, self.INPUT
        flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0)
        inp = INPUT()
        inp.type = 1                  # INPUT_KEYBOARD
        inp.ki.wVk = 0
        inp.ki.wScan = scan
        inp.ki.dwFlags = flags
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        arr = (INPUT * 1)(inp)
        self.user32.SendInput(1, arr, ctypes.sizeof(INPUT))

    def down(self, lane):
        if lane not in self.pressed:
            self._send(LANE_SCANS[lane], up=False)
            self.pressed.add(lane)

    def up(self, lane):
        if lane in self.pressed:
            self._send(LANE_SCANS[lane], up=True)
            self.pressed.discard(lane)

    def flick(self, lane):
        """Press the lane's flick key (Q W E R T Y) for ~FLICK_HOLD_S,
        then release. The release runs on a daemon thread so the
        capture loop never stalls for the 40 ms press duration.
        MuMu turns this key into an upward swipe on that lane."""
        scan = FLICK_SCANS[lane]
        with self._lock:
            if lane in self.flick_pressed:
                return
            self.flick_pressed.add(lane)
        self._send(scan, up=False)

        def _release(lane=lane, scan=scan):
            time.sleep(FLICK_HOLD_S)
            with self._lock:
                if lane not in self.flick_pressed:
                    return            # release_all() already handled it
                self.flick_pressed.discard(lane)
            try:
                self._send(scan, up=True)
            except Exception:
                pass

        threading.Thread(target=_release, daemon=True).start()

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def _send_mouse(self, flags):
        ctypes, INPUT = self.ctypes, self.INPUT
        inp = INPUT()
        inp.type = 0                  # INPUT_MOUSE
        inp.mi.dx = 0
        inp.mi.dy = 0
        inp.mi.mouseData = 0
        inp.mi.dwFlags = flags
        inp.mi.time = 0
        inp.mi.dwExtraInfo = 0
        arr = (INPUT * 1)(inp)
        self.user32.SendInput(1, arr, ctypes.sizeof(INPUT))

    def _mouse_up_locked(self):
        """Send a mouse left-button-up if marked down.
        Caller must hold self._lock."""
        if self._mouse_down:
            self._mouse_down = False
            try:
                self._send_mouse(self.MOUSEEVENTF_LEFTUP)
            except Exception:
                pass

    def mouse_flick(self, sx, sy, dy_px, steps=FLICK_STEPS,
                    duration_s=FLICK_DURATION_MS / 1000.0):
        """One upward mouse swipe: press at (sx, sy) — the lane's
        judge-line point in screen coordinates — move up dy_px over
        ~duration_s in a few steps, then release. Runs on a daemon
        thread so the capture loop never stalls. MuMu reads a mouse
        press-drag-release on its window as a touch swipe, so this
        needs no MuMu key-mapping widget (the Q W E R T Y widget
        approach proved unable to trigger flicks in manual testing).
        If a gesture is already running the call is coalesced away:
        a wide flick bar fires one event per covered lane at nearly
        the same moment, and one swipe anywhere on the bar is what
        the game needs."""
        with self._lock:
            if self._gesture_active:
                return
            self._gesture_active = True
            self._gesture_cancel.clear()

        def _gesture():
            try:
                self.user32.SetCursorPos(int(sx), int(sy))
                with self._lock:
                    self._mouse_down = True
                self._send_mouse(self.MOUSEEVENTF_LEFTDOWN)
                per = duration_s / steps
                for k in range(1, steps + 1):
                    if self._gesture_cancel.is_set():
                        break
                    time.sleep(per)
                    self.user32.SetCursorPos(
                        int(sx), int(sy - dy_px * k / steps))
            except Exception:
                pass
            finally:
                with self._lock:
                    self._mouse_up_locked()
                    self._gesture_active = False

        threading.Thread(target=_gesture, daemon=True).start()

    def release_all(self):
        for lane in sorted(self.pressed):
            try:
                self._send(LANE_SCANS[lane], up=True)
            except Exception:
                pass
        self.pressed.clear()
        with self._lock:
            flicks = sorted(self.flick_pressed)
            self.flick_pressed.clear()
        for lane in flicks:
            try:
                self._send(FLICK_SCANS[lane], up=True)
            except Exception:
                pass
        # mouse: cancel any running flick gesture and guarantee the
        # left button is up even when exiting mid-gesture (F8 / Ctrl+C)
        self._gesture_cancel.set()
        with self._lock:
            self._mouse_up_locked()


# ------------------------------------------------------- output: MaaTouch
class MaaTouchError(RuntimeError):
    """A MaaTouch launcher step failed; the message names the step
    and carries that step's own output."""


class _ProcTransport:
    """Byte transport over the adb shell process that runs MaaTouch:
    commands go to its stdin, the banner and any server output come
    from its stdout (drained by the launcher's reader thread)."""

    def __init__(self, proc):
        self.proc = proc

    def sendall(self, data):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise MaaTouchError(f"MaaTouch shell stream broke: {e}")

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


class MaaTouchSender:
    """minitouch-protocol touch sender over a byte transport (the
    adb shell stdio stream of the MaaTouch server process).

    Same calling pattern as KeySender (down/up/flick/release_all),
    but every method takes a contact id and device-pixel coordinates.
    down/up are idempotent per contact, like KeySender's key state.
    Gestures (flick, move_to) run on daemon threads so the capture
    loop never stalls; all writes are serialised by one lock.
    """

    def __init__(self, transport, banner):
        self.transport = transport
        (self.max_contacts, self.max_x,
         self.max_y, self.max_pressure) = banner
        self.pressure = max(1, min(MAATOUCH_PRESSURE, self.max_pressure))
        self._lock = threading.Lock()
        self._active = {}              # contact id -> (x, y)
        self._closed = False

    @property
    def active_contacts(self):
        with self._lock:
            return set(self._active)

    # -- low level ------------------------------------------------------
    def _send(self, *lines):
        data = ("\n".join(lines) + "\n").encode("utf-8")
        with self._lock:
            if self._closed:
                raise MaaTouchError("MaaTouch transport is closed")
            self.transport.sendall(data)

    def _clamp(self, x, y):
        return (max(0, min(int(round(x)), self.max_x)),
                max(0, min(int(round(y)), self.max_y)))

    # -- primitives -----------------------------------------------------
    def down(self, cid, x, y):
        with self._lock:
            if cid in self._active:
                return
        x, y = self._clamp(x, y)
        self._send(f"d {cid} {x} {y} {self.pressure}", "c")
        with self._lock:
            self._active[cid] = (x, y)

    def up(self, cid):
        with self._lock:
            if cid not in self._active:
                return
            del self._active[cid]
        self._send(f"u {cid}", "c")

    def move_to(self, cid, x, y, steps=MAATOUCH_MOVE_STEPS,
                total_ms=MAATOUCH_MOVE_MS):
        """Slide an already-down contact to (x, y) in a few steps
        spread over ~total_ms (server-side w waits between the m
        steps), on a daemon thread, so slide following never stalls
        the capture loop."""
        with self._lock:
            start = self._active.get(cid)
        if start is None:
            return
        x, y = self._clamp(x, y)
        sx, sy = start
        per = max(total_ms // max(steps, 1), 1)

        def _run():
            try:
                for k in range(1, steps + 1):
                    nx = sx + (x - sx) * k / steps
                    ny = sy + (y - sy) * k / steps
                    self._send(f"m {cid} {int(round(nx))} "
                               f"{int(round(ny))} {self.pressure}",
                               f"w {per}", "c")
                with self._lock:
                    if cid in self._active:
                        self._active[cid] = (x, y)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def flick(self, cid, x, y, dist_px, steps=MAATOUCH_FLICK_STEPS,
              step_ms=MAATOUCH_FLICK_STEP_MS):
        """Upward flick: always a clean down-move-up. If the contact
        was held (long note), lift first so the game sees a real swipe.
        Runs on a daemon thread."""
        x, y = self._clamp(x, y)
        top = max(y - int(round(dist_px)), 0)

        def _run():
            try:
                with self._lock:
                    was_held = cid in self._active
                    self._active.pop(cid, None)
                if was_held:
                    self._send(f"u {cid}", "c")
                    self._send(f"w {8}", "c")
                self._send(f"d {cid} {x} {y} {self.pressure}", "c")
                with self._lock:
                    self._active[cid] = (x, y)
                for k in range(1, steps + 1):
                    ny = y + (top - y) * k / steps
                    self._send(f"m {cid} {x} {int(round(ny))} "
                               f"{self.pressure}", f"w {step_ms}", "c")
                with self._lock:
                    self._active.pop(cid, None)
                self._send(f"u {cid}", "c")
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def release_all(self):
        """Lift every active contact, then reset the server (r is
        part of the minitouch protocol MaaTouch mirrors). Safe to
        call repeatedly and on a dead stream."""
        with self._lock:
            ids = sorted(self._active)
            self._active.clear()
        try:
            self._send(*([f"u {cid}" for cid in ids] + ["c", "r", "c"]))
        except Exception:
            pass

    def close(self):
        self.release_all()
        with self._lock:
            self._closed = True
        try:
            self.transport.close()
        except Exception:
            pass


class MaaTouchSession:
    """Owns a MaaTouch connection: the sender plus the adb shell
    process that runs the server (the sender's transport closes it).
    The device-side server exits with its shell session."""

    def __init__(self, adb, addr, sender):
        self.adb, self.addr, self.sender = adb, addr, sender

    def close(self):
        try:
            self.sender.close()
        except Exception:
            pass


def _adb_run(adb, args, step, timeout=30):
    """Run one adb command for the launcher. On failure raise
    MaaTouchError naming the step and carrying the command's own
    output — the launcher never falls back silently."""
    cmd = [adb] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise MaaTouchError(
            f"step '{step}': adb not found ({adb!r}). Install Google "
            "platform-tools and/or pass --adb with the full path to "
            "the adb executable.")
    except subprocess.TimeoutExpired:
        raise MaaTouchError(f"step '{step}': timed out running: "
                            f"{' '.join(cmd)}")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        raise MaaTouchError(f"step '{step}' failed (exit "
                            f"{p.returncode}): {' '.join(cmd)}\n{out}")
    return out


def start_maatouch(adb, addr, main_class=MAATOUCH_MAIN_CLASS):
    """Launcher for --input maatouch, run once at startup:
    locate the bundled server file -> adb connect -> push -> start
    the server with app_process inside an adb shell session and read
    its banner from that session's stdout; commands are then written
    to the session's stdin (this is how MaaTouch works — there is no
    listening socket and no adb forward). Any failure raises
    MaaTouchError naming the step and its output; there is NO silent
    fallback to key mode. Returns a MaaTouchSession on success."""
    import queue
    here = os.path.dirname(os.path.abspath(__file__))
    local_file = os.path.join(here, "maatouch", "maatouch")
    if not os.path.isfile(local_file):
        raise MaaTouchError(
            "step 'locate': the MaaTouch server file is missing — "
            f"expected it at {local_file}\nThe package normally "
            "bundles the official 'maatouch' v1.1.0 file (see "
            "maatouch/README.md). If it was deleted, download the "
            "'maatouch' asset from the official release at "
            "github.com/MaaAssistantArknights/MaaTouch, put it in "
            "the maatouch folder next to player_live.py, and retry.")
    print(f"[maatouch] adb connect {addr}")
    out = _adb_run(adb, ["connect", addr], "connect")
    print(f"[maatouch]   {out.splitlines()[0] if out else 'ok'}")
    print(f"[maatouch] push server -> {MAATOUCH_REMOTE_PATH}")
    _adb_run(adb, ["-s", addr, "push", local_file,
                   MAATOUCH_REMOTE_PATH], "push", timeout=120)
    shell_cmd = (f"CLASSPATH={MAATOUCH_REMOTE_PATH} app_process / "
                 f"{main_class}")
    print(f"[maatouch] start server: adb shell {shell_cmd}")
    try:
        proc = subprocess.Popen([adb, "-s", addr, "shell", shell_cmd],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise MaaTouchError(
            f"step 'start': adb not found ({adb!r}). Install Google "
            "platform-tools and/or pass --adb with the full path to "
            "the adb executable.")
    server_log = []
    line_q = queue.Queue()
    banner_done = threading.Event()

    def _reader():
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                server_log.append(line)
                if not banner_done.is_set():
                    line_q.put(line)
        except Exception:
            pass
        line_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    print("[maatouch] waiting for banner on the shell stream ...")
    banner = None
    banner_line = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            line = line_q.get(timeout=0.25)
        except queue.Empty:
            continue
        if line is None:
            break
        parts = line.split()
        if len(parts) == 5 and parts[0] == "^":
            try:
                banner = tuple(int(v) for v in parts[1:])
            except ValueError:
                banner = None
            if banner is not None:
                banner_line = line
                break
        # Any other line (e.g. a "v <version>" line or server
        # diagnostics) is kept in server_log and skipped.
    banner_done.set()
    if banner is None:
        try:
            proc.terminate()
        except Exception:
            pass
        raise MaaTouchError(
            "step 'banner': no '^ <max-contacts> <max-x> <max-y> "
            "<max-pressure>' banner from the MaaTouch server within "
            f"10 s (process exit code: {proc.poll()}). Server output "
            "so far:\n" + "\n".join(server_log[-20:]) +
            "\nCheck that the pushed file is the official MaaTouch "
            "server and that the main class is right "
            f"(--maatouch-class; current value {main_class!r}).")
    sender = MaaTouchSender(_ProcTransport(proc), banner)
    print(f"[maatouch] connected: banner {banner_line!r} "
          f"(device {addr}, via adb shell stdio)")
    return MaaTouchSession(adb, addr, sender)


class LiveAdapter:
    """TouchAdapter equivalent for keys. Always records rows in the same
    columns as player.py's scheduled_touches.csv (time_s, action, lane,
    x_px, kind, origin); sends real keys unless dry_run. A move is a
    key switch: old lane key up, new lane key down. A flick is played
    as a mouse swipe by default (--flick-mode mouse: press at the
    lane's judge-line point, swipe up, release — no MuMu widget
    needed); with --flick-mode key it sends the lane's Q W E R T Y
    key instead (for MuMu swipe widgets). In dry-run a flick is only
    recorded, like everything else. With input_mode='maatouch' an
    externally built MaaTouchSender replaces the KeySender: down/up
    become contact down/up in device pixels (one contact per lane),
    a move glides the held contact to the new lane in small steps,
    and a flick is an upward swipe gesture on the lane's contact —
    --flick-mode and the MuMu widgets are then irrelevant."""

    def __init__(self, csv_path, dry_run=False, flick_mode="mouse",
                 flick_dist=FLICK_SWIPE_R,
                 flick_duration_ms=FLICK_DURATION_MS,
                 flick_steps=FLICK_STEPS,
                 sender=None, input_mode="key"):
        self.rows = []
        self.csv_path = csv_path
        self.dry_run = dry_run
        self.flick_mode = flick_mode
        self.flick_dist = flick_dist            # fraction of window height
        self.flick_duration_ms = flick_duration_ms
        self.flick_steps = flick_steps
        self.flick_geom = None        # (client_left, client_top, frame_h)
        self.input_mode = input_mode  # "key" (default) | "maatouch"
        if dry_run:
            self.sender = None
        elif sender is not None:
            self.sender = sender      # MaaTouchSender (maatouch mode)
        else:
            self.sender = KeySender()
        # MaaTouch coordinate conversion state: the capture size is
        # set by main on every tracker build; the device size is the
        # fixed 2340x1080 MuMu configuration.
        self.frame_w, self.frame_h = DEVICE_W, DEVICE_H
        self._lane_cid = {}           # lane (1-based) -> contact id
        self._cid_lane = {}           # contact id -> lane (1-based)
        self.n_down = 0
        self.n_up = 0
        self.n_move = 0
        self.n_flick = 0

    def set_flick_geometry(self, client_left, client_top, frame_h):
        """Tell the adapter where the emulator client area sits on
        screen, so mouse-mode flicks can be aimed at the lane's
        judge-line point (client origin + the tracker's frame x)."""
        self.flick_geom = (client_left, client_top, frame_h)

    def set_frame_size(self, frame_w, frame_h):
        """Capture frame size for the MaaTouch coordinate conversion
        (x_dev = x_px * 2340 / frame_w, y_dev likewise with 1080)."""
        self.frame_w, self.frame_h = frame_w, frame_h

    def _dev_x(self, x_px):
        return int(round(x_px * DEVICE_W / max(self.frame_w, 1)))

    def _dev_y(self, yr):
        # y_px would be yr * frame_h; y_dev = y_px * 1080 / frame_h
        return int(round(yr * DEVICE_H))

    def _alloc_cid(self, lane):
        """Contact id for a press on lane (1-based): the lane's own
        index, unless that contact is still busy holding another
        lane (a slid hold), in which case any free contact; if all
        contacts are busy, the lane's own index (the sender's
        idempotent down then simply keeps the existing touch)."""
        cid = lane - 1
        if self._cid_lane.get(cid, lane) == lane:
            return cid
        busy = set(self._cid_lane) | self.sender.active_contacts
        for cand in range(getattr(self.sender, "max_contacts",
                                  N_LANES)):
            if cand not in busy:
                return cand
        return cid

    def _rec(self, t, action, lane, x, kind, origin):
        self.rows.append(dict(time_s=round(t, 3), action=action, lane=lane,
                              x_px=int(x), kind=kind, origin=origin))

    def down(self, t, lane, x, kind, origin):
        self._rec(t, "down", lane, x, kind, origin)
        self.n_down += 1
        # Return the stored row: the tracker amends its kind when the
        # press turns out to be a hold/slide (v0.8 truthful kinds).
        row = self.rows[-1]
        if self.sender:
            if self.input_mode == "maatouch":
                cid = self._alloc_cid(lane)
                self._lane_cid[lane] = cid
                self._cid_lane[cid] = lane
                self.sender.down(cid, self._dev_x(x),
                                 self._dev_y(TOUCH_YR))
            else:
                self.sender.down(lane - 1)
        return row

    def up(self, t, lane, x, kind, origin):
        self._rec(t, "up", lane, x, kind, origin)
        self.n_up += 1
        if self.sender:
            if self.input_mode == "maatouch":
                cid = self._lane_cid.pop(lane, lane - 1)
                if self._cid_lane.get(cid) == lane:
                    del self._cid_lane[cid]
                self.sender.up(cid)
            else:
                self.sender.up(lane - 1)

    def move(self, t, from_lane, to_lane, x, origin):
        self._rec(t, "move", to_lane, x, "slide", origin)
        self.n_move += 1
        if self.sender:
            if self.input_mode == "maatouch":
                # Real slide following: the held contact itself
                # glides to the new lane in a few small steps
                # (background), instead of the key mode's key switch.
                cid = self._lane_cid.pop(from_lane, from_lane - 1)
                self._lane_cid[to_lane] = cid
                self._cid_lane[cid] = to_lane
                self.sender.move_to(cid, self._dev_x(x),
                                    self._dev_y(TOUCH_YR))
            else:
                self.sender.up(from_lane - 1)
                self.sender.down(to_lane - 1)

    def flick(self, t, lane, x, origin):
        self._rec(t, "flick", lane, x, "flick", origin)
        self.n_flick += 1
        if self.sender:
            if self.input_mode == "maatouch":
                cid = self._lane_cid.pop(lane, None)
                if cid is not None:
                    self._cid_lane.pop(cid, None)
                self.sender.flick(lane - 1, self._dev_x(x),
                                  self._dev_y(JUDGE_CENTRE_R),
                                  int(round(self.flick_dist * DEVICE_H)))
            elif self.flick_mode == "mouse" and self.flick_geom is not None:
                left, top, h = self.flick_geom
                self.sender.mouse_flick(
                    left + x,
                    top + int(JUDGE_CENTRE_R * h),
                    int(self.flick_dist * h),
                    steps=self.flick_steps,
                    duration_s=self.flick_duration_ms / 1000.0)
            else:
                self.sender.flick(lane - 1)

    def release_all(self):
        self._lane_cid.clear()
        self._cid_lane.clear()
        if self.sender:
            self.sender.release_all()

    def close(self):
        with open(self.csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time_s", "action", "lane",
                                              "x_px", "kind", "origin"])
            w.writeheader(); w.writerows(self.rows)
        if self.sender is not None and hasattr(self.sender, "close"):
            try:
                self.sender.close()   # MaaTouch: release + close stream
            except Exception:
                pass


# ------------------------------------------------------- dual-band tracker
# Logic mirrors player.py's DualBandTracker step for step; only the
# geometry source (frame ratios for an arbitrary capture size) differs.
class Track:
    __slots__ = ("lane", "probe_on", "pred_arrival", "origin", "state",
                 "judge_since", "off_run", "path", "down_done",
                 "hue_deg", "hue_name", "became_hold", "down_row",
                 "grace_off", "rider")

    def __init__(self, lane, probe_on, pred_arrival, origin, hue_deg=None):
        self.lane = lane
        self.probe_on = probe_on
        self.pred_arrival = pred_arrival
        self.origin = origin              # 'predicted' | 'fallback'
        self.state = "approaching"        # approaching|arrived|holding|
                                          # grace|done (grace: v0.9)
        self.judge_since = None
        self.off_run = 0
        self.path = [lane]
        self.down_done = False
        self.hue_deg = hue_deg            # median hue at creation (degrees)
        self.hue_name = classify_hue_deg(hue_deg)  # flick colour or None
        self.became_hold = False          # v0.8: upgraded to a hold
        self.down_row = None              # v0.8: adapter row of our down
        self.grace_off = 0                # v0.9: consecutive fully-empty
                                          # judge frames seen in grace
        self.rider = False                # v0.9: fallback created while
                                          # another track already owns
                                          # this lane's band episode;
                                          # it fires its down but never
                                          # upgrades into a second
                                          # hold head for the same note

    @property
    def is_flick(self):
        return self.hue_name is not None

    @property
    def final_kind(self):
        """Truthful press kind once known (v0.8): 'slide' if it
        followed lanes, 'hold' if it upgraded, else 'tap'."""
        if len(self.path) > 1:
            return "slide"
        if self.became_hold:
            return "hold"
        return "tap"


class DualBandTracker:
    def __init__(self, adapter, width, height, lead_ms=0.0,
                 fall_time_init=0.08, flick_lead_ms=None,
                 fallback_lead_ms=25.0):
        self.adapter = adapter
        self.W, self.H = width, height
        self.lead = lead_ms / 1000.0
        # Flicks are recognised by the game only once the swipe has
        # accumulated enough displacement, which happens well after the
        # gesture starts (frame analysis of a live recording, 2026-10-24:
        # mouse flicks fired with the tap lead registered ~40-90 ms
        # late, mostly GOOD). They therefore get their own, larger lead.
        self.flick_lead = (lead_ms if flick_lead_ms is None
                           else flick_lead_ms) / 1000.0
        self.fallback_lead = max(fallback_lead_ms, 0.0) / 1000.0
        self.transit_est = fall_time_init
        self.transits = deque(maxlen=60)
        self.probe_y0 = int(PROBE_Y0R * height)
        self.probe_y1 = int(PROBE_Y1R * height)
        self.judge_y0 = int(JUDGE_Y0R * height)
        self.judge_y1 = int(JUDGE_Y1R * height)
        self.crop_y0, self.crop_y1 = self.probe_y0, self.judge_y1
        self.probe = RatioBandSampler(self.probe_y0, self.probe_y1,
                                      width, height)
        self.judge = RatioBandSampler(self.judge_y0, self.judge_y1,
                                      width, height)
        self.probe_on = [False] * N_LANES
        self.probe_absent = [99] * N_LANES
        self.judge_on = [False] * N_LANES
        self.judge_absent = [99] * N_LANES
        self.judge_cov_hist = [[] for _ in range(N_LANES)]
        # ^ v0.9: last four frames of judge coverage per lane; the
        # conditional release consults it to tell "band episode over"
        # (four fully-empty frames) from "head/neck coverage dip".
        self.tracks = []
        self.pending = []                 # (time, seq, fn)
        self.seq = 0
        self.press_stack = [[] for _ in range(N_LANES)]  # v0.8: tracks
                                              # merged into each lane's
                                              # physical contact
        self.last_flick_t = [None] * N_LANES  # v0.8: last scheduled
                                              # flick time per lane
        self.stats = dict(scheduled=0, predicted=0, fallback=0,
                          holds=0, holds_predicted=0, holds_fallback=0,
                          moves=0, blind=0, flicks=0,
                          flick_gold=0, flick_red=0, flick_green=0,
                          ups_merged=0, flick_deduped=0,
                          grace_entered=0, grace_upgraded=0,
                          grace_released=0, rider_fallbacks=0)
        self.last_judge_cov = np.zeros(N_LANES)
        self.last_probe_cov = np.zeros(N_LANES)

    def lane_centre_x(self, lane):
        e = edges_at_ratio(TOUCH_YR)
        return int(round((e[lane] + e[lane + 1]) / 2.0 * self.W))

    # -- scheduling ------------------------------------------------------
    def _schedule(self, t, fn):
        self.pending.append((t, self.seq, fn))
        self.seq += 1

    def _dispatch_due(self, now):
        due = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _, _, fn in sorted(due):
            fn()

    # -- v0.9 press helpers ---------------------------------------------
    def _release_press(self, tr, t):
        """Emit (or merge) the conditional release of a press track
        and retire it. v0.8 merged-contact stack semantics: the
        adapter up is emitted only when this track is the last one
        merged into the lane's physical contact."""
        tr.state = "done"
        stack = self.press_stack[tr.lane]
        if tr in stack:
            stack.remove(tr)
            if stack:
                self.stats["ups_merged"] += 1
                return        # contact continues for the others
        self.adapter.up(t, tr.lane + 1, self.lane_centre_x(tr.lane),
                        "tap", tr.origin)

    def _band_exit_unseen(self, lane):
        """True unless the judge band has been completely empty for
        the last four frames (a confirmed exit; the debounce is
        longer than the band edges' because head/neck coverage
        dips at 489 px include fully-empty runs of 2-3 frames). A press whose band episode has not provably
        ended earns the v0.9 grace window instead of an instant
        release."""
        h = self.judge_cov_hist[lane]
        return not (len(h) >= 4 and all(c <= 0.0 for c in h))

    def _become_hold(self, tr):
        tr.state = "holding"
        tr.became_hold = True
        self.stats["holds"] += 1
        self.stats["holds_" + tr.origin] += 1
        if tr.down_row is not None:
            tr.down_row["kind"] = "hold"

    def _schedule_press(self, track, down_t):
        # v0.8 (mirrors player.py): the conditional up is anchored to
        # max(down_t, pred_arrival) + TAP_HOLD_S so the hold-upgrade
        # check in process() — which compares against pred_arrival +
        # TAP_HOLD_S and runs before dispatch — always gets its chance
        # to cancel the release. Anchoring to down_t alone released
        # every predicted press `lead` ms before the upgrade check
        # could pass, making predicted holds impossible (live4: 1 of
        # 294 long contacts started predicted).
        # Merged-contact stack: tracks merged into one lane contact
        # all fire their down, but only the LAST one out emits the
        # adapter up — an interloping track finishing early can no
        # longer cut the owner's hold short.
        ad = self.adapter
        up_t = max(down_t, track.pred_arrival) + TAP_HOLD_S

        def do_down(tr=track, t=down_t):
            # v0.9: a fresh press on this lane ends any undecided
            # (grace-state) press first, so a following note always
            # gets its own contact edge (with the merged stack the
            # physical up only fires when the stack actually empties).
            for other in list(self.press_stack[tr.lane]):
                if not tr.rider and other is not tr \
                        and other.state == "grace":
                    self._release_press(other, t)
            tr.down_done = True
            self.stats["scheduled"] += 1
            self.stats[tr.origin] += 1
            tr.down_row = ad.down(t, tr.lane + 1,
                                  self.lane_centre_x(tr.lane),
                                  "tap", tr.origin)
            self.press_stack[tr.lane].append(tr)

        def do_up(tr=track, t=up_t):
            if tr.state == "holding":
                return            # upgraded: release handled at hold end
            if tr.rider:
                # v0.9 rider: the episode's owner decides the hold;
                # this duplicate press simply ends (stack-merged).
                self._release_press(tr, t)
                return
            if tr.state in ("arrived", "approaching") \
                    and self._band_exit_unseen(tr.lane):
                # v0.9 hysteresis: the band episode has not provably
                # ended (the head/neck coverage dip is not an exit) --
                # park the press in grace; process() upgrades it when
                # the body confirms, or releases it when the band is
                # truly empty / the window expires.
                tr.state = "grace"
                tr.grace_off = 0
                self.stats["grace_entered"] += 1
                return
            self._release_press(tr, t)

        self._schedule(down_t, do_down)
        self._schedule(up_t, do_up)

    def _schedule_flick(self, track, fire_t):
        # v0.8 dedupe (mirrors player.py): a second flick within
        # FLICK_DEDUPE_S in the same lane is the same object's body
        # re-firing — one tracked object, one gesture.
        last = self.last_flick_t[track.lane]
        if last is not None and 0.0 <= fire_t - last < FLICK_DEDUPE_S:
            self.stats["flick_deduped"] += 1
            return
        self.last_flick_t[track.lane] = fire_t
        ad = self.adapter

        def do_flick(tr=track, t=fire_t):
            tr.down_done = True
            self.stats["scheduled"] += 1
            self.stats["flicks"] += 1
            self.stats["flick_" + tr.hue_name] += 1
            self.stats[tr.origin] += 1
            ad.flick(t, tr.lane + 1, self.lane_centre_x(tr.lane), tr.origin)

        self._schedule(fire_t, do_flick)

    # -- per-frame processing ---------------------------------------------
    def process(self, t, frame):
        crop = frame[self.crop_y0:self.crop_y1]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = (hsv[:, :, 2] >= VAL_MIN) & (hsv[:, :, 1] >= SAT_MIN)
        probe_mask = mask[self.probe_y0 - self.crop_y0:
                          self.probe_y1 - self.crop_y0]
        judge_mask = mask[self.judge_y0 - self.crop_y0:
                          self.judge_y1 - self.crop_y0]
        probe_cov = self.probe.coverage(probe_mask)
        judge_cov = self.judge.coverage(judge_mask)
        self.last_probe_cov, self.last_judge_cov = probe_cov, judge_cov
        for l in range(N_LANES):
            h = self.judge_cov_hist[l]
            h.append(float(judge_cov[l]))
            if len(h) > 4:
                del h[0]
        probe_now = probe_cov >= FRAC_ON
        judge_now = judge_cov >= FRAC_ON

        probe_edge, judge_edge = [], []
        for l in range(N_LANES):
            if probe_now[l] and not self.probe_on[l] and self.probe_absent[l] >= 2:
                probe_edge.append(l)
            if judge_now[l] and not self.judge_on[l] and self.judge_absent[l] >= 2:
                judge_edge.append(l)
            self.probe_on[l] = bool(probe_now[l])
            self.probe_absent[l] = 0 if probe_now[l] else self.probe_absent[l] + 1
            self.judge_on[l] = bool(judge_now[l])
            self.judge_absent[l] = 0 if judge_now[l] else self.judge_absent[l] + 1

        # 1) probe entries -> new tracks with predicted arrivals.
        # Each new track samples the median hue of its masked pixels in
        # the probe band: gold/red/green means a flick note.
        probe_hues = None
        for l in probe_edge:
            if probe_hues is None:
                probe_hues = self.probe.lane_median_hue(
                    probe_mask,
                    hsv[self.probe_y0 - self.crop_y0:
                        self.probe_y1 - self.crop_y0, :, 0])
            pred = t + self.transit_est
            tr = Track(l, t, pred, "predicted", hue_deg=probe_hues[l])
            self.tracks.append(tr)
            if tr.is_flick:
                self._schedule_flick(tr, max(pred - self.flick_lead, t))
            else:
                self._schedule_press(tr, max(pred - self.lead, t))

        # 2) judge entries -> confirm an approaching track, else fallback
        judge_hues = None
        for l in judge_edge:
            cand = [tr for tr in self.tracks
                    if tr.state == "approaching" and tr.lane == l]
            if cand:
                tr = min(cand, key=lambda c: c.pred_arrival)
                tr.state = "arrived"
                tr.judge_since = t
                if tr.probe_on is not None:
                    self.transits.append(t - tr.probe_on)
                    if len(self.transits) >= 5:
                        self.transit_est = float(np.median(self.transits))
            else:
                if judge_hues is None:
                    judge_hues = self.judge.lane_median_hue(
                        judge_mask,
                        hsv[self.judge_y0 - self.crop_y0:
                            self.judge_y1 - self.crop_y0, :, 0])
                tr = Track(l, None, t, "fallback", hue_deg=judge_hues[l])
                # v0.9 rider marking: if another track already
                # owns this lane's band episode (holding it, or
                # parked in the upgrade grace window with its press
                # open), this fallback is a re-crossing of the SAME
                # object: it fires its down (the offline truth
                # counts re-edges as events in their own right) but
                # must not upgrade into a second hold head for one
                # note, nor force-release its owner.
                if any(o.lane == l and o.down_done
                       and o.state in ("holding", "grace")
                       for o in self.tracks):
                    tr.rider = True
                    self.stats["rider_fallbacks"] += 1
                tr.state = "arrived"
                tr.judge_since = t
                self.tracks.append(tr)
                fire_t = t - self.fallback_lead
                if tr.is_flick:
                    self._schedule_flick(tr, fire_t)
                else:
                    self._schedule_press(tr, fire_t)

        # 3) hold upgrade / slide following / hold end
        # (flick tracks excluded: a flick never becomes a hold)
        for tr in self.tracks:
            if tr.state == "arrived" and tr.down_done \
                    and not tr.is_flick and not tr.rider:
                if t >= tr.pred_arrival + TAP_HOLD_S and judge_now[tr.lane]:
                    self._become_hold(tr)
            elif tr.state == "grace" and not tr.is_flick:
                # v0.9 hysteresis resolution, in priority order:
                # the body confirms -> hold; the window expires ->
                # release; the band is provably empty (two fully
                # empty frames) -> release as the tap it was.
                if judge_now[tr.lane]:
                    if tr.judge_since is None:
                        tr.judge_since = t
                    self.stats["grace_upgraded"] += 1
                    self._become_hold(tr)
                elif t >= tr.pred_arrival + GRACE_S:
                    if judge_cov[tr.lane] > 0.0:
                        # never actually left the band during the
                        # whole window (slow thin-body ramp at 489
                        # px): it is a body, not a tap remnant
                        if tr.judge_since is None:
                            tr.judge_since = t
                        self.stats["grace_upgraded"] += 1
                        self._become_hold(tr)
                    else:
                        self.stats["grace_released"] += 1
                        self._release_press(tr, t)
                elif judge_cov[tr.lane] <= 0.0:
                    tr.grace_off += 1
                    if tr.grace_off >= GRACE_OFF_FRAMES:
                        self.stats["grace_released"] += 1
                        self._release_press(tr, t)
                else:
                    tr.grace_off = 0
            elif tr.state == "holding":
                if judge_now[tr.lane]:
                    tr.off_run = 0
                else:
                    tr.off_run += 1
                    if tr.off_run >= 2:
                        nxt = None
                        for nb in (tr.lane - 1, tr.lane + 1):
                            if 0 <= nb < N_LANES and judge_now[nb] and not any(
                                    o is not tr and o.state == "holding"
                                    and o.lane == nb for o in self.tracks):
                                nxt = nb
                                break
                        if nxt is not None:
                            self.adapter.move(t, tr.lane + 1, nxt + 1,
                                              self.lane_centre_x(nxt),
                                              tr.origin)
                            self.stats["moves"] += 1
                            # merged-contact membership follows the
                            # contact into the new lane (v0.8)
                            old_stack = self.press_stack[tr.lane]
                            if tr in old_stack:
                                old_stack.remove(tr)
                            self.press_stack[nxt].append(tr)
                            tr.lane = nxt
                            tr.path.append(nxt)
                            tr.off_run = 0
                            if tr.down_row is not None:
                                tr.down_row["kind"] = "slide"
                        elif (tr.off_run >= 4 and \
                                judge_cov[tr.lane] <= 0.0) or \
                                tr.off_run >= 6:
                            tr.state = "done"
                            if tr.down_row is not None:
                                tr.down_row["kind"] = tr.final_kind
                            stack = self.press_stack[tr.lane]
                            if tr in stack:
                                stack.remove(tr)
                                if stack:
                                    # other tracks still need this
                                    # contact; the last one out emits
                                    # the up (v0.8 merged contacts)
                                    self.stats["ups_merged"] += 1
                                    continue
                            self.adapter.up(t, tr.lane + 1,
                                            self.lane_centre_x(tr.lane),
                                            tr.final_kind, tr.origin)

        # 4) expire stale tracks (confirmed flick tracks retire here
        # too, since they never pass through the hold-end path)
        for tr in self.tracks:
            if tr.state == "approaching" and t > tr.pred_arrival + STALE_S:
                tr.state = "done"
                self.stats["blind"] += 1
            elif tr.is_flick and tr.state == "arrived" and \
                    t > tr.pred_arrival + STALE_S:
                tr.state = "done"
        self.tracks = [tr for tr in self.tracks if tr.state != "done"]

        # 5) fire due actions
        self._dispatch_due(t)

    def finish(self, t_end):
        self._dispatch_due(t_end + 10.0)
        # v0.9: presses parked in grace are not "holding"; release
        # them explicitly or their contacts would stick at stream end
        # (a do_up flushed by the dispatch above can enter grace).
        for tr in self.tracks:
            if tr.state == "grace":
                self._release_press(tr, t_end)
        for tr in self.tracks:
            if tr.state == "holding" and tr.down_done:
                if tr.down_row is not None:
                    tr.down_row["kind"] = tr.final_kind
                self.adapter.up(t_end, tr.lane + 1,
                                self.lane_centre_x(tr.lane),
                                tr.final_kind, tr.origin)
        self.tracks = []


# ------------------------------------------------------- window location
def set_dpi_awareness():
    """Make this process per-monitor DPI aware (Windows run time only).

    Without it, on a display scaled to e.g. 125%, user32 returns
    virtualised (logical) coordinates for the window rect while dxcam
    grabs physical pixels: the capture region lands in the wrong place
    and the preview looks like offset tiled copies of the game. Best
    effort only — any failure is ignored (setting Windows display
    scaling to 100% is the manual fallback, see SETUP_zh.md).
    """
    try:
        import ctypes
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def find_window_client_rect(title_substr):
    """Find a visible top-level window whose title contains title_substr
    (case-insensitive) and return its client area in screen coordinates
    as (left, top, right, bottom). Returns (None, [seen titles]) if not
    found. Windows run time only."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found, seen = [], []

    CB = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    enum_proc_type = CB(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        if title:
            seen.append(title)
        if title_substr.lower() in title.lower():
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                pt = wintypes.POINT(rect.left, rect.top)
                pt2 = wintypes.POINT(rect.right, rect.bottom)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                user32.ClientToScreen(hwnd, ctypes.byref(pt2))
                if pt2.x > pt.x and pt2.y > pt.y:
                    found.append((pt.x, pt.y, pt2.x, pt2.y))
                    return False
        return True

    user32.EnumWindows(enum_proc_type(_cb), 0)
    return (found[0] if found else None), seen


def f8_pressed():
    """True while F8 is held (polled). Windows run time only."""
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_F8) & 0x8000)


# ------------------------------------------------------- capture backends
class CaptureError(RuntimeError):
    pass


class DxcamBackend:
    name = "dxcam"

    def __init__(self, region):
        import dxcam  # ImportError handled by factory
        self.cam = dxcam.create(output_color="BGR")
        if self.cam is None:
            raise CaptureError("dxcam.create() returned None")
        self.region = region

    def grab(self):
        return self.cam.grab(region=self.region)

    def close(self):
        try:
            self.cam.stop()
        except Exception:
            pass


class MssBackend:
    name = "mss"

    def __init__(self, region):
        import mss  # ImportError handled by factory
        try:
            self.sct = mss.MSS()
        except AttributeError:      # older mss without the MSS alias
            self.sct = mss.mss()
        left, top, right, bottom = region
        self.monitor = {"left": left, "top": top,
                        "width": right - left, "height": bottom - top}

    def grab(self):
        img = self.sct.grab(self.monitor)
        return np.asarray(img)[:, :, :3]     # BGRA -> BGR

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


# The tracker only ever reads two thin horizontal bands (probe ~0.639-
# 0.672, judge ~0.785-0.819 of the frame height). Capturing just the
# strip y 0.60-0.86 and pasting it into a black full-size canvas keeps
# every downstream coordinate identical while cutting capture cost to
# roughly a quarter — this is what makes the mss fallback fast enough
# (~19 fps full-frame -> several times that) on machines where dxcam's
# DXGI duplication is unsupported by the graphics driver.
STRIP_TOP_R, STRIP_BOT_R = 0.60, 0.86


class StripBackend:
    """Wrap a backend that was created with the strip region (see
    main): paste each grabbed strip into a black full-size canvas so
    the tracker keeps working in full-frame coordinates."""

    def __init__(self, inner, client_rect):
        left, top, right, bottom = client_rect
        self.W, self.H = right - left, bottom - top
        self.y0 = int(STRIP_TOP_R * self.H)
        self.y1 = int(STRIP_BOT_R * self.H)
        self.inner = inner
        self.name = f"{inner.name}+strip"

    def grab(self):
        s = self.inner.grab()
        if s is None:
            return None
        canvas = np.zeros((self.H, self.W, 3), dtype=np.uint8)
        h = min(s.shape[0], self.y1 - self.y0)
        w = min(s.shape[1], self.W)
        canvas[self.y0:self.y0 + h, :w] = s[:h, :w]
        return canvas

    def close(self):
        self.inner.close()


def make_backend(preference, region):
    """Create a capture backend: dxcam first, mss as fallback. Clear
    install instructions and a clean exit if neither is available."""
    errors = []
    order = ["dxcam", "mss"] if preference == "auto" else [preference]
    for name in order:
        try:
            if name == "dxcam":
                return DxcamBackend(region)
            return MssBackend(region)
        except ImportError as e:
            errors.append(f"{name}: not installed ({e})")
        except Exception as e:
            errors.append(f"{name}: failed to start ({e})")
    print("ERROR: no usable screen-capture backend.\n  " +
          "\n  ".join(errors) +
          "\nInstall the preferred backend (dxcam, fastest on Windows):"
          "\n    pip install dxcam"
          "\n...or the fallback:"
          "\n    pip install mss"
          "\n(or simply: pip install -r requirements.txt)",
          file=sys.stderr)
    sys.exit(2)


# ------------------------------------------------------- frame dump
class FrameDumper:
    """Diagnostic recorder for --dump-frames PATH.

    Writes every frame fed to the tracker (the full canvas the
    capture backend hands over, strip-composited black borders
    included, at whatever size the window has this run) to a
    high-bitrate mp4, plus a sidecar "PATH.times.csv" with one row
    per WRITTEN frame:

        frame_index,tracker_t_seconds

    tracker_t_seconds is the same monotonic clock the touch-log CSV
    uses (t = now - t_start in the main loop), so a live run can be
    replayed offline through the exact pixels the tracker saw. Why:
    the v0.8.1 self-check could not reproduce live5's hold-upgrade
    failures from device-framebuffer recordings; the prime suspect
    is a systematic difference between those pixels and the ones
    mss+strip actually delivers from the MuMu window, and this
    recorder captures the latter verbatim.

    The mp4 container fps is nominal (the live loop's ~60); the
    authoritative timing is the sidecar. Codec: FFV1 first
    (lossless — the replayed pixels are bit-identical to the ones
    the tracker saw), then avc1/H264/mp4v as fallbacks for OpenCV
    builds without it. Odd frame sizes are padded with black
    rows/columns at the bottom/right to even dimensions (MPEG-style
    codecs silently crop 489 -> 488 otherwise); a replay must crop
    the padding back off — the true size is the run's window size.
    A strip-mode dump at ~1060x489 costs roughly 0.1 MB/frame with
    FFV1 (the canvas is mostly black), i.e. of the order of 1 GB
    per song. Writing runs on a background thread behind a bounded
    queue — the capture loop only copies the frame and enqueues
    it. If the queue fills, frames are dropped and counted
    (reported in the summary). Any failure (no codec, bad path,
    disk error) disables the dumper with a warning; the run itself
    is never affected.
    """

    FPS = 60.0
    QUEUE_MAX = 600             # ~10 s of frames buffered in RAM
    CODECS = ("FFV1", "avc1", "H264", "mp4v")

    def __init__(self, path, fps=None, codec=None):
        self.path = path
        self.times_path = path + ".times.csv"
        self.fps = fps or self.FPS
        self.codec = None
        self._force_codec = codec   # tests: pin one codec
        self._q = queue.Queue(maxsize=self.QUEUE_MAX)
        self._thread = None
        self._writer = None
        self._size = None
        self._times = []            # (frame_index, t) actually written
        self.written = 0
        self.dropped = 0
        self.error = None           # first failure, for the summary
        self._failed = False
        self._lock = threading.Lock()

    # -- producer side (capture loop thread) --------------------------
    def dump(self, frame, t):
        """Enqueue one frame (already a private copy owned by the
        dumper). Never blocks: on a full queue, a size mismatch or
        after a failure, the frame is dropped and counted."""
        if self._failed:
            with self._lock:
                self.dropped += 1
            return
        if self._size is not None and \
                (frame.shape[1], frame.shape[0]) != self._size:
            with self._lock:
                self.dropped += 1
            return
        try:
            self._q.put_nowait((frame, t))
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run,
                                            daemon=True)
            self._thread.start()

    # -- consumer side (writer thread) --------------------------------
    def _open_writer(self, W, H):
        tags = (self._force_codec,) if self._force_codec \
            else self.CODECS
        for tag in tags:
            try:
                w = cv2.VideoWriter(self.path,
                                    cv2.VideoWriter_fourcc(*tag),
                                    self.fps, (W, H))
            except Exception:
                continue
            if w is not None and w.isOpened():
                self.codec = tag
                return w
            if w is not None:
                w.release()
        return None

    @staticmethod
    def _pad_even(frame):
        """Pad odd dimensions with black at the bottom/right so
        the stored size is even (see class docstring)."""
        h, w = frame.shape[:2]
        if h % 2 or w % 2:
            frame = np.pad(frame, ((0, h % 2), (0, w % 2), (0, 0)))
        return frame

    def _fail(self, msg):
        if not self._failed:
            self._failed = True
            self.error = msg
            print(f"WARNING: frame dump disabled: {msg}",
                  file=sys.stderr)

    def _run(self):
        idx = 0
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                frame, t = item
                if self._writer is None and not self._failed:
                    padded = self._pad_even(frame)
                    self._writer = self._open_writer(
                        padded.shape[1], padded.shape[0])
                    if self._writer is None:
                        self._fail("could not open a video writer "
                                   f"for {self.path}")
                    else:
                        self._size = (frame.shape[1], frame.shape[0])
                if self._failed:
                    with self._lock:
                        self.dropped += 1
                    continue
                if (frame.shape[1], frame.shape[0]) != self._size:
                    with self._lock:
                        self.dropped += 1
                    continue
                try:
                    self._writer.write(self._pad_even(frame))
                except Exception as e:
                    self._fail(f"frame write failed: {e}")
                    with self._lock:
                        self.dropped += 1
                    continue
                self._times.append((idx, t))
                idx += 1
        finally:
            if self._writer is not None:
                try:
                    self._writer.release()
                except Exception:
                    pass
                self._writer = None
            with self._lock:
                self.written = idx

    def close(self):
        """Flush the queue, finish the mp4 and write the times
        sidecar. Never raises; returns a stats dict."""
        try:
            if self._thread is not None:
                while True:
                    try:
                        self._q.put(None, timeout=5.0)
                        break
                    except queue.Full:
                        if not self._thread.is_alive():
                            break
                self._thread.join(timeout=30.0)
                if self._thread.is_alive() and self.error is None:
                    self.error = ("writer thread still busy after "
                                  "30 s; dump may be truncated")
            if self._times:
                with open(self.times_path, "w", newline="") as f:
                    wcsv = csv.writer(f)
                    wcsv.writerow(["frame_index", "tracker_t_seconds"])
                    for i, t in list(self._times):
                        wcsv.writerow([i, f"{t:.6f}"])
            elif not self._failed and self.error is None:
                self.error = "no frames were written"
        except Exception as e:
            if self.error is None:
                self.error = f"frame dump close failed: {e}"
        with self._lock:
            return dict(written=self.written, dropped=self.dropped,
                        error=self.error, codec=self.codec)


# ------------------------------------------------------- preview window
class Preview:
    """OpenCV preview: lane boxes for both bands; a lane flashes while
    its key is (or was just) pressed. Purely observational."""

    def __init__(self):
        self.flash_until = [0.0] * N_LANES

    def note_actions(self, rows):
        now = time.monotonic()
        for r in rows:
            if r["action"] in ("down", "flick"):
                self.flash_until[r["lane"] - 1] = now + 0.15
            elif r["action"] == "move":
                self.flash_until[r["lane"] - 1] = now + 0.15

    def draw(self, frame, tracker):
        img = frame.copy()
        H, W = img.shape[:2]
        for (y0r, y1r, cov) in ((PROBE_Y0R, PROBE_Y1R, tracker.last_probe_cov),
                               (JUDGE_Y0R, JUDGE_Y1R, tracker.last_judge_cov)):
            y0, y1 = int(y0r * H), int(y1r * H)
            e = edges_at_ratio((y0r + y1r) / 2.0)
            for lane in range(N_LANES):
                x0, x1 = int(e[lane] * W), int(e[lane + 1] * W)
                on = cov[lane] >= FRAC_ON
                flash = time.monotonic() < self.flash_until[lane]
                colour = (0, 0, 255) if flash else \
                    ((0, 255, 0) if on else (255, 180, 0))
                cv2.rectangle(img, (x0, y0), (x1, y1), colour, 2)
                cv2.putText(img, str(lane + 1), (x0 + 4, y1 - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)
        small = cv2.resize(img, (min(W, 1170), int(H * min(W, 1170) / W)))
        cv2.imshow("ournotes_bot preview (F8 = stop)", small)
        cv2.waitKey(1)


# ------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Our Notes live player (Windows + MuMu). Lanes 1-6 "
                    "tap with keys D F G H J K; flick notes are played "
                    "with an upward mouse swipe (or Q W E R T Y keys "
                    "with --flick-mode key); stop with F8 or Ctrl+C.")
    ap.add_argument("--window-title", default="MuMu",
                    help="substring of the emulator window title "
                         "(default: 'MuMu')")
    ap.add_argument("--backend", choices=["auto", "dxcam", "mss"],
                    default="auto",
                    help="capture backend (default: auto = dxcam, then mss)")
    ap.add_argument("--lead-ms", type=float, default=60.0,
                    help="latency compensation: fire presses this many "
                         "ms before the predicted arrival (default: 0)")
    ap.add_argument("--flick-lead-ms", type=float, default=None,
                    help="extra lead used ONLY for flick gestures: a "
                         "swipe is recognised by the game once it has "
                         "accumulated displacement, ~40-90 ms after it "
                         "starts, so flicks fire this many ms before "
                         "the predicted arrival (default: 90 with "
                         "--input key; 40 with --input maatouch, whose "
                         "on-device pipeline is shorter)")
    ap.add_argument("--fallback-lead-ms", type=float, default=25.0,
                    help="small lead for fallback presses (default: 25)")
    ap.add_argument("--fall-init", type=float, default=0.08,
                    help="initial probe->judge fall time estimate, "
                         "seconds (default: 0.08)")
    ap.add_argument("--dry-run", action="store_true",
                    help="log touches to CSV only; send NO keys")
    ap.add_argument("--preview", action="store_true", default=None,
                    help="show the OpenCV preview window "
                         "(default: on for --dry-run, off for live)")
    ap.add_argument("--no-preview", dest="preview", action="store_false",
                    help="never show the preview window")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop automatically after this many seconds "
                         "(default: 0 = run until F8 / Ctrl+C)")
    ap.add_argument("--csv", default="scheduled_touches_live.csv",
                    help="touch log CSV path "
                         "(default: scheduled_touches_live.csv)")
    ap.add_argument("--dump-frames", default=None, metavar="PATH.mp4",
                    help="diagnostic: record every frame fed to the "
                         "tracker to this mp4 (lossless FFV1 where "
                         "available), plus a PATH.mp4.times.csv "
                         "sidecar giving each written frame's "
                         "tracker timestamp, so the run can be "
                         "replayed offline through the exact pixels "
                         "the tracker saw")
    ap.add_argument("--full-capture", action="store_true",
                    help="capture the whole window instead of only the "
                         "tracker's horizontal strip (slower; diagnostics)")
    ap.add_argument("--flick-mode", choices=["mouse", "key"],
                    default="mouse",
                    help="how flick notes are played: 'mouse' (default) "
                         "performs an upward mouse swipe at the lane's "
                         "judge-line point — no MuMu widget needed; "
                         "'key' presses the lane's Q W E R T Y key for "
                         "a MuMu swipe widget (fallback)")
    ap.add_argument("--flick-duration-ms", type=float,
                    default=FLICK_DURATION_MS,
                    help="mouse flick gesture duration in ms "
                         "(default: 100; raise it if the game ignores "
                         "the swipe as too fast, e.g. 140)")
    ap.add_argument("--flick-dist", type=float, default=FLICK_SWIPE_R,
                    help="mouse flick travel distance as a fraction of "
                         "the window height (default: 0.20)")
    ap.add_argument("--flick-steps", type=int, default=FLICK_STEPS,
                    help="mouse flick movement steps (default: 8)")
    ap.add_argument("--input", choices=["key", "maatouch"],
                    default="key",
                    help="input backend: 'key' (default) sends "
                         "keyboard/mouse via SendInput to the MuMu "
                         "window; 'maatouch' injects real multi-touch "
                         "events on the device through the MaaTouch "
                         "server over ADB — no MuMu key mapping and "
                         "no window focus needed for the touches")
    ap.add_argument("--adb", default="adb",
                    help="adb executable for --input maatouch "
                         "(default: 'adb' from PATH; give a full path "
                         "if platform-tools is not on PATH)")
    ap.add_argument("--adb-addr", default="127.0.0.1:16384",
                    help="ADB address of the emulator for --input "
                         "maatouch (default: 127.0.0.1:16384, the "
                         "MuMu 12 address)")
    ap.add_argument("--maatouch-class", default=MAATOUCH_MAIN_CLASS,
                    help="MaaTouch server main class for app_process "
                         "(default verified against the official "
                         "v1.1.0 server; override only for other "
                         "MaaTouch versions)")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("player_live.py must run on Windows (it captures a MuMu "
              "window and sends keys with SendInput).", file=sys.stderr)
        return 2
    if cv2 is None:
        print("OpenCV is required: pip install opencv-python\n"
              "(or: pip install -r requirements.txt)", file=sys.stderr)
        return 2
    preview_on = args.preview if args.preview is not None else args.dry_run

    flick_lead_ms = args.flick_lead_ms
    if flick_lead_ms is None:
        flick_lead_ms = 40.0 if args.input == "maatouch" else 90.0

    # MaaTouch launcher: at the very start of the main flow, before
    # any capture. A failure aborts the run (no silent key fallback).
    # Dry-run never touches the device (the maatouch equivalent of
    # key mode sending no keys): the launcher is skipped and touches
    # are only logged.
    session = None
    if args.input == "maatouch":
        if args.dry_run:
            print("input: maatouch requested, but --dry-run never "
                  "touches the device: launcher skipped, touches "
                  "are only logged.")
        else:
            try:
                session = start_maatouch(
                    args.adb, args.adb_addr,
                    main_class=args.maatouch_class)
            except MaaTouchError as e:
                print(f"ERROR: MaaTouch startup failed.\n{e}",
                      file=sys.stderr)
                return 2

    set_dpi_awareness()
    rect, seen = find_window_client_rect(args.window_title)
    if rect is None:
        if session is not None:
            session.close()
        print(f"ERROR: no visible window with title containing "
              f"{args.window_title!r}. Is MuMu running?\n"
              f"Visible window titles seen: {seen[:15]}",
              file=sys.stderr)
        return 2
    print(f"window client area: {rect}")

    region = rect
    if not args.full_capture:
        _l, _t, _r, _b = rect
        region = (_l, _t + int(STRIP_TOP_R * (_b - _t)),
                  _r, _t + int(STRIP_BOT_R * (_b - _t)))
    backend = make_backend(args.backend, region)
    if not args.full_capture:
        backend = StripBackend(backend, rect)
    print(f"capture backend: {backend.name}")

    dumper = None
    if args.dump_frames:
        dumper = FrameDumper(args.dump_frames)
        print(f"frame dump: recording tracker frames to "
              f"{args.dump_frames} (times: {args.dump_frames}.times.csv)")

    adapter = LiveAdapter(args.csv, dry_run=args.dry_run,
                          flick_mode=args.flick_mode,
                          flick_dist=args.flick_dist,
                          flick_duration_ms=args.flick_duration_ms,
                          flick_steps=args.flick_steps,
                          sender=session.sender if session else None,
                          input_mode=args.input)
    tracker = None
    preview = Preview() if preview_on else None
    frames_done = 0
    grabs = 0
    dump_stats = {}
    t_start = time.monotonic()
    last_csv_len = 0
    if args.dry_run:
        print("mode: DRY-RUN (no keys sent)")
    elif args.input == "maatouch":
        print("mode: LIVE (MaaTouch device touches will be sent)")
    else:
        print("mode: LIVE (keys D F G H J K will be sent)")
    if args.input == "maatouch":
        print("input: maatouch (real device touches via ADB; the "
              "MuMu window only needs to be visible for capture — "
              "keyboard focus and key mapping are irrelevant)")
        print(f"flick gesture (maatouch): {MAATOUCH_FLICK_STEPS} "
              f"steps x ~{MAATOUCH_FLICK_STEP_MS} ms, distance "
              f"{args.flick_dist:g} of device height")
    else:
        print("flick mode:", args.flick_mode,
              "(mouse swipe at the lane)" if args.flick_mode == "mouse"
              else "(Q W E R T Y keys for MuMu swipe widgets)")
        if args.flick_mode == "mouse":
            print(f"flick gesture: {args.flick_duration_ms:g} ms, "
                  f"{args.flick_dist:g} of window height, "
                  f"{args.flick_steps} steps")
    print(f"flick lead: {flick_lead_ms:g} ms "
          f"(taps use --lead-ms {args.lead_ms:g}, "
          f"fallback --fallback-lead-ms {args.fallback_lead_ms:g})")
    print("press F8 or Ctrl+C to stop.")
    try:
        while True:
            now = time.monotonic()
            if f8_pressed():
                print("F8 pressed — stopping.")
                break
            if args.duration and now - t_start >= args.duration:
                print("duration reached — stopping.")
                break
            frame = backend.grab()
            if frame is None:
                time.sleep(0.001)       # dxcam: no new frame yet
                continue
            grabs += 1
            t = now - t_start
            if tracker is None or frame.shape[1] != tracker.W or \
                    frame.shape[0] != tracker.H:
                if tracker is not None:
                    adapter.release_all()
                    print(f"frame size changed to {frame.shape[1]}x"
                          f"{frame.shape[0]}; tracker rebuilt, keys released")
                tracker = DualBandTracker(adapter, frame.shape[1],
                                          frame.shape[0],
                                          lead_ms=args.lead_ms,
                                          fall_time_init=args.fall_init,
                                          flick_lead_ms=flick_lead_ms,
                                          fallback_lead_ms=args.fallback_lead_ms)
                adapter.set_flick_geometry(rect[0], rect[1],
                                           frame.shape[0])
                adapter.set_frame_size(frame.shape[1], frame.shape[0])
            # Copy BEFORE process(): the dump must hold the pixels
            # exactly as fed, whatever the tracker does with them.
            dump_copy = frame.copy() if dumper is not None else None
            tracker.process(t, frame)
            frames_done += 1
            if dumper is not None:
                dumper.dump(dump_copy, t)
            if preview is not None:
                preview.note_actions(adapter.rows[last_csv_len:])
                preview.draw(frame, tracker)
            last_csv_len = len(adapter.rows)
    except KeyboardInterrupt:
        print("Ctrl+C — stopping.")
    finally:
        try:
            if tracker is not None:
                tracker.finish(time.monotonic() - t_start)
        finally:
            adapter.release_all()       # no key is ever left held down
            adapter.close()
            backend.close()
            if dumper is not None:
                dump_stats.update(dumper.close())
            if session is not None:
                session.close()         # MaaTouch shell session
            if preview is not None:
                cv2.destroyAllWindows()
    elapsed = max(time.monotonic() - t_start, 1e-9)
    st = tracker.stats if tracker is not None else {}
    print("---- summary ----")
    print(f"runtime:          {elapsed:.1f}s")
    print(f"capture rate:     {grabs / elapsed:.1f} fps "
          f"({grabs} frames grabbed, {frames_done} processed)")
    print(f"scheduled presses:{adapter.n_down} "
          f"(predicted {st.get('predicted', 0)}, "
          f"fallback {st.get('fallback', 0)})")
    print(f"releases:         {adapter.n_up}, slide moves: {adapter.n_move}, "
          f"holds: {st.get('holds', 0)}")
    print(f"flick actions:    {adapter.n_flick} "
          f"(gold {st.get('flick_gold', 0)}, red {st.get('flick_red', 0)}, "
          f"green {st.get('flick_green', 0)})")
    print(f"touch log:        {args.csv} ({len(adapter.rows)} rows)")
    if dumper is not None:
        print(f"frame dump:       {dump_stats.get('written', 0)} frames "
              f"written, {dump_stats.get('dropped', 0)} dropped "
              f"-> {args.dump_frames} "
              f"(times: {args.dump_frames}.times.csv, "
              f"codec: {dump_stats.get('codec')})")
        if dump_stats.get("error"):
            print(f"frame dump note:  {dump_stats['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
