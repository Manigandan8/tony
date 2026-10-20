# =====================================================================
# tony_core/wake.py
# Wake word detection thread (OpenWakeWord) and wake command handling.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - WAKE: dict {"heard": False, "last": 0.0, "pause": False, "reopen": False, "debug_until": 0.0}
# - _WAKE_MODEL: dict {"m": None, "err": ""}
# =====================================================================

import re
import time
from tony_core import config
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core import ears

WAKE = {"heard": False, "last": 0.0, "pause": False, "reopen": False, "debug_until": 0.0}
_WAKE_MODEL = {"m": None, "err": ""}


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
    warm = int(config.WAKE_WARMUP_S / 0.08) if first else 3
    try:
        model.reset()
    except Exception:
        pass
    last_dbg = 0.0
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CH, device=ears.MIC_DEVICE_INDEX) as stream:
        if first:
            print(f"[wake] ready — say '{config.WAKE_MODEL.replace('_', ' ')}'  (mic {ears._mic_name()}, trigger {config.WAKE_THRESHOLD})")
        while True:
            if speech.SPEAKING["now"] or ears.LISTENING["now"] or WAKE["pause"] or WAKE["reopen"]:
                WAKE["reopen"] = False
                return 0.0
            data, _ = stream.read(CH)
            x = data[:, 0]
            pk = int(np.abs(x.astype(np.int32)).max())
            # Only apply gain if there is actual vocal energy (pk >= 450), never boost dead silence!
            if 450 <= pk < 12000:
                x = np.clip(x.astype(np.float32) * min(20000 / pk, 3.0) * config.WAKE_GAIN, -32768, 32767).astype(np.int16)
            scores = model.predict(x)
            if warm > 0:
                warm -= 1
                continue
            top = float(max(scores.values())) if scores else 0.0
            if time.time() < WAKE["debug_until"] and top > 0.05 and time.time() - last_dbg > 0.3:
                last_dbg = time.time()
                print(f"   [wake score {top:.2f}]")
            if pk >= 350 and top > config.WAKE_THRESHOLD and time.time() - WAKE["last"] > config.WAKE_REFRACTORY_S:
                WAKE["last"] = time.time()
                return top


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
            time.sleep(0.1)
        ears._resolve_mic()
        try:
            score = _listen_for_wake(sd, np, model, first)
            first, fails = False, 0
        except Exception as e:
            fails += 1
            print(f"[wake] mic problem ({ears._mic_name()}): {str(e)[:80]}")
            if ears.MIC_DEVICE_INDEX is not None:
                ears.MIC_DEVICE_INDEX = None
                ears._MIC_FAIL["until"] = time.time() + 30
                continue
            time.sleep(10 if fails >= 3 else 3)
            continue
        if not score or not safety.TONY_BUSY.acquire(blocking=False):
            continue
        try:
            print(f"[wake] heard '{config.WAKE_MODEL.replace('_', ' ')}' (score {score:.2f})")
            speech.STOP["now"] = False
            ears._beep()
            time.sleep(0.2)
            overlay.ui_state("WAKE")
            msg = ears.listen()
            if msg:
                print(f"You (wake): {msg}")
                try:
                    from tony_core import triggers
                    triggers.touch()
                except Exception:
                    pass
                if re.search(r"\b(tony\s+)?stop\b|\b(cancel|abort)\b", msg, re.I):
                    speech.STOP["now"] = True
                    speech.speak("Stopped, sir.")
                    continue
                speech.ack_async()
                fp = brain.fast_path(msg)
                if fp is not None:
                    speech.wait_ack()
                    print("Tony:", fp)
                    overlay.ui_reply(fp)
                    speech.speak(fp)
                else:
                    answer = brain.ask_tony(msg)
                    speech.wait_ack()
                    print("Tony:", answer)
                    spoken = memory.core_reply(answer, detail=memory.wants_detail(msg))
                    overlay.ui_reply(spoken)
                    speech.speak(spoken)
        except Exception as e:
            print(f"  [wake command failed: {str(e)[:60]}]")
        finally:
            overlay.ui_level(0)
            safety.TONY_BUSY.release()
            overlay.ui_state("IDLE")