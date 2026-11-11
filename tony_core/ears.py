# =====================================================================
# tony_core/ears.py
# Ear & Microphone: Decoupled Zero-Blind-Spot Audio Capture,
# Background Whisper STT, Dynamic Voice-Drop Endpointing, and Barge-In.
# =====================================================================

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
from tony_core import audio_hub
from tony_core.providers import groqai

LISTENING = {"now": False}
BARGE_IN = {"triggered": False, "pre_roll": []}
CONTINUOUS_EAR = {"active": False}

_ROLLING_NOISE_LVLS = deque(maxlen=100)

VOICE_INPUT = config.VOICE_INPUT
STT_LANGUAGE = config.STT_LANGUAGE
STT_ACCURACY = config.STT_ACCURACY
MIC_THRESHOLD = config.MIC_THRESHOLD
SPEAK_SILENCE_S = 0.65
STT_PROMPT = config.STT_PROMPT


def _pick_stt_model():
    order = ("whisper-large-v3-turbo", "whisper-large-v3")
    for p in order:
        if p in groqai.AVAILABLE_MODELS:
            return p
    return order[0]


STT_MODEL = "whisper-large-v3-turbo"


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


_raw_mic = _load_settings().get("mic_device")
MIC_DEVICE_INDEX = _raw_mic if (isinstance(_raw_mic, int) and _raw_mic >= 0) else None
_MIC_FAIL = {"until": 0.0}


def _resolve_mic():
    global MIC_DEVICE_INDEX
    old_device = MIC_DEVICE_INDEX
    if isinstance(MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX < 0:
        MIC_DEVICE_INDEX = None
    if time.time() < _MIC_FAIL["until"]:
        return
    name = _load_settings().get("mic_device_name", "")
    if name:
        try:
            import sounddevice as sd
            matches = []
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and name.lower() in d["name"].lower():
                    api = sd.query_hostapis(d["hostapi"])["name"]
                    matches.append((0 if "WASAPI" in api else 1, i))
            MIC_DEVICE_INDEX = sorted(matches)[0][1] if matches else None
        except Exception:
            MIC_DEVICE_INDEX = None
    if old_device != MIC_DEVICE_INDEX and audio_hub.get_audio_hub().running:
        try:
            audio_hub.get_audio_hub().restart(device=MIC_DEVICE_INDEX)
            print(f"[audio] shared microphone switched to device {MIC_DEVICE_INDEX}")
        except Exception as e:
            print(f"[audio] microphone switch failed: {str(e)[:80]}")


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
    return len(flags) >= need and sum(flags[-window:]) >= need


def _speech_ended(flags, frames_needed):
    if len(flags) < frames_needed:
        return False
    recent = flags[-frames_needed:]
    tail_quiet = sum(flags[-min(8, frames_needed):]) == 0
    return tail_quiet and sum(recent) <= max(2, int(0.18 * frames_needed))


def _energy_trigger(floor):
    return max(floor * 1.75, floor + 320, 400)


def _speaker_index():
    try:
        import sounddevice as sd
        best = None
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0 and "speakers" in d["name"].lower():
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
        t = np.arange(int(rate * 0.08)) / rate
        env = np.minimum(1.0, np.minimum(t / 0.015, (0.08 - t) / 0.025))
        tone = (0.12 * env * np.sin(2 * np.pi * 740 * t) * 32767).astype(np.int16)
        sd.play(tone, rate, device=dev, blocking=True)
    except Exception:
        pass


def _wait_for_speaker(max_s=15):
    t0 = time.time()
    t = speech._SPEECH_THREAD.get("t")
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(max_s)
    while speech.SPEAKING["now"] and time.time() - t0 < max_s:
        time.sleep(0.03)


def watch_for_barge_in():
    """Speaker-calibrated Barge-In: ignores Tony's own laptop speaker output, interrupts only on loud human voice."""
    if not getattr(config, "BARGE_IN_ENABLED", True) or CONTINUOUS_EAR["active"]:
        return
    try:
        import numpy as np
    except ImportError:
        return

    vad = _get_vad()
    rate = 16000
    pre_buf = deque(maxlen=16)
    bleed_window = deque(maxlen=50)
    voiced_streak = 0
    hub = audio_hub.get_audio_hub()
    sub = None
    legacy_stream = None

    try:
        if hub.running:
            sub = hub.subscribe("barge-in", max_frames=80)
            read_frame = lambda: np.frombuffer(sub.read(timeout=1.0), dtype=np.int16)
        else:
            import sounddevice as sd
            dev = MIC_DEVICE_INDEX if (isinstance(MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX >= 0) else None
            legacy_stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=480, device=dev)
            legacy_stream.start()
            read_frame = lambda: legacy_stream.read(480)[0][:, 0].copy()

        t0 = time.monotonic()
        while speech.SPEAKING["now"] and not speech.STOP["now"] and not speech.OVERLAY_STOP["now"]:
            try:
                mono = read_frame()
            except TimeoutError:
                continue
            if mono.size != 480:
                continue
            abs_m = np.abs(mono.astype(np.int32))
            lvl = int(abs_m.mean())
            pk = int(abs_m.max())
            pre_buf.append(mono.copy())

            elapsed = time.monotonic() - t0
            if elapsed < 0.45:
                bleed_window.append(lvl)
                continue

            bleed_floor = float(np.percentile(bleed_window, 90)) if bleed_window else 600.0
            # Require voice to be 2.6x louder than speaker bleed (and peak > 6500) for 7 frames (~210ms)
            barge_thresh = max(bleed_floor * 2.6, bleed_floor + 1400, 1900)
            is_vad = bool(vad.is_speech(mono.tobytes(), rate)) if vad else True

            if is_vad and lvl > barge_thresh and pk > 6500:
                voiced_streak += 1
                if voiced_streak >= 7:
                    print("\n  [barge-in: heard you speaking — interrupting Tony!]")
                    BARGE_IN["pre_roll"] = [f.reshape(-1, 1) for f in pre_buf]
                    BARGE_IN["triggered"] = True
                    speech.stop_speech(from_overlay=False)
                    return
            else:
                voiced_streak = max(0, voiced_streak - 1)
                if lvl < barge_thresh:
                    bleed_window.append(lvl)
    except Exception:
        pass
    finally:
        if sub is not None:
            sub.close()
        if legacy_stream is not None:
            try:
                legacy_stream.stop()
                legacy_stream.close()
            except Exception:
                pass


def _record_levels(seconds, skip=0.3):
    import numpy as np
    fs, levels = 1600, []
    hub = audio_hub.get_audio_hub()
    if hub.running:
        with hub.subscribe("mic-calibration", max_frames=160) as sub:
            for i in range(int((seconds + skip) / 0.1)):
                d = np.frombuffer(sub.read_samples(fs, timeout=2.0), dtype=np.int16)
                if i * 0.1 >= skip:
                    levels.append(int(np.abs(d.astype(np.int32)).mean()))
        return levels

    import sounddevice as sd
    dev = MIC_DEVICE_INDEX if (isinstance(MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX >= 0) else None
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=fs, device=dev) as stream:
        for i in range(int((seconds + skip) / 0.1)):
            d, _ = stream.read(fs)
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
    if audio_hub.get_audio_hub().running:
        try:
            audio_hub.get_audio_hub().restart(device=MIC_DEVICE_INDEX)
        except Exception as e:
            return f"Saved microphone {MIC_DEVICE_INDEX}, but switching failed: {str(e)[:70]}"
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
        model = wake._get_wake_model()
        _get_vad()
        overlay.ui_state("LISTENING")
        _beep()
        print(f"\n >> Say 'Hey Jarvis' clearly for the next {seconds} seconds <<")
        levels, scores = [], []
        hub = audio_hub.get_audio_hub()
        if hub.running:
            with hub.subscribe("mic-doctor", max_frames=160) as sub:
                for i in range(int(seconds / 0.08)):
                    x = np.frombuffer(sub.read_samples(1280, timeout=2.0), dtype=np.int16)
                    lvl = int(np.abs(x.astype(np.int32)).mean())
                    levels.append(lvl)
                    sc = float(max(model.predict(x).values())) if model else 0.0
                    scores.append(sc)
                    if i % 3 == 0:
                        print(f"   level {lvl:>5} {'#' * min(40, lvl // 250):<40} wake {sc:.2f}")
        else:
            dev = MIC_DEVICE_INDEX if (isinstance(MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX >= 0) else None
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280, device=dev) as stream:
                for i in range(int(seconds / 0.08)):
                    d, _ = stream.read(1280)
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
    "crow": "chrome", "crome": "chrome", "chrom": "chrome",
    "notpad": "notepad", "not book": "notepad",
    "you tube": "youtube", "utube": "youtube", "view table": "youtube", "view tube": "youtube",
    "you too": "youtube", "youth tube": "youtube",
    "braille": "brave", "brief": "brave", "grave": "brave",
    "poky": "poki", "packy": "poki",
    "spotify app": "spotify", "the calculator": "calculator",
    "rip port": "report", "re port": "report"
}

_HALLUCINATIONS = {
    "", "you", "thank you", "thanks for watching", "thank you for watching",
    "bye", "subtitles by", "amara.org", "please subscribe", "like and subscribe",
    "voice commands for tony", "wake word hey jarvis",
    "natural spoken conversation and desktop commands"
}


def _fix_mishears(text: str) -> str:
    for k, v in _MISHEAR.items():
        text = re.sub(rf"\b{re.escape(k)}\b", v, text, flags=re.I)
    parts = [p.strip() for p in re.split(r"[.!?]+", text) if p.strip()]
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        return parts[0]
    return text


def capture_utterance_wav(max_seconds=9.0, silence_seconds=SPEAK_SILENCE_S, wait_seconds=12.0, is_followup=False) -> bytes | None:
    """
    Captures a single spoken utterance and returns the raw WAV bytes in 0.0ms
    WITHOUT blocking on the network Whisper upload!
    Also handles live Barge-In if the user starts speaking while Tony is talking!
    """
    if speech.OVERLAY_STOP["now"]:
        return None
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        return None

    _resolve_mic()
    vad = _get_vad()
    rate = 16000
    fsize = 480  # 30 ms frames
    f_s = fsize / rate
    need, window = (4, 6) if vad else (3, 4)
    pre, recent = deque(maxlen=int(0.45 / f_s)), deque(maxlen=window)
    frames, flags = [], []
    started, t_start, max_seen, trig = False, 0.0, 0, ""
    voiced_count = 0
    voice_peak_ema = 0.0
    bleed_window = deque(maxlen=40)

    if BARGE_IN["triggered"]:
        for pr_frame in BARGE_IN["pre_roll"][-12:]:
            pre.append(pr_frame)
        BARGE_IN["pre_roll"] = []
        BARGE_IN["triggered"] = False
        trig = "barge-in"

    fixed = MIC_THRESHOLD or _ROOM["thresh"]
    end_frames = int(0.65 / f_s)
    dev = MIC_DEVICE_INDEX if (isinstance(MIC_DEVICE_INDEX, int) and MIC_DEVICE_INDEX >= 0) else None

    LISTENING["now"] = True
    if not speech.SPEAKING["now"]:
        overlay.ui_state("LISTENING")

    try:
        hub = audio_hub.get_audio_hub()
        subscription = None
        stream = None
        if hub.running:
            subscription = hub.subscribe("utterance-listener", max_frames=200)
            read_frame = lambda: np.frombuffer(subscription.read(timeout=2.0), dtype=np.int16)
        else:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=fsize, device=dev)
            stream.start()
            read_frame = lambda: stream.read(fsize)[0][:, 0].copy()

        try:
            t0 = time.time()
            while True:
                if speech.OVERLAY_STOP["now"]:
                    return None
                mono = read_frame()
                if mono.size != fsize:
                    continue
                abs_mono = np.abs(mono.astype(np.int32))
                level = int(abs_mono.mean())
                peak_f = int(abs_mono.max())
                max_seen = max(max_seen, level)

                # If Tony is currently speaking, require Barge-In threshold above speaker bleed!
                if speech.SPEAKING["now"] and not started:
                    bleed_window.append(level)
                    bleed_floor = float(np.percentile(bleed_window, 80)) if len(bleed_window) >= 6 else 450.0
                    start_gate = max(bleed_floor * 1.75, bleed_floor + 550, 820)
                    req_peak = 2200
                else:
                    if not started or len(_ROLLING_NOISE_LVLS) < 15:
                        _ROLLING_NOISE_LVLS.append(level)
                    floor = float(np.percentile(_ROLLING_NOISE_LVLS, 30)) if len(_ROLLING_NOISE_LVLS) >= 6 else 350.0
                    start_gate = fixed or max(floor * 1.75, floor + 350, 420)
                    req_peak = 1200

                floor_base = float(np.percentile(_ROLLING_NOISE_LVLS, 30)) if len(_ROLLING_NOISE_LVLS) >= 6 else 350.0
                dynamic_keep_gate = max(floor_base * 1.45, floor_base + 220, voice_peak_ema * 0.32, 340)

                if not speech.SPEAKING["now"]:
                    overlay.ui_level(min(level // 100, 100))

                is_vad_speech = bool(vad.is_speech(mono.tobytes(), rate)) if vad else True
                if not started:
                    voiced = is_vad_speech and (level > start_gate) and (peak_f > req_peak)
                else:
                    voiced = is_vad_speech and (level > dynamic_keep_gate) and (peak_f > 1000)
                    if voiced:
                        voice_peak_ema = max(voice_peak_ema * 0.92, float(level))

                if not trig:
                    trig = "voice detector" if vad else f"loudness {int(start_gate)}"

                now = time.time()
                frame_2d = mono.reshape(-1, 1)

                if not started:
                    pre.append(frame_2d)
                    recent.append(voiced)
                    if _speech_started(list(recent), need, window):
                        # If Tony was speaking when user started talking, cut off Tony's speech immediately!
                        if speech.SPEAKING["now"]:
                            print("\n  [barge-in: heard you speaking — interrupting Tony!]")
                            speech.stop_speech(from_overlay=False)
                            trig = "barge-in"
                        overlay.ui_state("LISTENING")
                        started, t_start = True, now
                        voice_peak_ema = float(level)
                        frames.extend(pre)
                        flags = list(recent)
                        voiced_count = sum(1 for v in flags if v)
                    elif now - t0 > wait_seconds:
                        return None
                else:
                    frames.append(frame_2d)
                    flags.append(voiced)
                    if voiced:
                        voiced_count += 1

                    if _speech_ended(flags, end_frames):
                        if voiced_count < 7 and (now - t0) < (wait_seconds - 1.0):
                            started = False
                            frames.clear()
                            flags.clear()
                            recent.clear()
                            voiced_count = 0
                            voice_peak_ema = 0.0
                            continue
                        break
                    if now - t_start > max_seconds:
                        break
        finally:
            if subscription is not None:
                subscription.close()
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"  [mic error: {str(e)[:80]}]")
        return None
    finally:
        LISTENING["now"] = False
        overlay.ui_level(0)

    if speech.OVERLAY_STOP["now"] or not frames or voiced_count < 5:
        return None
    audio = np.concatenate(frames)
    peak = int(np.abs(audio).max()) if len(audio) else 0
    if 0 < peak < 18000:
        audio = np.clip(audio.astype(np.float32) * min(20000 / peak, 4.0), -32768, 32767).astype(np.int16)
    print(f"  [heard {len(audio)/rate:.1f}s, mic peak {peak}, via {trig}]")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


# Matches only English/Latin characters, Tamil script (\u0B80-\u0BFF), numbers, and standard punctuation
_ALLOWED_SCRIPT_RE = re.compile(r"^[\u0000-\u024F\u0B80-\u0BFF\u2000-\u206F\s\d.,!?'\";:()\-+*/=@#$%&]+$")


def transcribe_wav_bytes(wav_bytes: bytes, filter_echo: bool = True) -> str:
    """Transcribes captured WAV bytes in English or Tamil (தமிழ்) and blocks foreign-script hallucinations."""
    if not wav_bytes or speech.OVERLAY_STOP["now"]:
        return ""
    overlay.ui_state("THINKING")
    try:
        kwargs = dict(file=("speech.wav", wav_bytes), model=STT_MODEL, temperature=0, prompt=STT_PROMPT)
        if STT_LANGUAGE:
            kwargs["language"] = STT_LANGUAGE
        r = groqai.groq.audio.transcriptions.create(**kwargs)
        text = (getattr(r, "text", r) or "").strip()

        # If auto-detect hallucinated a non-English / non-Tamil script (e.g. Korean/Russian/Arabic on noise),
        # re-run with English forced so it never outputs random foreign scripts!
        if text and not _ALLOWED_SCRIPT_RE.match(text):
            kwargs["language"] = "en"
            r = groqai.groq.audio.transcriptions.create(**kwargs)
            text = (getattr(r, "text", r) or "").strip()
    except Exception as e:
        print(f"  [transcription failed: {str(e)[:80]}]")
        return ""

    text = _fix_mishears(text.strip(" .!?,"))
    low = text.lower()
    if low in _HALLUCINATIONS or "bilingual conversation and desktop commands" in low or "வணக்கம் ஜார்விஸ் எப்படி இருக்கீங்க" in low:
        return ""
    text = re.sub(r"^(?:hey\s+|ok\s+|okay\s+|ஏய்\s+|ஹே\s+)?(?:jarvis|tony|ஜார்விஸ்|டோனி)[,\s]+", "", text, flags=re.I).strip()
    if filter_echo and speech.is_self_echo(text):
        print(f"  [echo-guard: ignored Tony's own speaker echo '{text[:50]}']")
        return ""
    return text


def listen(max_seconds=9.0, silence_seconds=SPEAK_SILENCE_S, wait_seconds=12, is_followup=False) -> str:
    """Waits for Tony to finish speaking (unless barge-in triggered), then listens and transcribes."""
    if speech.OVERLAY_STOP["now"]:
        return ""
    if not BARGE_IN["triggered"]:
        _wait_for_speaker(15)
    speech.STOP["now"] = False
    wav_bytes = capture_utterance_wav(
        max_seconds=min(float(max_seconds), 9.0),
        silence_seconds=silence_seconds,
        wait_seconds=wait_seconds,
        is_followup=is_followup
    )
    if not wav_bytes:
        return ""
    return transcribe_wav_bytes(wav_bytes, filter_echo=is_followup)