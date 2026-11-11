# =====================================================================
# tony_core/telegram_link.py
# Telegram remote bridge polling loop.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - None
# =====================================================================

import os
import time
from tony_core import config
from tony_core.net import HTTP
from tony_core import safety
from tony_core import brain
from tony_core import memory

_OFFSET_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", config.TONY_HOME), "Tony", "telegram_offset.txt"
)


def _load_offset() -> int:
    try:
        if os.path.isfile(_OFFSET_FILE):
            with open(_OFFSET_FILE, encoding="utf-8") as f:
                return int(f.read().strip() or "0")
    except Exception:
        pass
    return 0


def _save_offset(offset: int):
    try:
        os.makedirs(os.path.dirname(_OFFSET_FILE), exist_ok=True)
        with open(_OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(offset)))
    except Exception:
        pass


def _telegram_poll():
    """
    Fixed Audit 2.13:
    - Persists update offset across restarts and skips stale pre-boot messages.
    - Checks HTTP status codes (e.g. 409 Conflict) and sleeps so it never spins at 100% CPU.
    """
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    offset = _load_offset()
    boot_ts = time.time()
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    print("[phone] Telegram remote active")

    while True:
        try:
            r = HTTP.get(f"{url}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=32)
            if r.status_code == 409:
                print("  [phone] Telegram 409 Conflict (another instance is polling) — sleeping 15s")
                time.sleep(15)
                continue
            if r.status_code != 200:
                print(f"  [phone] Telegram poll HTTP {r.status_code} — sleeping 5s")
                time.sleep(5)
                continue

            updates = (r.json() or {}).get("result") or []
            for u in updates:
                offset = int(u["update_id"]) + 1
                _save_offset(offset)
                msg = u.get("message") or {}
                # Skip stale messages sent more than 30s before Tony booted
                if msg.get("date", boot_ts) < (boot_ts - 30):
                    continue
                if str(msg.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                print(f"\n[phone] {text}")
                if not safety.TONY_BUSY.acquire(blocking=False):
                    HTTP.post(
                        f"{url}/sendMessage",
                        json={"chat_id": config.TELEGRAM_CHAT_ID, "text": "Busy right now."},
                        timeout=10,
                    )
                    continue
                safety.CTX.remote = True
                try:
                    ans = brain.ask_tony(text)
                finally:
                    safety.CTX.remote = False
                    safety.TONY_BUSY.release()
                HTTP.post(
                    f"{url}/sendMessage",
                    json={"chat_id": config.TELEGRAM_CHAT_ID, "text": memory.core_reply(ans)},
                    timeout=10,
                )
        except Exception as e:
            print(f"  [phone] Telegram error: {str(e)[:60]}")
            time.sleep(5)