# =====================================================================
# tony_core/speech.py
# Speech output (MOUTH): edge-tts, pyttsx3 fallback, phrase cache, acknowledgements.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - SPEAKING: dict {"now": False}
# - SPEAK_LOCK: threading.RLock serializing audio playback
# - STOP: dict {"now": False} global stop signal
# - _CANCEL: dict {"gen": 0} speech generation cancellation counter
# - _SPEECH_THREAD: dict {"t": None} active speech worker thread
# - _offline_engine: pyttsx3 engine instance or None
# =====================================================================

import os
import time
import random
import hashlib
import asyncio
import threading
import pygame
import edge_tts
from tony_core import config
from tony_core import overlay

SPEAKING = {"now": False}
SPEAK_LOCK = threading.RLock()
STOP = {"now": False}

_CANCEL = {"gen": 0}
_SPEECH_THREAD = {"t": None}
_offline_engine = None

EDGE_VOICE = config.EDGE_VOICE
EDGE_RATE = config.EDGE_RATE
PHRASE_DIR = config.PHRASE_DIR
TTS_TIMEOUT_S = config.TTS_TIMEOUT_S
TTS_VOLUME = config.TTS_VOLUME
VOICE_ENGINE = config.VOICE_ENGINE

def _stop_requested():
    if STOP["now"]: return True
    if threading.current_thread() is threading.main_thread():
        try:
            import msvcrt
            while msvcrt.kbhit():
                if msvcrt.getwche() in ("\r", "\n"):
                    STOP["now"] = True
                    print("\n  [STOP requested from keyboard]")
                    return True
        except Exception:
            pass
    return False

def _speak_offline(text):
    global _offline_engine
    import pyttsx3
    if _offline_engine is None:
        _offline_engine = pyttsx3.init()
        _offline_engine.setProperty("rate", 175)
        _offline_engine.setProperty("volume", 1.0)
    _offline_engine.say(text)
    _offline_engine.runAndWait()

def _phrase_path(text):
    key = hashlib.md5(f"{EDGE_VOICE}|{EDGE_RATE}|{text}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(PHRASE_DIR, key + ".mp3")

def _tts_to(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    async def make():
        await asyncio.wait_for(edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE).save(tmp), timeout=TTS_TIMEOUT_S)
    asyncio.run(make())
    os.replace(tmp, path)

def _tts_file(text):
    if len(text) <= 240:
        p = _phrase_path(text)
        if not os.path.exists(p): _tts_to(p, text)
        return p
    p = os.path.join(config.HERE, "tony_tts.mp3")
    _tts_to(p, text)
    return p

def _mixer_init():
    if not pygame.mixer.get_init(): pygame.mixer.init()
    pygame.mixer.music.set_volume(TTS_VOLUME)

def _speak_edge(text, gen=None):
    path = _tts_file(text)
    if gen is not None and gen != _CANCEL["gen"]: return
    _mixer_init()
    try:
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        clock = pygame.time.Clock()
        while pygame.mixer.music.get_busy():
            if gen is not None and gen != _CANCEL["gen"]:
                pygame.mixer.music.stop()
                break
            clock.tick(25)
    finally:
        try: pygame.mixer.music.unload()
        except Exception: pass

def speak(text: str, gen=None):
    if not text: return
    with SPEAK_LOCK:
        SPEAKING["now"] = True
        overlay.ui_state("SPEAKING")
        t0 = time.time()
        try:
            try:
                if VOICE_ENGINE == "offline": _speak_offline(text)
                else: _speak_edge(text, gen)
                if time.time() - t0 > 6:
                    print(f"  [mouth slow: {time.time()-t0:.1f}s — first time; repeats instant]")
            except Exception as e:
                print(f"  [mouth '{VOICE_ENGINE}' FAILED: {str(e)[:70]}]")
                try:
                    if VOICE_ENGINE == "offline": _speak_edge(text, gen)
                    else: _speak_offline(text)
                except Exception as e2:
                    print(f"  [mouth fallback FAILED too: {str(e2)[:70]}]")
        finally:
            SPEAKING["now"] = False
            overlay.ui_state("IDLE")
            time.sleep(0.25)

def say_async(text: str):
    if not text: return
    gen = _CANCEL["gen"]
    t = threading.Thread(target=speak, args=(text, gen), daemon=True)
    _SPEECH_THREAD["t"] = t
    t.start()

def wait_speech():
    t = _SPEECH_THREAD["t"]
    if t and t.is_alive() and t is not threading.current_thread():
        t.join()

def stop_speech():
    _CANCEL["gen"] += 1
    try:
        if pygame.mixer.get_init(): pygame.mixer.music.stop()
    except Exception: pass

ACKS = ["On it.", "Working on it, sir.", "Right away.", "One moment.", "Consider it done."]

def prepare_voice_cache(extra=()):
    if VOICE_ENGINE != "edge": return
    def work():
        for ph in list(ACKS) + list(extra):
            try:
                path = _phrase_path(ph)
                if not os.path.exists(path): _tts_to(path, ph)
            except Exception: return
            time.sleep(0.15)
    threading.Thread(target=work, daemon=True).start()

def ack_async():
    files = [p for p in (_phrase_path(a) for a in ACKS) if os.path.exists(p)]
    if VOICE_ENGINE != "edge" or not files: return
    def _play():
        try:
            with SPEAK_LOCK:
                SPEAKING["now"] = True
                _mixer_init()
                pygame.mixer.music.load(random.choice(files))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy(): time.sleep(0.05)
                pygame.mixer.music.unload()
        except Exception: pass
        finally: SPEAKING["now"] = False
    threading.Thread(target=_play, daemon=True).start()

def wait_ack():
    try:
        while pygame.mixer.get_init() and pygame.mixer.music.get_busy(): time.sleep(0.05)
    except Exception: pass
