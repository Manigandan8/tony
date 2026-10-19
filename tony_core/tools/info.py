# =====================================================================
# tony_core/tools/info.py
# Information tools: weather, reminders, notes, timestamp parsing.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - _REMINDERS: list of active reminder timer threads
# =====================================================================

import os
import re
import datetime
import threading
from tony_core import config
from tony_core.net import HTTP
from tony_core.speech import speak, SPEAK_LOCK

_REMINDERS = []

def _parse_timestamp(text: str):
    t = text.lower()
    m = re.search(r'(\d{1,2}):(\d{1,2})', t)
    if m: return int(m.group(1)) * 60 + int(m.group(2))
    total = 0
    mm = re.search(r'(\d+)\s*(?:minutes?|mins?|min\.?)\b', t)
    ss = re.search(r'(\d+)\s*(?:seconds?|secs?|sec)\b', t)
    if mm: total += int(mm.group(1)) * 60
    if ss: total += int(ss.group(1))
    if total == 0:
        b = re.search(r'(?:from|at|start(?:ing)? (?:from|at)?)\s+(\d{1,4})\b', t)
        if b: total = int(b.group(1))
    return total if total > 0 else None

def _strip_timestamp(text: str) -> str:
    t = text.lower()
    t = re.sub(r'\d{1,2}:\d{1,2}', ' ', t)
    t = re.sub(r'\b(?:from|at|start(?:ing)?(?: from| at)?)\s+\d{1,4}\s*(?:minutes?|mins?|min\.?|seconds?|secs?|sec)?\b', ' ', t)
    t = re.sub(r'\d{1,4}\s*(?:minutes?|mins?|min\.?|seconds?|secs?|sec)\b', ' ', t)
    t = re.sub(r'\b(?:start|starting|begin|from|at)\b', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()

def set_reminder(minutes: float = 5, text: str = "Reminder") -> str:
    try: mins = max(0.1, min(float(minutes), 1440))
    except Exception: return "Give minutes as a number."
    def fire():
        with SPEAK_LOCK:
            try: speak(f"Reminder, sir: {text}")
            except Exception: pass
    t = threading.Timer(mins * 60, fire)
    t.daemon = True; t.start()
    _REMINDERS.append(t)
    return f"Reminder set: '{text}' in {mins:g} minutes."

def take_note(text: str) -> str:
    p = os.path.join(config.TONY_HOME, "notes.txt")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M} — {text}\n")
        return f"Noted: '{text[:60]}'."
    except Exception as e: return f"Note failed: {str(e)[:60]}"

def weather(city: str = "") -> str:
    c = (city or config.TONY_CITY).strip()
    if not c: return "Which city? Say e.g. 'weather in Chennai', or set TONY_CITY in .env."
    try:
        r = HTTP.get(f"https://wttr.in/{c}?format=%l:+%c+%t,+feels+like+%f,+humidity+%h,+wind+%w",
                     timeout=10, headers={"User-Agent": "curl"})
        return (r.text.strip() or "No data.")[:200]
    except Exception as e: return f"Weather failed: {str(e)[:60]}"
