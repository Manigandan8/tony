"""
fix_listening.py  —  patches YOUR tony.py in place so "Hey Jarvis" and the microphone work reliably.

    1. Put this file in the same folder as tony.py
    2. Run:   python fix_listening.py
    3. Restart Tony, then type:  mic doctor

Everything else in your tony.py is left exactly as it is. A backup (tony.py.bak_<time>) is made first,
and nothing is written at all if any required part of your file can't be found.
"""
import sys, os, re, datetime

BLK1 = r'''# ---------- WAKE MODEL + MICROPHONE TOOLS (added by fix_listening.py) ----------
SETTINGS_FILE = os.path.join(HERE, "tony_settings.json")

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

# Which microphone Tony uses. None = Windows default. Change it by typing:  mic list   then   mic use 3
MIC_DEVICE_INDEX = _load_settings().get("mic_device")
TTS_TIMEOUT_S = 12           # a stalled voice request can never freeze Tony (or the microphone) again
WAKE_WARMUP_S = 1.5
WAKE_GAIN = 1.0              # >1 boosts a quiet mic for the wake word only
for _k, _v in (("pause", False), ("reopen", False), ("debug_until", 0.0)):
    WAKE.setdefault(_k, _v)

_ROOM = {"thresh": None}
_VAD = {"obj": None, "tried": False}
_WAKE_MODEL = {"m": None, "err": ""}

def _get_vad():
    """webrtcvad = a real voice detector that ignores steady room noise. Falls back to an adaptive loudness trigger."""
    if not _VAD["tried"]:
        _VAD["tried"] = True
        try:
            import webrtcvad
            _VAD["obj"] = webrtcvad.Vad(2)
            print("[ears] voice-activity detector: ON")
        except Exception:
            print("[ears] tip: run  pip install webrtcvad-wheels  — it hears you much more reliably in a noisy room")
    return _VAD["obj"]

def _speech_started(flags, need=4, window=6):
    return sum(flags[-window:]) >= need

def _speech_ended(flags, frames_needed):
    recent = flags[-frames_needed:]
    return len(recent) >= frames_needed and sum(recent) <= max(1, int(0.08 * frames_needed))

def _energy_trigger(floor):
    """Loudness trigger relative to the current room noise (used only when webrtcvad isn't installed)."""
    return max(floor * 1.8, floor + 300)

def _beep():
    try:
        winsound.Beep(880, 110)      # short and synchronous, so the microphone never records it
    except Exception:
        pass

def _wait_for_speaker(max_s=15):
    """Never open the mic while Tony is still talking — but never wait forever either."""
    t0 = time.time()
    t = _SPEECH_THREAD.get("t")
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(max_s)
    while SPEAKING["now"] and time.time() - t0 < max_s:
        time.sleep(0.05)
    if time.time() - t0 > 1.0:
        print(f"  [waited {time.time()-t0:.0f}s for Tony to finish speaking]")

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
    """Guided: 2 quiet seconds, then 3 seconds of you talking. Sets the fallback trigger and warns if the mic is too weak."""
    try:
        import numpy as np
        _wait_for_speaker(10)
        _beep(); print("  [calibration 1/2: stay QUIET for 2 seconds]")
        quiet = _record_levels(2.0)
        _beep(); print("  [calibration 2/2: now SAY A SENTENCE out loud for 3 seconds]")
        talk = _record_levels(3.0)
        noise = float(np.percentile(quiet, 90))
        speech = float(np.percentile(talk, 75))
        thr = max(int(noise + 0.35 * (speech - noise)), int(noise * 1.15) + 50)
        _ROOM["thresh"] = thr
        print(f"  [mic calibrated: room noise {int(noise)}, your voice {int(speech)}, trigger {thr}]")
        if speech < noise * 1.5:
            print("  [warning: your voice is barely louder than the room noise — raise the mic volume in Windows Sound settings, "
                  "move closer, or use a headset mic]")
        return thr
    except Exception as e:
        print(f"  [mic calibration failed: {str(e)[:60]}]")
        return None

def _get_wake_model():
    if _WAKE_MODEL["m"] is None and not _WAKE_MODEL["err"]:
        try:
            import openwakeword
            from openwakeword.model import Model
            def make():
                return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            try:
                _WAKE_MODEL["m"] = make()
            except Exception:
                print("[wake] downloading the wake-word model (first time only)...")
                openwakeword.utils.download_models()
                _WAKE_MODEL["m"] = make()
        except Exception as e:
            _WAKE_MODEL["err"] = f"{type(e).__name__}: {str(e)[:120]}"
    return _WAKE_MODEL["m"]

def _mic_name():
    try:
        import sounddevice as sd
        idx = MIC_DEVICE_INDEX
        if idx is None:
            idx = sd.default.device[0]
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
            tags = []
            if i == default_in:
                tags.append("(Windows default)")
            if MIC_DEVICE_INDEX == i:
                tags.append("<-- Tony uses this")
            rows.append(f"   {i:>3}: {d['name'][:46]:<46} [{api}] {' '.join(tags)}")
    return "\n".join(rows) if rows else "   (no input devices found)"

def _set_mic(idx):
    global MIC_DEVICE_INDEX
    MIC_DEVICE_INDEX = int(idx)
    _save_settings(mic_device=MIC_DEVICE_INDEX)
    WAKE["reopen"] = True
    return f"Using microphone {MIC_DEVICE_INDEX}."

def mic_doctor(seconds=5):
    """One command that shows exactly where listening breaks: device, loudness, voice detector, wake score."""
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        return "The sounddevice package is missing."
    WAKE["pause"] = True
    time.sleep(0.4)                       # let the wake thread release the microphone
    _wait_for_speaker(10)
    try:
        print("\n[mic doctor] ------------------------------------------------")
        print(f" microphone Tony uses: {_mic_name()}")
        print(" input devices:\n" + mic_list())
        model, vad = _get_wake_model(), _get_vad()
        if model is None:
            print(f" wake engine: NOT loaded ({_WAKE_MODEL['err']}) -> pip install openwakeword onnxruntime")
        ui_state("LISTENING")
        _beep()
        print(f"\n >> Say 'Hey Jarvis' clearly, a few times, for the next {seconds} seconds <<")
        levels, voiced, scores = [], [], []
        try:
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280,
                                device=MIC_DEVICE_INDEX) as s:
                for i in range(int(seconds / 0.08)):
                    d, _ = s.read(1280)
                    x = d[:, 0]
                    lvl = int(np.abs(x.astype(np.int32)).mean())
                    levels.append(lvl)
                    if vad:
                        voiced.append(any(vad.is_speech(x[j:j + 480].tobytes(), 16000) for j in (0, 480)))
                    sc = 0.0
                    if model:
                        try:
                            sc = float(max(model.predict(x).values()))
                        except Exception:
                            sc = 0.0
                    scores.append(sc)
                    if i % 3 == 0:
                        print(f"   level {lvl:>5} {'#' * min(40, lvl // 250):<40} wake {sc:.2f}")
        except Exception as e:
            print(f" !! couldn't open the microphone: {str(e)[:100]}\n    try: mic list   then   mic use <number>")
            return "I can't open the microphone."
        peak_level = max(levels) if levels else 0
        peak_score = max(scores) if scores else 0.0
        print(f"\n loudness: quietest {int(np.percentile(levels, 10))}, loudest {peak_level}")
        if vad:
            print(f" voice detector heard speech in {100 * sum(voiced) // max(1, len(voiced))}% of the time")
        print(f" wake score: peak {peak_score:.2f}  (needs more than {WAKE_THRESHOLD})")
        if peak_level < 300:
            print(" VERDICT: the microphone is almost silent. It is muted, turned way down, or Tony is using the wrong device.\n"
                  "          Fix: type  mic list  then  mic use <number>  (try the entry named Microphone / Array), and check the Windows mic volume.")
            return "The microphone sounds silent."
        if model is None:
            print(" VERDICT: the mic works, but the wake engine did not load. Run: pip install openwakeword onnxruntime")
            return "The wake engine isn't loaded."
        if peak_score > WAKE_THRESHOLD:
            print(" VERDICT: all good. 'Hey Jarvis' would wake Tony.")
            return "Wake word works."
        print(f" VERDICT: the mic works, but the wake model did not recognise 'Hey Jarvis' (peak {peak_score:.2f}).\n"
              f"          Try: say it clearly like 'Hey JAR-vis' about 20 cm from the mic; lower WAKE_THRESHOLD to about "
              f"{max(0.2, round(peak_score * 0.7, 2))}; or set WAKE_GAIN = 2.0 if your mic is quiet. Pressing Enter always works.")
        return "I heard you, but not the wake word."
    finally:
        try:
            if _WAKE_MODEL["m"] is not None:
                _WAKE_MODEL["m"].reset()
        except Exception:
            pass
        WAKE["pause"] = False
        ui_state("IDLE")

def _mic_command(msg):
    """Typed or spoken mic commands: mic doctor / mic list / mic use N / calibrate mic / wake debug."""
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (msg or "").lower())).strip()
    t = re.sub(r"^(?:(?:hey|ok|okay) )?(?:tony|jarvis) ", "", t)
    t = re.sub(r"^(?:please|can you|could you) ", "", t)
    if re.fullmatch(r"calibrate (?:the )?(?:mic|microphone)", t):
        calibrate_mic()
        return "Microphone calibrated."
    if re.fullmatch(r"(?:mic|microphone|wake(?: word)?) (?:doctor|test|check)|(?:test|check) (?:the )?(?:mic|microphone|wake word)", t):
        return mic_doctor()
    if re.fullmatch(r"(?:list )?(?:mic|mics|microphones?)(?: list| devices)?|which mic", t):
        try:
            print("\n[microphones]\n" + mic_list() + "\n  (use one with:  mic use <number>)")
            return "Microphone list printed in the console."
        except Exception as e:
            return f"Couldn't list microphones: {str(e)[:50]}"
    m = re.fullmatch(r"(?:use (?:mic|microphone)|(?:mic|microphone) use) (?:number )?(\d{1,3})", t)
    if m:
        return _set_mic(m.group(1))
    if re.fullmatch(r"wake (?:debug|scores?)", t):
        WAKE["debug_until"] = time.time() + 30
        return "Showing wake scores in the console for 30 seconds."
    return None
'''

BLK2 = r'''def listen(max_seconds=12, silence_seconds=SPEAK_SILENCE_S, wait_seconds=12) -> str:
    _wait_for_speaker(15)
    LISTENING["now"] = True
    ui_state("LISTENING")
    try:
        result = _listen_inner(max_seconds, silence_seconds, wait_seconds)
    finally:
        LISTENING["now"] = False
    return result

def _listen_inner(max_seconds, silence_seconds, wait_seconds):
    try:
        import sounddevice as sd, numpy as np, wave
    except ImportError:
        print("  [voice input needs: pip install sounddevice numpy]")
        return ""
    from collections import deque
    vad = _get_vad()
    rate = 16000
    fsize = 480 if vad else 1600                 # 30 ms frames for the detector, 100 ms for the loudness fallback
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
            skip = int(0.25 / f_s)               # drop the mic's start-up pop
            while True:
                data, _ = stream.read(fsize)
                if skip > 0:
                    skip -= 1
                    continue
                mono = data[:, 0]
                level = int(np.abs(mono.astype(np.int32)).mean())
                max_seen = max(max_seen, level)
                ui_level(min(level // 100, 100))
                if _stop_requested():
                    return ""
                if vad:
                    voiced = bool(vad.is_speech(mono.tobytes(), rate))
                    trig = "voice detector"
                else:
                    lvls.append(level)
                    floor = float(np.percentile(lvls, 20)) if len(lvls) >= 4 else float(level)
                    thr = fixed or _energy_trigger(floor)
                    voiced = level > thr
                    trig = f"loudness {int(thr)}"
                if not started:
                    pre.append(data)
                    recent.append(voiced)
                    if _speech_started(list(recent), need, window):
                        started, t_start = True, time.time()
                        frames.extend(pre)
                        flags = list(recent)
                    elif time.time() - t0 > wait_seconds:
                        print(f"  [heard nothing — loudest level {max_seen}, trigger: {trig}. "
                              f"If your voice should have counted, type: mic doctor]")
                        return ""
                else:
                    frames.append(data)
                    flags.append(voiced)
                    if _speech_ended(flags, end_frames) or time.time() - t_start > max_seconds:
                        break
    except Exception as e:
        print(f"  [mic error: {str(e)[:80]}]")
        return ""
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
    try:
        kwargs = dict(file=("speech.wav", buf.getvalue()), model=STT_MODEL, temperature=0, prompt=STT_PROMPT)
        if STT_LANGUAGE:
            kwargs["language"] = STT_LANGUAGE
        r = groq.audio.transcriptions.create(**kwargs)
        text = (getattr(r, "text", r) or "").strip()
    except Exception as e:
        print(f"  [transcription failed: {str(e)[:80]}]")
        return ""
    text = _fix_mishears(text.strip(" .!?,"))
    if text.lower() in ("", "you", "thank you", "thanks for watching"):
        return ""
    return text
'''

BLK3 = r'''# ---------- WAKE WORD THREAD ----------
def _listen_for_wake(sd, np, model, first):
    """Open the mic ONLY while waiting for the wake word. Returns the score when heard, or 0.0 if the mic was
    needed by something else (so the stream is closed and nothing ever fights over the device)."""
    CH = 1280
    warm = int(WAKE_WARMUP_S / 0.08) if first else 3
    try:
        model.reset()
    except Exception:
        pass
    last_dbg = 0.0
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CH, device=MIC_DEVICE_INDEX) as stream:
        if first:
            print(f"[wake] ready — say 'Hey Jarvis'  (mic {_mic_name()}, trigger {WAKE_THRESHOLD})")
        while True:
            if SPEAKING["now"] or LISTENING["now"] or WAKE["pause"] or WAKE["reopen"]:
                WAKE["reopen"] = False
                return 0.0
            data, _ = stream.read(CH)
            x = data[:, 0]
            if WAKE_GAIN != 1.0:
                x = np.clip(x.astype(np.float32) * WAKE_GAIN, -32768, 32767).astype(np.int16)
            scores = model.predict(x)
            if warm > 0:
                warm -= 1
                continue
            top = float(max(scores.values())) if scores else 0.0
            if time.time() < WAKE["debug_until"] and top > 0.05 and time.time() - last_dbg > 0.3:
                last_dbg = time.time()
                print(f"   [wake score {top:.2f}]")
            if top > WAKE_THRESHOLD and time.time() - WAKE["last"] > WAKE_REFRACTORY_S:
                WAKE["last"] = time.time()
                return top

def _wake_loop():
    model = _get_wake_model()
    if model is None:
        print(f"[wake] unavailable: {_WAKE_MODEL['err'] or 'unknown error'}\n"
              f"       fix: pip install openwakeword onnxruntime  (then restart). Press Enter to talk meanwhile.")
        return
    try:
        import sounddevice as sd, numpy as np
    except ImportError:
        print("[wake] disabled — needs: pip install sounddevice numpy")
        return
    first, fails = True, 0
    while True:
        while SPEAKING["now"] or LISTENING["now"] or WAKE["pause"]:
            time.sleep(0.1)
        try:
            score = _listen_for_wake(sd, np, model, first)
            first, fails = False, 0
        except Exception as e:
            fails += 1
            print(f"[wake] microphone problem ({_mic_name()}): {str(e)[:100]}\n"
                  f"       type  mic doctor  to diagnose, or  mic list  /  mic use <number>")
            if fails >= 3:
                print("[wake] giving up for now — press Enter to talk, or fix the mic and restart.")
                return
            time.sleep(3)
            continue
        if not score:
            continue                              # the mic was needed elsewhere; reopen when it is free
        if not TONY_BUSY.acquire(blocking=False):
            continue
        try:
            print(f"[wake] heard 'Hey Jarvis' (score {score:.2f})")
            STOP["now"] = False
            _beep()
            time.sleep(0.2)                       # let the beep die away before the mic opens
            ui_state("WAKE")
            msg = listen()
            if msg:
                print(f"You (wake): {msg}")
                if re.search(r"\b(tony\s+)?stop\b|\b(cancel|abort)\b", msg, re.I):
                    STOP["now"] = True
                    print("  [STOP requested]")
                    speak("Stopped, sir.")
                    continue
                ack_async()
                fp = fast_path(msg)
                if fp is not None:
                    wait_ack()
                    print("Tony:", fp)
                    ui_reply(fp)
                    speak(fp)
                else:
                    answer = ask_tony(msg)
                    wait_ack()
                    print("Tony:", answer)
                    spoken = core_reply(answer, detail=wants_detail(msg))
                    ui_reply(spoken)
                    speak(spoken)
        except Exception as e:
            print(f"  [wake command failed: {str(e)[:60]}]")
        finally:
            TONY_BUSY.release()
            ui_state("IDLE")
'''


def region(s, start_pat, end_pat, new, label, problems):
    a = re.search(start_pat, s, re.M)
    if not a:
        problems.append(f"could not find the start of: {label}")
        return s
    b = re.compile(end_pat, re.M).search(s, a.end())
    if not b:
        problems.append(f"could not find the end of: {label}")
        return s
    if not new.endswith("\n\n"):
        new = new.rstrip("\n") + "\n\n"
    return s[:a.start()] + new + s[b.start():]


def optional(s, pat, repl, label, notes, flags=re.M):
    new, n = re.subn(pat, repl, s, count=1, flags=flags)
    notes.append(("applied" if n else "SKIPPED (not found)") + f": {label}")
    return new


def patch(text):
    """Returns (new_text, problems, notes)."""
    problems, notes = [], []
    crlf = "\r\n" in text
    s = text.replace("\r\n", "\n")
    if "WAKE MODEL + MICROPHONE TOOLS" in s:
        return text, ["already patched — nothing to do"], notes

    # 1) microphone tools + settings + guided calibration (replaces your old calibrate_mic)
    s = region(s, r"^MIC_DEVICE_INDEX\s*=\s*None[^\n]*\n", r"^_MISHEAR\s*=\s*\{", BLK1,
               "microphone settings / calibrate_mic", problems)
    # 2) listen() + _listen_inner()  (voice-activity detection)
    s = region(s, r"^def listen\(", r"^# -{5,} WAKE WORD THREAD -{5,}", BLK2, "listen()", problems)
    # 3) the wake-word thread
    s = region(s, r"^# -{5,} WAKE WORD THREAD -{5,}", r"^# -{5,} BRIEFINGS -{5,}", BLK3, "wake-word thread", problems)
    if problems:
        return text, problems, notes

    # config + small edits (each reported, none fatal)
    s = optional(s, r"^MIC_THRESHOLD\s*=\s*\d+[^\n]*$",
                 "MIC_THRESHOLD = None         # None = automatic (voice detector / adaptive). A number forces a fixed loudness trigger.",
                 "MIC_THRESHOLD -> automatic", notes)
    s = optional(s, r"^WAKE_THRESHOLD\s*=\s*[\d.]+[^\n]*$", "WAKE_THRESHOLD = 0.2", "WAKE_THRESHOLD -> 0.2 (headset attempts dip to 0.24; noise stays under 0.07)", notes)
    s = optional(s, r"^WAKE_REFRACTORY_S\s*=\s*\d+[^\n]*$", "WAKE_REFRACTORY_S = 4", "WAKE_REFRACTORY_S 20 -> 4", notes)
    s = optional(s, r"await edge_tts\.Communicate\(([^\n]*?)\)\.save\(tmp\)",
                 r"await asyncio.wait_for(edge_tts.Communicate(\1).save(tmp), timeout=TTS_TIMEOUT_S)",
                 "voice requests time out instead of freezing Tony", notes)
    s = optional(s, r"^def fast_path\(msg: str\):\n",
                 "def fast_path(msg: str):\n    _r = _mic_command(msg)\n    if _r is not None:\n        return _r\n",
                 "mic commands (mic doctor / mic list / mic use N / calibrate mic / wake debug)", notes)
    s = optional(s, r"^calibrate_mic\(\)\n(?=prepare_voice_cache\()", "_get_vad()\n",
                 "no blind noise calibration at startup", notes)
    s = optional(s, r'^speak\("Tony online\. All systems nominal\."\)',
                 'say_async("Tony online. All systems nominal.")',
                 "greeting no longer blocks the start-up (wake thread starts immediately)", notes)

    if crlf:
        s = s.replace("\n", "\r\n")
    try:
        compile(s.replace("\r\n", "\n"), "tony.py", "exec")
    except SyntaxError as e:
        return text, [f"the patched file would not compile ({e}) — nothing was changed"], notes
    return s, [], notes


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tony.py"
    if not os.path.exists(path):
        print(f"Can't find {path}. Put fix_listening.py in the same folder as tony.py and run it from there.")
        return 1
    with open(path, "rb") as f:
        text = f.read().decode("utf-8")
    new, problems, notes = patch(text)
    if problems:
        print("NOTHING WAS CHANGED:")
        for p in problems:
            print("  -", p)
        return 1
    bak = f"{path}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    with open(bak, "wb") as f:
        f.write(text.encode("utf-8"))
    with open(path, "wb") as f:
        f.write(new.encode("utf-8"))
    print(f"Patched {path}  (backup: {bak})")
    for n in notes:
        print("  -", n)
    print("\nNext: restart Tony, then type:  mic doctor")
    print("Optional but recommended:  pip install webrtcvad-wheels")
    return 0


if __name__ == "__main__":
    sys.exit(main())