# =====================================================================
# tony_core/safety.py
# Security gate, path access verification, confirmation checks, action logging.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - CTX: threading.local context (holds remote flag for Telegram)
# - TURN: dict tracking actions and failures for the current turn
# - APPROVED: dict of path approvals and timestamps
# - TONY_BUSY: threading.Lock preventing concurrent execution of commands
# - PENDING_CONFIRM: dict coordinating keyboard/voice confirmation across threads
# =====================================================================

import os
import re
import time
import datetime
import threading
from tony_core import config
from tony_core import triggers

CTX = threading.local()
TURN = {"actions": [], "failures": [], "msg": "", "executed_calls": {}, "last_output": ""}
APPROVED = {}
TONY_BUSY = threading.Lock()
PENDING_CONFIRM = {"active": False, "text": "", "event": threading.Event()}

REMOTE_ALLOW_TOOLS = {
    "web_search", "calculator_math", "weather", "system_status", "top_processes",
    "active_window_info", "look_at_screen", "verify_screen", "set_reminder",
    "take_note", "media_control", "set_volume", "list_files", "find_anywhere",
    "recent_pc_activity"
}


def _remote():
    return getattr(CTX, "remote", False)


def supply_confirm_input(text: str):
    """Called by app.py main loop when user types y/n in console while a background confirm() is waiting."""
    PENDING_CONFIRM["text"] = (text or "").strip().lower()
    PENDING_CONFIRM["event"].set()


def log_action(name, args, note=""):
    try:
        safe = {
            k: (str(v) if len(str(v)) < 80 else str(v)[:77] + "...")
            for k, v in (args or {}).items() if k != "content"
        }
        with open(config.ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {'[phone] ' if _remote() else ''}{name} {safe} {note}\n")
    except Exception:
        pass


_YES = {"yes", "yeah", "yep", "yup", "sure", "confirm", "proceed", "okay", "ok", "affirmative", "y", "allow"}
_NO = {"no", "nope", "cancel", "stop", "don", "dont", "never", "deny", "abort", "n"}
_POWER_WORDS = ("shut down", "shutdown", "restart", "reboot")


def confirm(question: str) -> bool:
    if _remote():
        return False
    if triggers.is_privileged():
        low = (question or "").lower()
        if not any(w in low for w in _POWER_WORDS):
            return True

    print(f"\n  [CONFIRM] {question}")
    print("  >> Say 'yes' or 'no' out loud (or type y/n and press Enter) <<")
    ans = ""
    try:
        from tony_core.speech import speak
        from tony_core import ears
        is_bg_thread = threading.current_thread() is not threading.main_thread()

        PENDING_CONFIRM["text"] = ""
        PENDING_CONFIRM["event"].clear()
        PENDING_CONFIRM["active"] = True
        try:
            # Never arm barge-in during a confirmation prompt!
            ears.BARGE_IN["triggered"] = False
            ears.BARGE_IN["pre_roll"] = []
            speak(f"Confirm: {question}", allow_barge_in=False)

            if PENDING_CONFIRM["event"].is_set():
                ans = PENDING_CONFIRM["text"]
            elif is_bg_thread and config.VOICE_INPUT:
                voice_ans = (ears.listen(max_seconds=5, wait_seconds=6, is_followup=True) or "").lower()
                # If user typed y/yes in console while mic was listening, prefer the typed input!
                ans = PENDING_CONFIRM["text"] if PENDING_CONFIRM["event"].is_set() else voice_ans
            elif not is_bg_thread:
                ans = input("  Allow? (y/N, Enter = speak): ").strip().lower()
                if not ans and config.VOICE_INPUT:
                    ans = (ears.listen(max_seconds=5, wait_seconds=6, is_followup=True) or "").lower()
        finally:
            PENDING_CONFIRM["active"] = False
    except (EOFError, KeyboardInterrupt, Exception):
        PENDING_CONFIRM["active"] = False
        return False

    words = set(re.findall(r"[a-z]+", ans))
    if words & _NO:
        return False
    return bool(words & _YES) or "go ahead" in ans or "do it" in ans or ans.startswith("y")


def _normpath(p):
    return os.path.abspath(p).lower().replace("/", "\\")


def _shell_folder_safe(reg_name: str, fallback: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        ) as k:
            val, _ = winreg.QueryValueEx(k, reg_name)
            p = os.path.expandvars(val)
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    return fallback


_HOME = os.path.expanduser("~")
_REAL_FOLDERS = [
    _shell_folder_safe("{374DE290-123F-4565-9164-39C4925E467B}", os.path.join(_HOME, "Downloads")),
    _shell_folder_safe("Personal", os.path.join(_HOME, "Documents")),
    _shell_folder_safe("Desktop", os.path.join(_HOME, "Desktop")),
    os.path.join(_HOME, "Downloads"),
    os.path.join(_HOME, "Documents"),
    os.path.join(_HOME, "Desktop"),
    config.TONY_HOME,
]
SAFE_WRITE_DIRS = list({_normpath(d) for d in _REAL_FOLDERS if d})

_HERE_NORM = _normpath(config.HERE)
_TONY_HOME_NORM = _normpath(config.TONY_HOME)

PROTECTED_FILES = {
    _normpath(os.path.join(config.HERE, n))
    for n in ("tony.py", "routines.json", ".env", "tony_facts.json", "tony_memory.json", "tony_actions.log")
}
BLOCKED_PARTS = (
    "\\start menu\\programs\\startup", "\\windows\\", "\\system32", "\\program files",
    "\\.ssh\\", "\\appdata\\roaming\\microsoft\\windows", "\\tony_core\\"
)
RISKY_EXT = (".bat", ".cmd", ".ps1", ".vbs", ".reg", ".exe", ".dll", ".lnk", ".msi", ".scr", ".hta", ".pyw")


def _path_verdict(p):
    low = _normpath(p)
    if low in PROTECTED_FILES or any(x in low for x in BLOCKED_PARTS):
        return "block"
    if (low == _HERE_NORM or low.startswith(_HERE_NORM + "\\")) and not low.startswith(_TONY_HOME_NORM + "\\"):
        return "block"
    inside = any(low == d or low.startswith(d + "\\") for d in SAFE_WRITE_DIRS)
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
    if v == "confirm":
        if triggers.is_privileged() and not _remote():
            APPROVED[_normpath(p)] = time.time()
            log_action("write_gate", {"path": str(p)}, "[privileged auto-approve]")
            return None
        if time.time() - APPROVED.get(_normpath(p), 0) > 600:
            if not confirm(f"write {os.path.basename(p)} into {os.path.dirname(p)}?"):
                return "The user did not approve that write. It was cancelled; do NOT retry."
            APPROVED[_normpath(p)] = time.time()
    return None


READONLY_CMD = re.compile(
    r"^\s*(dir|echo|ipconfig|whoami|hostname|ver|tasklist|systeminfo|where|"
    r"git (status|log|branch|diff)|php -v|node -v|python --version|npm -v)\b",
    re.I
)

HARD_BLOCK = re.compile(
    r"\b(format\s+[a-z]:|diskpart|bcdedit|cipher\s+/w|reg\s+(delete|add)|vssadmin|wbadmin|takeown|icacls|"
    r"remove-item\b.*(-recurse|-force)|powershell\b.*-(e|enc|encodedcommand)\b|"
    r"(del|erase|rd|rmdir)\b.*\b[a-z]:\\(users|windows)?\s*$|(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\)",
    re.I
)


def _cmd_needs_confirm(c):
    return bool(re.search(r"[&|;<>`\n^%]", c)) or not READONLY_CMD.match(c)


_FAIL_MARKERS = (
    "Do NOT claim success", "No folder", "No such file", "not found",
    "Couldn't", "Blocked", "did not approve", "Cancelled", "Refused",
    "Create failed", "Write failed", "Append failed", "failed", "unavailable",
    "Unknown tool", "Tool error"
)