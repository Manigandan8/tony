# =====================================================================
# tony_core/app.py
# Application entrypoint: boot sequence, trigger threads, and main loop.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - None
# =====================================================================

import os
import time
import datetime
import threading

from tony_core import config
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core import ears
from tony_core import wake
from tony_core import brain
from tony_core import telegram_link
from tony_core import triggers
from tony_core.tools import ui

from tony_core.providers import (
    groqai, openrouter, zai, github_models,
    mistralai, cerebras, cohereai
)

def _briefing_loop():
    if not config.BRIEFING_TIMES: return
    fired = {}
    print(f"[briefings] scheduled at: {', '.join(config.BRIEFING_TIMES)}")
    while True:
        try:
            now = datetime.datetime.now()
            hm = f"{now:%H:%M}"
            today = f"{now:%Y-%m-%d}"
            if hm in config.BRIEFING_TIMES and fired.get(hm) != today:
                fired[hm] = today
                if not safety.TONY_BUSY.acquire(blocking=False):
                    time.sleep(10)
                    continue
                try:
                    overlay.ui_state("BRIEFING")
                    speech.say_async(f"Sir, it is {hm}. Preparing your scheduled briefing.")
                    prompt = f"Give a concise daily briefing for {now:%A, %B %d, %Y} at {hm}. Mention weather if known, and remind the user of their core priorities. Keep it to max 4 warm sentences."
                    ans = brain.ask_tony(prompt)
                    spoken = memory.core_reply(ans, detail=True)
                    overlay.ui_reply(spoken); speech.speak(spoken)
                finally: safety.TONY_BUSY.release()
        except Exception: pass
        time.sleep(20)

def boot():
    # 1. Model discovery: Groq
    groqai.init_models()

    # 2. Model discovery: OpenRouter
    openrouter.init_models()

    # 3. Provider status prints
    print(f"[brain-zai] models: {zai.ZAI_BRAIN_MODELS if zai.ZAI_API_KEY else 'none (check ZAI_API_KEY)'}")
    print(f"[vision-zai] models: {zai.ZAI_VISION_MODELS if zai.ZAI_API_KEY else 'none (check ZAI_API_KEY)'}")
    print(f"[brain-github] models: {github_models.GITHUB_BRAIN_MODELS if github_models.GITHUB_TOKEN else 'skipped (no GITHUB_TOKEN in .env)'}")
    print(f"[vision-github] models: {github_models.GITHUB_VISION_MODELS if github_models.GITHUB_TOKEN else 'skipped'}")
    print(f"[brain-mistral] models: {mistralai.MISTRAL_BRAIN_MODELS if mistralai.MISTRAL_API_KEY else 'skipped (no MISTRAL_API_KEY in .env)'}")
    print(f"[vision-mistral] models: {mistralai.MISTRAL_VISION_MODELS if mistralai.MISTRAL_API_KEY else 'skipped'}")
    print(f"[brain-cerebras] models: {cerebras.CEREBRAS_BRAIN_MODELS if cerebras.CEREBRAS_API_KEY else 'skipped (no CEREBRAS_API_KEY in .env)'}")
    print(f"[brain-cohere] models: {cohereai.COHERE_BRAIN_MODELS if cohereai.COHERE_API_KEY else 'skipped (no COHERE_API_KEY in .env)'}")
    print(f"[vision-cohere] models: {cohereai.COHERE_VISION_MODELS if cohereai.COHERE_API_KEY else 'skipped'}")

    # 4. Z.ai probe
    if zai.ZAI_API_KEY:
        zai._zai_probe()

    # 5. Memory restoration
    memory.init_memory()

    # 6. STT selection
    ears.init_ears()

def main():
    boot()
    print("Tony ready (overlay + wake + briefings + routines + folders + 9 brains + voice + JARVIS tools).\n")
    ears._get_vad()
    speech.prepare_voice_cache()
    threading.Thread(target=overlay._overlay_app, daemon=True).start()
    time.sleep(1.5)
    speech.say_async("Tony online. All systems nominal.")

    # ---------- TRIGGER THREADS (Audio & Wake Word Seam) ----------
    triggers.init(speak=speech.speak, is_speaking=lambda: speech.SPEAKING["now"], mic_index=ears.MIC_DEVICE_INDEX)
    triggers.start()
    if config.WAKE_WORD_ENABLED:
        time.sleep(0.5)
        threading.Thread(target=wake._wake_loop, daemon=True).start()
    # -------------------------------------------------------------

    threading.Thread(target=_briefing_loop, daemon=True).start()
    threading.Thread(target=telegram_link._telegram_poll, daemon=True).start()
    overlay.ui_state("IDLE")
    overlay.ui_reply("Say 'Hey Jarvis' or type below.")

    while True:
        try:
            msg = input("You (Enter = speak, or type): ").strip()
        except (EOFError, KeyboardInterrupt):
            triggers.stop()
            speech.speak("Going offline. Goodbye, sir.")
            break
        if not msg and config.VOICE_INPUT:
            if not safety.TONY_BUSY.acquire(blocking=False):
                print("  (Tony is busy with a background task)")
                continue
            try:
                msg = ears.listen()
            finally:
                safety.TONY_BUSY.release()
        if msg and config.VOICE_INPUT:
            print(f"You: {msg}")
        if not msg:
            continue
        triggers.touch()
        if msg.lower() in ("quit", "exit", "goodbye"):
            triggers.stop()
            speech.speak("Going offline. Goodbye, sir.")
            break
        if msg.lower().startswith("forget"):
            with memory.MEMORY_LOCK:
                memory.HISTORY.clear()
                if os.path.exists(config.MEMORY_FILE):
                    try: os.remove(config.MEMORY_FILE)
                    except Exception: pass
            ui.STATE.update({"chrome_running": ui._chrome_running(), "chrome_profile": "default", "last_site": "none"})
            print("[memory] wiped.")
            speech.speak("Memory wiped, sir.")
            continue

        with safety.TONY_BUSY:
            t_start = time.time()
            speech.ack_async()
            fp = brain.fast_path(msg)
            if fp is not None:
                print("Tony:", fp)
                overlay.ui_reply(fp)
                speech.wait_ack()
                speech.speak(fp)
                print(f"  [TOTAL: {time.time()-t_start:.1f}s]\n")
                continue
            answer = brain.ask_tony(msg)
            speech.wait_ack()
            print("Tony:", answer)
            spoken = memory.core_reply(answer, detail=memory.wants_detail(msg))
            overlay.ui_reply(spoken)
            speech.speak(spoken)
            speech.STOP["now"] = False
            print(f"  [TOTAL: {time.time()-t_start:.1f}s]\n")
