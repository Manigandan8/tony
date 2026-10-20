# =====================================================================
# tony_core/ears.py
# Ear & Microphone: Whisper STT, VAD, speech listening, mic management.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - LISTENING: dict {"now": False}
# - MIC_DEVICE_INDEX: integer or None (reassigned in _resolve_mic, _set_mic)
# - _MIC_FAIL: dict {"until": 0.0}
# - _ROOM: dict {"thresh": None}
# - _VAD: dict {"obj": None, "tried": False}
# - STT_MODEL: active Whisper model ID
# =====================================================================

import os
import io
import re
import time
import json
import wave
import threading
from collections import deque

from tony_core import config
from tony_core import overlay
from tony_core import speech
from tony_core.providers import groqai

LISTENING = {"now": False}

VOICE_INPUT = config.VOICE_INPUT
STT_LANGUAGE = config.STT_LANGUAGE
STT_ACCURACY = config.STT_ACCURACY
MIC_THRESHOLD = config.MIC_THRESHOLD
SPEAK_SILENCE_S = config.SPEAK_SILENCE_S
STT_PROMPT = config.STT_PROMPT


def _pick_stt_model():
    order = ("whisper-large-v3", "whisper-large-v3-turbo") if STT_ACCURACY else ("whisper-large-v3-turbo", "whisper-large-v3")
    for p in order:
        if p in groqai.AVAILABLE_MODELS:
            return p
    return order[0]


STT_MODEL = "whisper-large-v3"


def init_ears():
    global STT_MODEL
    STT_MODEL = _pick_stt_model()
    print(f"[ears] using: {STT_MODEL}")


SETTINGS_FILE = config.SETTINGS_FILE


def _load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(**kw):
    s = _load_settings()
    s.update(kw)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass


MIC_DEVICE_INDEX = _load_settings().get("mic_device")
_MIC_FAIL = {"until": 0.0}


def _resolve_mic():
    global MIC_DEVICE_INDEX
    if time.time() < _MIC_FAIL["until"]:
        return
    name = _load_settings().get("mic_device_name", "")
    if not name:
        return
    try:
        import sounddevice as sd
        matches = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and name.lower() in d["name"].lower():
                api = sd.query_hostapis(d["hostapi"])["name"]
                matches.append((0 if "WASAPI" in api else 1, i))
        MIC_DEVICE_INDEX = sorted(matches)[0][1] if matches else None
    except Exception:
        pass


_ROOM = {"thresh": None}
_VAD = {"obj": None, "tried": False}


def _get_vad():
    if not _VAD["tried"]:
        _VAD["tried"] = True
        try:
            import webrtcvad
            _VAD["obj"] = webrtcvad.Vad(3)
            print("[ears] voice-activity detector: ON")
        except Exception:
            print("[ears] tip: run pip install webrtcvad-wheels")
    return _VAD["obj"]


def _speech_started(flags, need=4, window=6):
    return sum(flags[-window:]) >= need


def _speech_ended(flags, frames_needed):
    recent = flags[-frames_needed:]
    return len(recent) >= frames_needed and sum(recent) <= max(2, int(0.25 * frames_needed))


def _energy_trigger(floor):
    return max(floor * 1.8, floor + 300)


def _speaker_index():
    try:
        import sounddevice as sd
        best = None
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0 and "speakers (realtek" in d["name"].lower():
                if "WASAPI" in sd.query_hostapis(d["hostapi"])["name"]:
                    return i
                if best is None:
                    best = i
        return best
    except Exception:
        return None


def _beep():
    try:
        import numpy as np, sounddevice as sd
        dev = _speaker_index()
        rate = int(sd.query_devices(dev)["default_samplerate"]) if dev is not None else 44100
        t = np.arange(int(rate * 0.12)) / rate
        env = np.minimum(1.0, np.minimum(t / 0.02, (0.12 - t) / 0.04))
        tone = (0.15 * env * np.sin(2 * np.pi * 740 * t) * 32767).astype(np.int16)
        sd.play(tone, rate, device=dev, blocking=True)
    except Exception:
        pass


def _wait_for_speaker(max_s=15):
    t0 = time.time()
    t = speech._SPEECH_THREAD.get("t")
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(max_s)
    while speech.SPEAKING["now"] and time.time() - t0 < max_s:
        time.sleep(0.05)


def _record_levels(seconds, skip=0.3):
    import sounddevice as sd, numpy as np
    fs, levels = 1600, []
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=fs, device=MIC_DEVICE_INDEX) as s:
        for i in range(int((seconds + skip) / 0.1)):
            d, _ = s.read(fs)
            if i * 0.1 >= skip:
                levels.append(int(np.abs(d.astype(np.int32)).mean()))
    return levels


def calibrate_mic():
    try:
        import numpy as np
        _wait_for_speaker(10)
        _beep()
        print("  [calibration 1/2: stay QUIET for 2 seconds]")
        quiet = _record_levels(2.0)
        _beep()
        print("  [calibration 2/2: now SAY A SENTENCE out loud for 3 seconds]")
        talk = _record_levels(3.0)
        noise, speech_lvl = float(np.percentile(quiet, 90)), float(np.percentile(talk, 75))
        thr = max(int(noise + 0.35 * (speech_lvl - noise)), int(noise * 1.15) + 50)
        _ROOM["thresh"] = thr
        print(f"  [mic calibrated: room noise {int(noise)}, voice {int(speech_lvl)}, trigger {thr}]")
        return thr
    except Exception as e:
        print(f"  [mic calibration failed: {str(e)[:60]}]")
        return None


def _mic_name():
    try:
        import sounddevice as sd
        idx = sd.default.device[0] if MIC_DEVICE_INDEX is None else MIC_DEVICE_INDEX
        if idx is None or idx < 0:
            return f"Windows default ({sd.query_devices(kind='input')['name']})"
        return f"{idx}: {sd.query_devices(idx)['name']}"
    except Exception:
        return "Windows default"


def mic_list():
    import sounddevice as sd
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    rows = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            try:
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception:
                api = "?"
            tags = (["(Windows default)"] if i == default_in else []) + (["<-- Tony uses this"] if MIC_DEVICE_INDEX == i else [])
            rows.append(f"   {i:>3}: {d['name'][:46]:<46} [{api}] {' '.join(tags)}")
    return "\n".join(rows) if rows else "   (no input devices found)"


def _set_mic(idx):
    global MIC_DEVICE_INDEX
    from tony_core import wake
    MIC_DEVICE_INDEX = int(idx)
    try:
        import sounddevice as sd
        _name = sd.query_devices(MIC_DEVICE_INDEX)["name"]
    except Exception:
        _name = ""
    _save_settings(mic_device=MIC_DEVICE_INDEX, mic_device_name=_name)
    wake.WAKE["reopen"] = True
    return f"Using microphone {MIC_DEVICE_INDEX}."


def mic_doctor(seconds=5):
    from tony_core import wake
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        return "The sounddevice package is missing."
    wake.WAKE["pause"] = True
    time.sleep(0.4)
    _wait_for_speaker(10)
    try:
        print(f"\n[mic doctor] microphone: {_mic_name()}\n" + mic_list())
        model, vad = wake._get_wake_model(), _get_vad()
        overlay.ui_state("LISTENING")
        _beep()
        print(f"\n >> Say 'Hey Jarvis' clearly for the next {seconds} seconds <<")
        levels, scores = [], []
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280, device=MIC_DEVICE_INDEX) as s:
            for i in range(int(seconds / 0.08)):
                d, _ = s.read(1280)
                x = d[:, 0]
                lvl = int(np.abs(x.astype(np.int32)).mean())
                levels.append(lvl)
                sc = float(max(model.predict(x).values())) if model else 0.0
                scores.append(sc)
                if i % 3 == 0:
                    print(f"   level {lvl:>5} {'#' * min(40, lvl // 250):<40} wake {sc:.2f}")
        return "Wake word works." if max(scores or [0.0]) > config.WAKE_THRESHOLD else "I heard you, but not the wake word."
    except Exception as e:
        return f"Mic error: {str(e)[:60]}"
    finally:
        try:
            if wake._WAKE_MODEL["m"]:
                wake._WAKE_MODEL["m"].reset()
        except Exception:
            pass
        overlay.ui_level(0)
        wake.WAKE["pause"] = False
        overlay.ui_state("IDLE")


def _mic_command(msg):
    from tony_core import wake
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (msg or "").lower())).strip()
    t = re.sub(r"^(?:(?:hey|ok|okay) )?(?:tony|jarvis) ", "", t)
    t = re.sub(r"^(?:please|can you|could you) ", "", t)
    if re.fullmatch(r"calibrate (?:the )?(?:mic|microphone)", t):
        calibrate_mic()
        return "Microphone calibrated."
    if re.fullmatch(r"(?:mic|microphone|wake(?: word)?) (?:doctor|test|check)|(?:test|check) (?:the )?(?:mic|microphone|wake word)", t):
        return mic_doctor()
    if re.fullmatch(r"(?:list )?(?:mic|mics|microphones?)(?: list| devices)?|which mic", t):
        print("\n[microphones]\n" + mic_list())
        return "Microphone list printed in the console."
    m = re.fullmatch(r"(?:use (?:mic|microphone)|(?:mic|microphone) use) (?:number )?(\d{1,3})", t)
    if m:
        return _set_mic(m.group(1))
    if re.fullmatch(r"wake (?:debug|scores?)", t):
        wake.WAKE["debug_until"] = time.time() + 30
        return "Showing wake scores in the console for 30 seconds."
    return None


_MISHEAR = {
    "crow": "chrome", "crome": "chrome", "chrom": "chrome", "cron": "chrome",
    "ages": "edge", "age's": "edge", "notpad": "notepad", "not book": "notepad",
    "you tube": "youtube", "utube": "youtube", "poky": "poki", "packy": "poki",
    "spotify app": "spotify", "the calculator": "calculator",
    "rip port": "report", "re port": "report", "repo": "report"
}


def _fix_mishears(text):
    for k, v in _MISHEAR.items():
        text = re.sub(rf"\b{re.escape(k)}\b", v, text, flags=re.I)
    return text


def listen(max_seconds=12, silence_seconds=SPEAK_SILENCE_S, wait_seconds=6) -> str:
    _wait_for_speaker(15)
    _resolve_mic()
    LISTENING["now"] = True
    overlay.ui_state("LISTENING")
    try:
        return _listen_inner(max_seconds, silence_seconds, wait_seconds)
    finally:
        LISTENING["now"] = False
        overlay.ui_level(0)
        overlay.ui_state("IDLE")


def _listen_inner(max_seconds, silence_seconds, wait_seconds):
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        return ""
    vad = _get_vad()
    rate = 16000
    fsize = 480 if vad else 1600
    f_s = fsize / rate
    need, window = (4, 6) if vad else (2, 3)
    pre, recent = deque(maxlen=int(0.6 / f_s)), deque(maxlen=window)
    lvls = deque(maxlen=int(3 / f_s))
    frames, flags = [], []
    started, t_start, max_seen, trig = False, 0.0, 0, ""
    fixed = MIC_THRESHOLD or _ROOM["thresh"]
    end_frames = int(silence_seconds / f_s)
    print("  [listening... speak now]")
    try:
        with sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=fsize, device=MIC_DEVICE_INDEX) as stream:
            t0 = time.time()
            skip = int(0.25 / f_s)
            while True:
                data, _ = stream.read(fsize)
                if skip > 0:
                    skip -= 1
                    continue
                mono = data[:, 0]
                level = int(np.abs(mono.astype(np.int32)).mean())
                max_seen = max(max_seen, level)
                overlay.ui_level(min(level // 100, 100))
                if speech._stop_requested():
                    return ""
                if vad:
                    # Require both WebRTC VAD + minimum voice energy so silent static doesn't hold mic open
                    voiced = bool(vad.is_speech(mono.tobytes(), rate)) and (level > 120)
                    trig = "voice detector"
                else:
                    lvls.append(level)
                    floor = float(np.percentile(lvls, 20)) if len(lvls) >= 4 else float(level)
                    thr = fixed or _energy_trigger(floor)
                    voiced, trig = level > thr, f"loudness {int(thr)}"
                if not started:
                    pre.append(data)
                    recent.append(voiced)
                    if _speech_started(list(recent), need, window):
                        started, t_start = True, time.time()
                        frames.extend(pre)
                        flags = list(recent)
                    elif time.time() - t0 > wait_seconds:
                        print(f"  [heard nothing — loudest {max_seen}]")
                        return ""
                else:
                    frames.append(data)
                    flags.append(voiced)
                    if _speech_ended(flags, end_frames) or time.time() - t_start > max_seconds:
                        break
    except Exception as e:
        print(f"  [mic error: {str(e)[:80]}]")
        return ""
    finally:
        # Stream is now closed — immediately zero out HUD mic meter
        overlay.ui_level(0)

    audio = np.concatenate(frames)
    peak = int(np.abs(audio).max()) if len(audio) else 0
    if 0 < peak < 20000:
        audio = np.clip(audio.astype(np.float32) * min(24000 / peak, 8.0), -32768, 32767).astype(np.int16)
    print(f"  [heard {len(audio)/rate:.1f}s, mic peak {peak}, via {trig}]")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    overlay.ui_state("THINKING")
    try:
        kwargs = dict(file=("speech.wav", buf.getvalue()), model=STT_MODEL, temperature=0, prompt=STT_PROMPT)
        if STT_LANGUAGE:
            kwargs["language"] = STT_LANGUAGE
        r = groqai.groq.audio.transcriptions.create(**kwargs)
        text = (getattr(r, "text", r) or "").strip()
    except Exception as e:
        print(f"  [transcription failed: {str(e)[:80]}]")
        return ""
    text = _fix_mishears(text.strip(" .!?,"))
    return "" if text.lower() in ("", "you", "thank you", "thanks for watching") else text