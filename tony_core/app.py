# =====================================================================
# tony_core/app.py
# Application entrypoint: boot sequence, trigger threads, and main loop.
# =====================================================================
import ctypes
import platform


def enable_windows_dpi_awareness():
    if platform.system() == "Windows":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


enable_windows_dpi_awareness()

import os
import re
import time
import datetime
import threading

from tony_core import config
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core import ears
from tony_core import audio_hub
from tony_core import conversation
from tony_core import wake
from tony_core import brain
from tony_core import telegram_link
from tony_core import triggers
from tony_core import pc_index
from tony_core import screen_pulse
from tony_core.tools import TOOLS, ui

from tony_core.providers import (
    groqai, openrouter, zai, github_models,
    mistralai, cerebras, cohereai
)


def _register_pc_index_tools():
    TOOLS.update({
        "find_anywhere": pc_index.find_anywhere,
        "open_anything": pc_index.open_anything,
        "recent_pc_activity": pc_index.recent_pc_activity,
        "watch_screen_for": screen_pulse.watch_screen_for,
    })
    existing_names = {t["function"]["name"] for t in brain.GROQ_TOOLS}
    if "find_anywhere" not in existing_names:
        brain.GROQ_TOOLS.extend([
            brain.T("find_anywhere", "Search the ENTIRE PC for any file or folder by name.", {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["any", "file", "folder"]}}, ["query"]),
            brain.T("open_anything", "Open ANY file, folder, or installed app anywhere on the PC (including 'the file I just created/downloaded/edited').", {"target": {"type": "string"}}, ["target"]),
            brain.T("recent_pc_activity", "Check what files were recently created, downloaded, or edited on the computer.", {"count": {"type": "integer"}}, []),
        ])
    if "watch_screen_for" not in existing_names:
        brain.GROQ_TOOLS.append(
            brain.T("watch_screen_for", "Watch the screen every 1 second and alert when `target_text` appears, or toggle `auto_skip_ads`.", {"target_text": {"type": "string"}, "auto_skip_ads": {"type": "boolean"}}, [])
        )


def _briefing_loop():
    if not config.BRIEFING_TIMES:
        return
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
                    overlay.ui_reply(spoken)
                    speech.speak(spoken)
                finally:
                    safety.TONY_BUSY.release()
        except Exception:
            pass
        time.sleep(20)


def boot():
    groqai.init_models()
    openrouter.init_models()

    print(f"[brain-zai] models: {zai.ZAI_BRAIN_MODELS if zai.ZAI_API_KEY else 'none (check ZAI_API_KEY)'}")
    print(f"[vision-zai] models: {zai.ZAI_VISION_MODELS if zai.ZAI_API_KEY else 'none (check ZAI_API_KEY)'}")
    print(f"[brain-github] models: {github_models.GITHUB_BRAIN_MODELS if github_models.GITHUB_TOKEN else 'skipped (no GITHUB_TOKEN in .env)'}")
    print(f"[vision-github] models: {github_models.GITHUB_VISION_MODELS if github_models.GITHUB_TOKEN else 'skipped'}")
    print(f"[brain-mistral] models: {mistralai.MISTRAL_BRAIN_MODELS if mistralai.MISTRAL_API_KEY else 'skipped (no MISTRAL_API_KEY in .env)'}")
    print(f"[vision-mistral] models: {mistralai.MISTRAL_VISION_MODELS if mistralai.MISTRAL_API_KEY else 'skipped'}")
    print(f"[brain-cerebras] models: {cerebras.CEREBRAS_BRAIN_MODELS if cerebras.CEREBRAS_API_KEY else 'skipped (no CEREBRAS_API_KEY in .env)'}")
    print(f"[brain-cohere] models: {cohereai.COHERE_BRAIN_MODELS if cohereai.COHERE_API_KEY else 'skipped (no COHERE_API_KEY in .env)'}")
    print(f"[vision-cohere] models: {cohereai.COHERE_VISION_MODELS if cohereai.COHERE_API_KEY else 'skipped'}")

    if zai.ZAI_API_KEY:
        zai._zai_probe()

    memory.init_memory()
    ears.init_ears()
    ears._resolve_mic()
    try:
        audio_hub.get_audio_hub().start(device=ears.MIC_DEVICE_INDEX)
        print("[audio] shared microphone online (16 kHz / 30 ms frames)")
    except Exception as e:
        print(f"[audio] shared capture unavailable; using legacy streams: {str(e)[:100]}")

    _register_pc_index_tools()
    pc_index.start_pc_index()
    screen_pulse.start_screen_pulse()


def main():
    boot()
    print("Tony ready (overlay + wake + 1s screen-pulse + PC-index + 9 brains + voice).\n")
    ears._get_vad()
    speech.prepare_voice_cache()
    threading.Thread(target=overlay._overlay_app, daemon=True).start()
    time.sleep(1.5)
    speech.say_async("Tony online. All systems nominal.")

    triggers.init(speak=speech.speak, is_speaking=lambda: speech.SPEAKING["now"], mic_index=ears.MIC_DEVICE_INDEX)
    triggers.start()
    if config.WAKE_WORD_ENABLED:
        time.sleep(0.5)
        threading.Thread(target=wake._wake_loop, daemon=True).start()

    threading.Thread(target=_briefing_loop, daemon=True).start()
    threading.Thread(target=telegram_link._telegram_poll, daemon=True).start()
    overlay.ui_state("IDLE")
    overlay.ui_reply("Say 'Hey Jarvis' or type below.")

    while True:
        try:
            msg = input("You (Enter = speak, or type): ").strip()
        except (EOFError, KeyboardInterrupt):
            triggers.stop()
            screen_pulse.stop_screen_pulse()
            speech.speak("Going offline. Goodbye, sir.", allow_barge_in=False)
            break

        if safety.PENDING_CONFIRM["active"]:
            safety.supply_confirm_input(msg or "yes")
            continue

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
            screen_pulse.stop_screen_pulse()
            speech.speak("Going offline. Goodbye, sir.", allow_barge_in=False)
            break
        if re.fullmatch(r"\s*(?:tony\s+|jarvis\s+)?(?:please\s+)?(?:forget\s+everything|clear\s+memory|wipe\s+memory|reset\s+memory)\s*[.!]?\s*", msg, re.I):
            with memory.MEMORY_LOCK:
                memory.HISTORY.clear()
                if os.path.exists(config.MEMORY_FILE):
                    try:
                        os.remove(config.MEMORY_FILE)
                    except Exception:
                        pass
            ui.STATE.update({"chrome_running": ui._chrome_running(), "chrome_profile": "default", "last_site": "none"})
            print("[memory] wiped.")
            speech.speak("Conversation memory wiped, sir.")
            continue

        with safety.TONY_BUSY:
            t_start = time.time()
            fp = brain.fast_path(msg)
            if fp is not None:
                print("Tony:", fp)
                overlay.ui_reply(fp)
                speech.speak(fp)
                print(f"  [TOTAL: {time.time()-t_start:.1f}s]\n")
                continue
            answer = conversation.respond_and_speak(msg, allow_barge_in=True)
            print("Tony:", answer or "(no response)")
            overlay.ui_reply(memory.core_reply(answer, detail=memory.wants_detail(msg)) if answer else "")
            speech.STOP["now"] = False
            print(f"  [TOTAL: {time.time()-t_start:.1f}s]\n")

    triggers.stop()
    screen_pulse.stop_screen_pulse()
    audio_hub.get_audio_hub().stop()