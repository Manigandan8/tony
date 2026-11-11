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
from urllib.parse import quote
from tony_core import config
from tony_core.net import HTTP
from tony_core.speech import speak, SPEAK_LOCK

_REMINDERS = []

# Explicit timestamp patterns that NEVER match years like 'from 2019' (Audit Part 5)
_HMS_RE = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")
_MS_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_MIN_RE = re.compile(r"\b(\d+)\s*(?:minutes?|mins?|min)\b", re.I)
_SEC_RE = re.compile(r"\b(\d+)\s*(?:seconds?|secs?|sec)\b", re.I)
_HR_RE = re.compile(r"\b(\d+)\s*(?:hours?|hrs?|hr)\b", re.I)


def _parse_timestamp(text: str):
    """
    Fixed Audit Part 5:
    - Parses HH:MM:SS ('1:23:45') before MM:SS ('1:23').
    - Requires explicit time units ('minutes', 'seconds', 'hours') or colon format,
      so 'song from 2019' or 'at 5' is never mistaken for a timestamp.
    """
    t = text or ""
    m_hms = _HMS_RE.search(t)
    if m_hms:
        return int(m_hms.group(1)) * 3600 + int(m_hms.group(2)) * 60 + int(m_hms.group(3))

    m_ms = _MS_RE.search(t)
    if m_ms:
        return int(m_ms.group(1)) * 60 + int(m_ms.group(2))

    total = 0
    hh = _HR_RE.search(t)
    mm = _MIN_RE.search(t)
    ss = _SEC_RE.search(t)
    if hh:
        total += int(hh.group(1)) * 3600
    if mm:
        total += int(mm.group(1)) * 60
    if ss:
        total += int(ss.group(1))
    return total if total > 0 else None


def _strip_timestamp(text: str) -> str:
    """Fixed Audit Part 5: Strips only actual timestamp expressions and preserves query case."""
    t = text or ""
    t = _HMS_RE.sub(" ", t)
    t = _MS_RE.sub(" ", t)
    t = re.sub(
        r"\b(?:starting\s+(?:from|at)|start\s+(?:from|at)|from|at)?\s*"
        r"(?:\d+\s*(?:hours?|hrs?|hr|minutes?|mins?|min|seconds?|secs?|sec)\s*(?:and\s*)?)+",
        " ",
        t,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", t).strip()


def set_reminder(minutes: float = 5, text: str = "Reminder") -> str:
    try:
        mins = max(0.1, min(float(minutes), 1440))
    except Exception:
        return "Give minutes as a number."

    def fire():
        with SPEAK_LOCK:
            try:
                speak(f"Reminder, sir: {text}")
            except Exception:
                pass

    t = threading.Timer(mins * 60, fire)
    t.daemon = True
    t.start()
    _REMINDERS.append(t)
    return f"Reminder set: '{text}' in {mins:g} minutes."


def read_notes(count: int = 10) -> str:
    p = os.path.join(config.TONY_HOME, "notes.txt")
    if not os.path.isfile(p):
        return "You have no saved notes yet."
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
        if not lines:
            return "Your notes file is empty."
        recent = lines[-max(1, min(int(count or 10), 30)):]
        return "Your recent notes:\n" + "\n".join(recent)
    except Exception as e:
        return f"Couldn't read notes: {str(e)[:60]}"


def take_note(text: str) -> str:
    clean = (text or "").strip()
    if not clean or clean.lower() in ("read", "show", "list", "read notes", "show notes"):
        return read_notes()
    p = os.path.join(config.TONY_HOME, "notes.txt")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M} — {clean}\n")
        return f"Noted: '{clean[:60]}'."
    except Exception as e:
        return f"Note failed: {str(e)[:60]}"


def weather(city: str = "") -> str:
    """Fixed Audit Part 5: URL-encodes city name and validates HTTP status code."""
    c = (city or config.TONY_CITY).strip()
    if not c:
        return "Which city? Say e.g. 'weather in Chennai', or set TONY_CITY in .env."
    try:
        encoded_city = quote(c)
        r = HTTP.get(
            f"https://wttr.in/{encoded_city}?format=%l:+%c+%t,+feels+like+%f,+humidity+%h,+wind+%w",
            timeout=10,
            headers={"User-Agent": "curl/8.0"},
        )
        txt = (r.text or "").strip()
        if r.status_code != 200 or not txt or "unknown location" in txt.lower() or "<html" in txt.lower():
            return f"Couldn't find weather data for '{c}'. Do NOT claim success."
        return txt[:220]
    except Exception as e:
        return f"Weather failed: {str(e)[:60]}"