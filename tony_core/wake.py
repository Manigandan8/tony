# =====================================================================
# tony_core/wake.py
# Wake word detection thread (OpenWakeWord) and turn-by-turn continuous
# conversation loop (listens only AFTER finishing the current work).
# =====================================================================

import re
import time
from tony_core import config
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core import ears
from tony_core import audio_hub
from tony_core import conversation

WAKE = {"heard": False, "last": 0.0, "pause": False, "reopen": False, "debug_until": 0.0}
_WAKE_MODEL = {"m": None, "err": ""}

_SLEEP_PAT = re.compile(
    r"^\s*(?:(?:hey|ok|okay|ஏய்|ஹே)\s+)?(?:tony|jarvis|ஜார்விஸ்|டோனி)?\s*,?\s*"
    r"(?:go\s+to\s+sleep|sleep\s+mode|sleep\s+now|stand\s*by|go\s+to\s+idle|idle\s+mode|"
    r"that'?s\s+all(?:\s+for\s+now)?|nothing\s+else|stop\s+listening|"
    r"bye|goodbye|see\s+you(?:\s+later)?|talk\s+to\s+you\s+later|thank\s+you\s+bye|"
    r"தூங்கு|போய்\s+தூங்கு|போதும்|ஸ்டாண்ட்\s*பை|அப்புறம்\s+பேசலாம்|பாய்|போயிட்டு\s+வரேன்|"
    r"thoongu|pothum|appuram\s+pesalam)\s*[.!]*\s*$",
    re.I
)


def _get_wake_model():
    if _WAKE_MODEL["m"] is None and not _WAKE_MODEL["err"]:
        try:
            import openwakeword
            from openwakeword.model import Model
            try:
                _WAKE_MODEL["m"] = Model(wakeword_models=[config.WAKE_MODEL], inference_framework="onnx")
            except Exception:
                openwakeword.utils.download_models()
                _WAKE_MODEL["m"] = Model(wakeword_models=[config.WAKE_MODEL], inference_framework="onnx")
        except Exception as e:
            _WAKE_MODEL["err"] = f"{type(e).__name__}: {str(e)[:120]}"
    return _WAKE_MODEL["m"]


def _listen_for_wake(sd, np, model, first):
    CH = 1280
    warm = int(config.WAKE_WARMUP_S / 0.08) if first else 2
    try:
        model.reset()
    except Exception:
        pass
    last_dbg = 0.0
    hub = audio_hub.get_audio_hub()
    sub = None
    stream = None
    try:
        if hub.running:
            sub = hub.subscribe("wake-word", max_frames=120)
            read_samples = lambda: np.frombuffer(sub.read_samples(CH, timeout=2.0), dtype=np.int16)
        else:
            dev = ears.MIC_DEVICE_INDEX if (isinstance(ears.MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX >= 0) else None
            stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CH, device=dev)
            stream.start()
            read_samples = lambda: stream.read(CH)[0][:, 0].copy()

        if first:
            print(f"[wake] ready — say '{config.WAKE_MODEL.replace('_', ' ')}'  (mic {ears._mic_name()}, trigger {config.WAKE_THRESHOLD})")
        while True:
            if speech.SPEAKING["now"] or ears.LISTENING["now"] or WAKE["pause"] or WAKE["reopen"]:
                WAKE["reopen"] = False
                return 0.0
            try:
                x = read_samples()
            except TimeoutError:
                continue
            pk = int(np.abs(x.astype(int)).max())
            if 0 < pk < 12000:
                x = np.clip(x.astype(int) * min(20000 / pk, 4.0) * config.WAKE_GAIN, -32768, 32767).astype(np.int16)
            scores = model.predict(x)
            if warm > 0:
                warm -= 1
                continue
            top = float(max(scores.values())) if scores else 0.0
            if time.time() < WAKE["debug_until"] and top > 0.05 and time.time() - last_dbg > 0.3:
                last_dbg = time.time()
                print(f"   [wake score {top:.2f}]")
            if top > config.WAKE_THRESHOLD and time.time() - WAKE["last"] > config.WAKE_REFRACTORY_S:
                WAKE["last"] = time.time()
                return top
    finally:
        if sub is not None:
            sub.close()
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


def _conversation_session(brain):
    """
    Turn-by-Turn Conversation Loop:
    1. Listens to your command
    2. Completes the action and finishes speaking
    3. Only then starts listening for your next command!
    """
    is_followup = False
    announced_waiting = False
    speech.OVERLAY_STOP["now"] = False
    ears.CONTINUOUS_EAR["active"] = False

    while True:
        if speech.OVERLAY_STOP["now"]:
            speech.OVERLAY_STOP["now"] = False
            print("  [convo stopped via overlay button — returning to idle]")
            break

        if is_followup and not announced_waiting:
            print("  [convo mode active — speak anytime, or say 'go to sleep' / 'stand by' for idle]")
            overlay.ui_reply("Convo mode — speak anytime ('stand by' to sleep)")
            announced_waiting = True

        t_stt0 = time.perf_counter()
        msg = ears.listen(
            max_seconds=9.0,
            silence_seconds=config.SPEAK_SILENCE_S,
            wait_seconds=30.0,
            is_followup=True
        )
        t_stt = time.perf_counter() - t_stt0

        if speech.OVERLAY_STOP["now"]:
            speech.OVERLAY_STOP["now"] = False
            print("  [convo stopped via overlay button — returning to idle]")
            break

        if not msg:
            is_followup = True
            continue

        announced_waiting = False
        print(f"You ({'convo' if is_followup else 'wake'}): {msg}")
        try:
            from tony_core import triggers
            triggers.touch()
        except Exception:
            pass

        if _SLEEP_PAT.match(msg):
            farewell = "Alright, standing by. Say Hey Jarvis whenever you need me!"
            print("Tony:", farewell)
            overlay.ui_reply(farewell)
            speech.speak(farewell, allow_barge_in=False)
            break

        if re.fullmatch(r"\s*(?:tony\s+|jarvis\s+)?(?:stop|cancel|abort|quiet)\s*[.!]*\s*", msg, re.I):
            speech.stop_speech(from_overlay=False)
            speech.speak("Stopped. What's next?", allow_barge_in=False)
            is_followup = True
            continue

        t_brain0 = time.perf_counter()
        fp = brain.fast_path(msg)
        if fp is not None:
            t_brain = time.perf_counter() - t_brain0
            print("Tony:", fp)
            overlay.ui_reply(fp)
            t_tts0 = time.perf_counter()
            speech.speak(fp, allow_barge_in=True)
            t_tts = time.perf_counter() - t_tts0
            print(f"  [perf: listen+STT {t_stt:.1f}s | fast-path {t_brain*1000:.0f}ms | TTS {t_tts:.1f}s]")
        else:
            t_tts0 = time.perf_counter()
            answer = conversation.respond_and_speak(msg, allow_barge_in=True)
            t_brain = time.perf_counter() - t_brain0
            t_tts = time.perf_counter() - t_tts0
            print("Tony:", answer or "(no response)")
            overlay.ui_reply(memory.core_reply(answer, detail=memory.wants_detail(msg)) if answer else "")
            print(f"  [perf: listen+STT {t_stt:.1f}s | brain+stream {t_brain:.1f}s | speech {t_tts:.1f}s]")

        if speech.OVERLAY_STOP["now"]:
            speech.OVERLAY_STOP["now"] = False
            print("  [convo stopped via overlay button — returning to idle]")
            break

        if ears.BARGE_IN["triggered"]:
            is_followup = True
            continue

        # Wait until Tony has completely finished speaking before listening again!
        ears._wait_for_speaker(10)
        time.sleep(0.2)
        is_followup = True


def _wake_loop():
    model = _get_wake_model()
    if model is None:
        print(f"[wake] unavailable: {_WAKE_MODEL['err']}")
        return
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        return
    first, fails = True, 0
    from tony_core import brain
    while True:
        while speech.SPEAKING["now"] or ears.LISTENING["now"] or WAKE["pause"]:
            time.sleep(0.08)
        ears._resolve_mic()
        try:
            score = _listen_for_wake(sd, np, model, first)
            first, fails = False, 0
        except Exception as e:
            fails += 1
            print(f"[wake] mic busy, retrying ({ears._mic_name()}): {str(e)[:60]}")
            time.sleep(0.6 if fails < 5 else 2.0)
            continue
        if not score or not safety.TONY_BUSY.acquire(blocking=False):
            continue
        try:
            print(f"[wake] heard '{config.WAKE_MODEL.replace('_', ' ')}' (score {score:.2f})")
            speech.STOP["now"] = False
            speech.OVERLAY_STOP["now"] = False
            ears._beep()
            time.sleep(0.1)
            overlay.ui_state("WAKE")
            _conversation_session(brain)
        except Exception as e:
            print(f"  [wake command failed: {str(e)[:60]}]")
        finally:
            ears.CONTINUOUS_EAR["active"] = False
            ears._wait_for_speaker(8)
            speech.SPEAKING["now"] = False
            ears.LISTENING["now"] = False
            ears.BARGE_IN["triggered"] = False
            ears.BARGE_IN["pre_roll"] = []
            overlay.ui_level(0)
            overlay.ui_state("IDLE")
            safety.TONY_BUSY.release()
            time.sleep(0.2)
            WAKE["last"] = time.time() - 1.0
            print("  [idle — ready for 'hey jarvis']")