# =====================================================================
# tony_core/speech.py
# Speech output (MOUTH): Sentence-pipelined edge-tts, pyttsx3 fallback,
# bounded phrase cache, overlay STOP, and Mark-LV Self-Echo Guard.
# =====================================================================

import os
import re
import time
import queue
import random
import difflib
import hashlib
import asyncio
import threading
import pygame
import edge_tts
from collections import deque
from tony_core import config
from tony_core import overlay

SPEAKING = {"now": False}
SPEAK_LOCK = threading.RLock()
STOP = {"now": False}
OVERLAY_STOP = {"now": False}

# Keeps the last 5 sentences Tony spoke so ears.py never transcribes Tony's own speaker echo!
RECENT_SPOKEN = deque(maxlen=5)

_CANCEL = {"gen": 0}
_SPEECH_THREAD = {"t": None}
_offline_engine = None

EDGE_VOICE = config.EDGE_VOICE
EDGE_RATE = config.EDGE_RATE
PHRASE_DIR = config.PHRASE_DIR
TTS_TIMEOUT_S = config.TTS_TIMEOUT_S
TTS_VOLUME = config.TTS_VOLUME
VOICE_ENGINE = config.VOICE_ENGINE

ACKS = ["On it.", "Right away.", "One moment."]


def _record_spoken_echo(text: str):
    clean = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
    if clean:
        RECENT_SPOKEN.appendleft((time.time(), clean))


def is_self_echo(transcript: str) -> bool:
    """
    Mark-LV Self-Echo Guard:
    Returns True if Whisper transcribed Tony's own voice bleeding from the laptop speakers!
    """
    cand = re.sub(r"[^\w\s]", "", (transcript or "").lower()).strip()
    if not cand:
        return True
    now = time.time()
    cand_words = set(cand.split())
    for ts, spoken in list(RECENT_SPOKEN):
        if now - ts > 12.0:
            continue
        if cand in spoken or spoken in cand:
            return True
        spoken_words = set(spoken.split())
        if len(cand_words) >= 2 and spoken_words:
            overlap = len(cand_words & spoken_words) / len(cand_words)
            if overlap >= 0.75:
                return True
        if difflib.SequenceMatcher(None, cand, spoken).ratio() >= 0.72:
            return True
    return False


def current_generation():
    return _CANCEL["gen"]


def _stop_requested(expected_generation=None):
    return bool(
        STOP["now"]
        or OVERLAY_STOP["now"]
        or (expected_generation is not None and expected_generation != _CANCEL["gen"])
    )


def _speak_offline(text):
    global _offline_engine
    import pyttsx3
    if _offline_engine is None:
        _offline_engine = pyttsx3.init()
        _offline_engine.setProperty("rate", 175)
        _offline_engine.setProperty("volume", 1.0)
    _record_spoken_echo(text)
    _offline_engine.say(text)
    _offline_engine.runAndWait()





def _prune_phrase_cache(max_files=50):
    try:
        if not os.path.isdir(PHRASE_DIR):
            return
        files = [
            os.path.join(PHRASE_DIR, f)
            for f in os.listdir(PHRASE_DIR)
            if f.endswith(".mp3") and not f.startswith("_temp_tts_")
        ]
        if len(files) > max_files:
            files.sort(key=lambda p: os.path.getmtime(p))
            for old_f in files[: len(files) - max_files]:
                try:
                    os.remove(old_f)
                except Exception:
                    pass
    except Exception:
        pass


_TAMIL_CHAR_RE = re.compile(r"[\u0B80-\u0BFF]")


def _pick_voice_for_text(text: str) -> str:
    """Automatically selects the native Indian Tamil Neural Voice whenever Tamil script is present!"""
    if _TAMIL_CHAR_RE.search(text or ""):
        return getattr(config, "EDGE_VOICE_TAMIL", "ta-IN-ValluvarNeural")
    return EDGE_VOICE


def _phrase_path(text):
    voice = _pick_voice_for_text(text)
    key = hashlib.md5(f"{voice}|{EDGE_RATE}|{text}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(PHRASE_DIR, key + ".mp3")


def _tts_to(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    voice = _pick_voice_for_text(text)

    async def make():
        await asyncio.wait_for(
            edge_tts.Communicate(text, voice, rate=EDGE_RATE).save(tmp),
            timeout=TTS_TIMEOUT_S,
        )

    asyncio.run(make())
    os.replace(tmp, path)


def _tts_file(text, chunk_idx=0):
    if text in ACKS or len(text) <= 40:
        p = _phrase_path(text)
        if not os.path.exists(p):
            _tts_to(p, text)
            _prune_phrase_cache()
        return p
    os.makedirs(PHRASE_DIR, exist_ok=True)
    p = os.path.join(PHRASE_DIR, f"_temp_tts_{threading.get_ident()}_{chunk_idx}.mp3")
    _tts_to(p, text)
    return p


def _mixer_init():
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.set_volume(max(0.65, float(TTS_VOLUME)))


def _split_into_clauses(text: str) -> list:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]
    return parts if parts else [text.strip()]


def _cancelled(gen):
    return bool(STOP["now"] or OVERLAY_STOP["now"] or (gen is not None and gen != _CANCEL["gen"]))


def _play_mp3_file(path: str, gen=None):
    if not path or not os.path.exists(path) or _cancelled(gen):
        return
    _mixer_init()
    try:
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        clock = pygame.time.Clock()
        while pygame.mixer.music.get_busy():
            if _cancelled(gen):
                pygame.mixer.music.stop()
                break
            clock.tick(35)
    finally:
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass


def _watch_barge_in(allow_barge_in=True):
    if allow_barge_in and getattr(config, "BARGE_IN_ENABLED", True):
        from tony_core import ears
        # Only spawn separate barge-in watcher if continuous ear producer isn't already watching
        if not ears.CONTINUOUS_EAR["active"]:
            threading.Thread(target=ears.watch_for_barge_in, name="tony-barge-in", daemon=True).start()


def _speak_chunks(text_chunks, allow_barge_in=True, expected_gen=None):
    with SPEAK_LOCK:
        if OVERLAY_STOP["now"] or (expected_gen is not None and expected_gen != _CANCEL["gen"]):
            return
        gen = _CANCEL["gen"]
        STOP["now"] = False
        SPEAKING["now"] = True
        overlay.ui_state("SPEAKING")
        speech_started_at = time.perf_counter()
        first_clause_logged = False
        _watch_barge_in(allow_barge_in)

        if VOICE_ENGINE == "offline":
            try:
                text = " ".join(str(x).strip() for x in text_chunks if str(x).strip())
                if text and not _cancelled(gen):
                    _speak_offline(text)
            finally:
                SPEAKING["now"] = False
                overlay.ui_state("IDLE")
            return

        audio_q = queue.Queue(maxsize=3)

        def put_audio(item):
            while not _cancelled(gen):
                try:
                    audio_q.put(item, timeout=0.15)
                    return True
                except queue.Full:
                    continue
            return False

        def producer():
            try:
                for idx, clause in enumerate(text_chunks):
                    clause = str(clause or "").strip()
                    if not clause or _cancelled(gen):
                        break
                    _record_spoken_echo(clause)
                    try:
                        path = _tts_file(clause, chunk_idx=idx)
                        item = ("file", path)
                    except Exception as exc:
                        print(f"  [tts chunk {idx + 1} failed; trying offline voice: {str(exc)[:60]}]")
                        item = ("offline", clause)
                    if not put_audio(item):
                        if item[0] == "file" and os.path.basename(item[1]).startswith("_temp_tts_"):
                            try:
                                os.remove(item[1])
                            except OSError:
                                pass
                        break
            except Exception as exc:
                print(f"  [tts producer failed: {str(exc)[:80]}]")
            finally:
                if not _cancelled(gen):
                    put_audio(None)

        worker = threading.Thread(target=producer, name="tony-tts-producer", daemon=True)
        worker.start()
        try:
            while True:
                if _cancelled(gen):
                    try:
                        if pygame.mixer.get_init():
                            pygame.mixer.music.stop()
                    except Exception:
                        pass
                    break
                try:
                    item = audio_q.get(timeout=0.1)
                except queue.Empty:
                    if not worker.is_alive():
                        break
                    continue
                if item is None:
                    break
                kind, payload = item
                if not first_clause_logged:
                    print(f"  [tts: first clause ready in {time.perf_counter() - speech_started_at:.2f}s]")
                    first_clause_logged = True
                if kind == "offline":
                    if not _cancelled(gen):
                        try:
                            _speak_offline(payload)
                        except Exception as exc:
                            print(f"  [offline TTS failed: {str(exc)[:70]}]")
                    continue
                path = payload
                try:
                    _play_mp3_file(path, gen=gen)
                finally:
                    if os.path.basename(path).startswith("_temp_tts_"):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
        finally:
            if _cancelled(gen):
                try:
                    if pygame.mixer.get_init():
                        pygame.mixer.music.stop()
                except Exception:
                    pass
            SPEAKING["now"] = False
            overlay.ui_state("IDLE")


def speak_stream(text_chunks, allow_barge_in: bool = True, expected_generation=None):
    if text_chunks is None:
        return
    _speak_chunks(text_chunks, allow_barge_in=allow_barge_in, expected_gen=expected_generation)


def _speak_edge_pipelined(text: str, gen=None):
    _speak_chunks(_split_into_clauses(text), allow_barge_in=True, expected_gen=gen)


def speak(text: str, gen=None, allow_barge_in: bool = True):
    if not text:
        return
    _speak_chunks(_split_into_clauses(text), allow_barge_in=allow_barge_in, expected_gen=gen)


def say_async(text: str):
    if not text:
        return
    gen = _CANCEL["gen"]
    t = threading.Thread(target=speak, args=(text, gen, False), daemon=True)
    _SPEECH_THREAD["t"] = t
    t.start()


def wait_speech():
    t = _SPEECH_THREAD["t"]
    if t and t.is_alive() and t is not threading.current_thread():
        t.join()


def stop_speech(from_overlay: bool = False):
    _CANCEL["gen"] += 1
    STOP["now"] = True
    if from_overlay:
        OVERLAY_STOP["now"] = True
    try:
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
    except Exception:
        pass
    try:
        if _offline_engine is not None:
            _offline_engine.stop()
    except Exception:
        pass


def prepare_voice_cache(extra=()):
    if VOICE_ENGINE != "edge":
        return

    def work():
        for ph in list(ACKS) + list(extra):
            try:
                path = _phrase_path(ph)
                if not os.path.exists(path):
                    _tts_to(path, ph)
            except Exception:
                return
            time.sleep(0.15)

    threading.Thread(target=work, daemon=True).start()


def ack_async():
    files = [p for p in (_phrase_path(a) for a in ACKS) if os.path.exists(p)]
    if VOICE_ENGINE != "edge" or not files:
        return
    gen = _CANCEL["gen"]

    def _play():
        try:
            with SPEAK_LOCK:
                if _cancelled(gen):
                    return
                SPEAKING["now"] = True
                overlay.ui_state("SPEAKING")
                _mixer_init()
                choice = random.choice(files)
                pygame.mixer.music.load(choice)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if _cancelled(gen):
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.02)
                pygame.mixer.music.unload()
        except Exception:
            pass
        finally:
            SPEAKING["now"] = False
            overlay.ui_state("IDLE")

    threading.Thread(target=_play, name="tony-ack", daemon=True).start()


def wait_ack():
    try:
        while pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            time.sleep(0.04)
    except Exception:
        pass