# =====================================================================
# tony_core/safety.py
# Security gate, path access verification, confirmation checks, action logging.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - CTX: threading.local context (holds remote flag for Telegram)
# - TURN: dict tracking actions and failures for the current turn
# - APPROVED: dict of path approvals and timestamps
# - TONY_BUSY: threading.Lock preventing concurrent execution of commands
# =====================================================================

import os
import re
import time
import datetime
import threading
from tony_core import config

CTX = threading.local()
TURN = {"actions": [], "failures": [], "msg": ""}
APPROVED = {}
TONY_BUSY = threading.Lock()

def _remote():
    return getattr(CTX, "remote", False)

def log_action(name, args, note=""):
    try:
        safe = {k: (str(v) if len(str(v)) < 80 else str(v)[:77] + "...")
                for k, v in (args or {}).items() if k != "content"}
        with open(config.ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {'[phone] ' if _remote() else ''}{name} {safe} {note}\n")
    except Exception:
        pass

_YES = {"yes", "yeah", "yep", "yup", "sure", "confirm", "proceed", "okay", "ok", "affirmative", "y"}

def confirm(question: str) -> bool:
    if _remote(): return False
    print(f"  [CONFIRM] {question}")
    try:
        from tony_core.speech import speak
        from tony_core.ears import listen
        speak(f"Confirm: {question}")
        ans = input("  Allow? (y/N, Enter = speak): ").strip().lower()
        if not ans and config.VOICE_INPUT:
            ans = (listen() or "").lower()
    except (EOFError, KeyboardInterrupt):
        return False
    words = set(re.findall(r"[a-z]+", ans))
    if words & {"no", "nope", "cancel", "stop", "don", "dont", "never"}:
        return False
    return bool(words & _YES) or "go ahead" in ans or "do it" in ans

def _normpath(p):
    return os.path.abspath(p).lower().replace("/", "\\")

_HOME = os.path.expanduser("~")
SAFE_WRITE_DIRS = [_normpath(os.path.join(_HOME, d)) for d in ("Documents", "Desktop", "Downloads")] + [_normpath(config.TONY_HOME)]
PROTECTED_FILES = {_normpath(os.path.join(config.HERE, n)) for n in
                   ("tony.py", "routines.json", ".env", "tony_facts.json", "tony_memory.json", "tony_actions.log")}
BLOCKED_PARTS = ("\\start menu\\programs\\startup", "\\windows\\", "\\system32", "\\program files",
                 "\\.ssh\\", "\\appdata\\roaming\\microsoft\\windows")
RISKY_EXT = (".bat", ".cmd", ".ps1", ".vbs", ".reg", ".exe", ".dll", ".lnk", ".msi", ".scr", ".hta", ".pyw")

def _path_verdict(p):
    low = _normpath(p)
    if low in PROTECTED_FILES or any(x in low for x in BLOCKED_PARTS):
        return "block"
    inside = any(low.startswith(d + "\\") for d in SAFE_WRITE_DIRS)
    if not inside or os.path.splitext(low)[1] in RISKY_EXT:
        return "confirm"
    return "ok"

def _resolve_path(path):
    p = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
    return os.path.join(config.TONY_HOME, p) if not os.path.isabs(p) else p

def _write_gate(p):
    v = _path_verdict(p)
    if v == "block":
        return "Blocked: that location is protected. Tell the user plainly; do NOT try another route."
    if v == "confirm" and time.time() - APPROVED.get(_normpath(p), 0) > 600:
        if not confirm(f"write {os.path.basename(p)} into {os.path.dirname(p)}?"):
            return "The user did not approve that write. It was cancelled; do NOT retry."
        APPROVED[_normpath(p)] = time.time()
    return None

READONLY_CMD = re.compile(r"^\s*(dir|echo|ipconfig|whoami|hostname|ver|tasklist|systeminfo|where|"
                          r"git (status|log|branch|diff)|php -v|node -v|python --version|npm -v)\b", re.I)
HARD_BLOCK = re.compile(r"\b(format\s+[a-z]:|diskpart|bcdedit|cipher\s+/w|reg\s+delete|"
                        r"(del|erase|rd|rmdir)\b.*\b[a-z]:\\\s*$|(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\)", re.I)

def _cmd_needs_confirm(c):
    return bool(re.search(r"[&|;<>`\n^%]", c)) or not READONLY_CMD.match(c)

_FAIL_MARKERS = ("Do NOT claim success", "No folder", "No such file", "not found",
                 "Couldn't", "Blocked", "did not approve", "Create failed", "Write failed",
                 "Append failed", "failed", "unavailable")
