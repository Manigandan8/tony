# =====================================================================
# tony_core/telegram_link.py
# Telegram remote bridge polling loop.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - None
# =====================================================================

import time
from tony_core import config
from tony_core.net import HTTP
from tony_core import safety
from tony_core import brain
from tony_core import memory

def _telegram_poll():
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID): return
    offset, url = 0, f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    print("[phone] Telegram remote active")
    while True:
        try:
            r = HTTP.get(f"{url}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=30)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID): continue
                text = (msg.get("text") or "").strip()
                if not text: continue
                print(f"\n[phone] {text}")
                if not safety.TONY_BUSY.acquire(blocking=False):
                    HTTP.post(f"{url}/sendMessage", json={"chat_id": config.TELEGRAM_CHAT_ID, "text": "Busy right now."}, timeout=10)
                    continue
                safety.CTX.remote = True
                try: ans = brain.ask_tony(text)
                finally: safety.CTX.remote = False; safety.TONY_BUSY.release()
                HTTP.post(f"{url}/sendMessage", json={"chat_id": config.TELEGRAM_CHAT_ID, "text": memory.core_reply(ans)}, timeout=10)
        except Exception: time.sleep(5)
