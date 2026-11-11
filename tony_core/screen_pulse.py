# =====================================================================
# tony_core/screen_pulse.py
# Always-On 1-Second Screen Awareness Engine (<1% CPU, <4MB RAM).
#
# Every 1.0 second:
# - Captures the live screen into a single rolling slot (no RAM growth)
# - Computes a 64x36 pixel-diff score to detect screen changes/popups/video
# - Extracts live foreground window + visible on-screen UI text/elements via UIA
# - Keeps a 30-second rolling timeline of what the user was looking at
# - Supports proactive screen watchdogs (e.g. auto-clicking "Skip Ad" or alerting)
# =====================================================================

import io
import time
import threading
from collections import deque
from tony_core import config

PULSE_STATE = {
    "running": False,
    "fps_interval": 1.0,          # Captures screen every 1.0 second
    "latest_pil": None,           # Most recent PIL Image (1 slot in RAM)
    "latest_ts": 0.0,             # Timestamp of latest capture
    "active_window": "unknown",   # Current foreground window title
    "visible_elements": [],       # Live clickable buttons/links/titles on screen right now
    "diff_pct": 0.0,              # % of screen pixels that changed in the last 1 second
    "motion_state": "static",     # 'static', 'changing', or 'video_or_scrolling'
    "vlm_summary": "",            # Optional cached VLM scene description
    "last_vlm_ts": 0.0,
    "auto_skip_ads": False,       # Optional hands-free YouTube ad skipper
    "watch_target": "",           # Optional text/event Tony is watching the screen for
}

# Rolling 15-item timeline of screen states over the last ~60 seconds
SCREEN_TIMELINE = deque(maxlen=15)
_LOCK = threading.Lock()


def _grab_fast_pil():
    """Captures the primary monitor in ~4ms using mss."""
    from PIL import Image
    try:
        import mss
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception:
        from PIL import ImageGrab
        return ImageGrab.grab()


def _thumb_bytes(pil_img):
    """Creates a tiny 64x36 grayscale byte array in 0.2ms for ultra-fast pixel diffing."""
    small = pil_img.resize((64, 36)).convert("L")
    return small.tobytes()


def _calc_diff_pct(b1: bytes, b2: bytes) -> float:
    if not b1 or not b2 or len(b1) != len(b2):
        return 100.0
    changed = sum(1 for x, y in zip(b1, b2) if abs(x - y) > 12)
    return round((changed / len(b1)) * 100.0, 1)


def _fast_uia_snapshot():
    """Reads the active window title and top visible text/buttons on screen in <15ms (0% GPU/API cost)."""
    win_title = "Desktop"
    visible_items = []
    try:
        import pyautogui
        w = pyautogui.getActiveWindow()
        if w and w.title:
            win_title = w.title.strip()[:85]
    except Exception:
        pass

    try:
        from pywinauto import Desktop
        top_w = Desktop(backend="uia").top_window()
        deadline = time.perf_counter() + 0.035  # Strict 35ms budget so CPU stays under 1%!
        seen = set()
        for el in top_w.descendants():
            if time.perf_counter() > deadline or len(visible_items) >= 18:
                break
            try:
                ct = el.element_info.control_type
                if ct not in ("Hyperlink", "Button", "TabItem", "ListItem", "Edit", "MenuItem"):
                    continue
                if not el.is_visible():
                    continue
                name = (el.window_text() or "").strip()
                if len(name) < 2 or name.lower() in seen:
                    continue
                seen.add(name.lower())
                visible_items.append(name[:60])
            except Exception:
                continue
    except Exception:
        pass

    return win_title, visible_items


def _pulse_loop():
    time.sleep(2.5)  # Let boot complete first
    prev_thumb = None
    last_timeline_win = ""
    print("[screen-pulse] 1-second live screen vision active (<1% CPU)")

    while PULSE_STATE["running"]:
        t0 = time.perf_counter()
        try:
            img = _grab_fast_pil()
            curr_thumb = _thumb_bytes(img)
            diff_pct = _calc_diff_pct(prev_thumb, curr_thumb)
            prev_thumb = curr_thumb

            win_title, ui_items = _fast_uia_snapshot()

            if diff_pct < 1.5:
                m_state = "static"
            elif diff_pct > 22.0:
                m_state = "video_or_scrolling"
            else:
                m_state = "changing"

            with _LOCK:
                PULSE_STATE["latest_pil"] = img
                PULSE_STATE["latest_ts"] = time.time()
                PULSE_STATE["active_window"] = win_title
                if ui_items:
                    PULSE_STATE["visible_elements"] = ui_items
                PULSE_STATE["diff_pct"] = diff_pct
                PULSE_STATE["motion_state"] = m_state

                # Record window transitions in the 60-second screen timeline
                if win_title and win_title != last_timeline_win and "tony" not in win_title.lower():
                    stamp = time.strftime("%H:%M:%S")
                    SCREEN_TIMELINE.appendleft(f"[{stamp}] {win_title}")
                    last_timeline_win = win_title

            # Optional Proactive Watchdog 1: Auto-click "Skip Ad" on YouTube
            if PULSE_STATE.get("auto_skip_ads") and "youtube" in win_title.lower():
                if any("skip ad" in item.lower() or item.lower() == "skip" for item in ui_items):
                    try:
                        from tony_core.tools import ui
                        ui.click_screen(app="chrome", element="Skip")
                    except Exception:
                        pass

            # Optional Proactive Watchdog 2: Alert user when a watched text/event appears on screen
            watch_q = (PULSE_STATE.get("watch_target") or "").lower().strip()
            if watch_q:
                blob = (win_title + " " + " ".join(ui_items)).lower()
                if watch_q in blob:
                    PULSE_STATE["watch_target"] = ""
                    try:
                        from tony_core import speech
                        speech.say_async(f"Hey, '{watch_q}' just appeared on your screen!")
                    except Exception:
                        pass

        except Exception:
            pass

        elapsed = time.perf_counter() - t0
        sleep_s = max(0.2, float(PULSE_STATE["fps_interval"]) - elapsed)
        time.sleep(sleep_s)


def start_screen_pulse():
    if PULSE_STATE["running"]:
        return
    PULSE_STATE["running"] = True
    t = threading.Thread(target=_pulse_loop, name="tony-screen-pulse", daemon=True)
    t.start()


def stop_screen_pulse():
    PULSE_STATE["running"] = False


def get_latest_frame():
    """Returns the pre-captured PIL screenshot from the last 1 second (0ms capture delay!)."""
    with _LOCK:
        if PULSE_STATE["latest_pil"] is not None and (time.time() - PULSE_STATE["latest_ts"]) < 2.5:
            return PULSE_STATE["latest_pil"].copy()
    return _grab_fast_pil()


def live_screen_telemetry() -> str:
    """
    Injected into Tony's system_prompt() on EVERY turn so Tony always sees
    what is on the screen right now and what changed over the last 60 seconds!
    """
    with _LOCK:
        win = PULSE_STATE["active_window"]
        motion = PULSE_STATE["motion_state"]
        diff = PULSE_STATE["diff_pct"]
        items = list(PULSE_STATE["visible_elements"])[:12]
        timeline = list(SCREEN_TIMELINE)[:5]

    items_str = ", ".join(items) if items else "none exposed via UIA"
    hist_str = " -> ".join(timeline) if timeline else "just started"
    return (
        f"LIVE 1-SECOND SCREEN FEED: Active Window = '{win}' (Screen motion: {motion}, {diff}% pixel change/s). "
        f"Visible on-screen controls/links right now: [{items_str}]. "
        f"Recent screen history: {hist_str}."
    )


def watch_screen_for(target_text: str = "", auto_skip_ads: bool = False) -> str:
    """Tool: Tells Tony's 1-second screen pulse to watch for specific text/events or auto-skip ads."""
    PULSE_STATE["auto_skip_ads"] = bool(auto_skip_ads)
    if target_text:
        PULSE_STATE["watch_target"] = target_text.strip()
        return f"I am watching your screen every second and will alert you as soon as '{target_text}' appears."
    if auto_skip_ads:
        return "Enabled automatic YouTube ad skipping on my 1-second screen watcher."
    PULSE_STATE["watch_target"] = ""
    return "Cleared screen watch alert."