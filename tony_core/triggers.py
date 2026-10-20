# =====================================================================
# tony_core/triggers.py
# Clap-triggered PRIVILEGED (secret) mode.
#
# Double-clap on the microphone toggles privileged mode:
#   clap x2 -> engage        "Privileged mode engaged, sir."
#   clap x2 -> stand down    "Standing down, sir."
#   idle > PRIVILEGED_IDLE_MINUTES -> auto stand down
#
# This module owns its own microphone stream, independent of ears.py,
# so both listeners can run side by side.
#
# MIC BACKENDS: pyaudio first -- probes candidates in order (CLAP_DEVICE,
# system default input, every input device) and keeps the FIRST that opens,
# at 16 kHz mono (device default rate as fallback). Then sounddevice
# (name-matched, MME -> WASAPI -> DirectSound; WDM-KS last, it does not
# support blocking reads). If nothing opens, retries every 30 seconds.
#
# INTEGRATION (app.py):
#     from tony_core import triggers
#     triggers.init(speak=speech.speak, is_speaking=..., mic_index=ears.MIC_DEVICE_INDEX)
#     triggers.start()                # arm the clap listener
#     triggers.stop()                 # at shutdown
#     triggers.is_privileged()        # True while privileged mode is on
#     triggers.touch()                # call once per user turn (idle timer)
#
# .ENV OPTIONS:
#     CLAP_ENABLED=1                  # 0 disables the listener
#     CLAP_SENSITIVITY=0.6            # 0.1 (strict) - 1.0 (hair trigger)
#     PRIVILEGED_IDLE_MINUTES=15      # auto stand-down timeout
#     CLAP_DEVICE=                    # optional: device index or name substring
#                                     # (e.g. CLAP_DEVICE=1 or CLAP_DEVICE=Realtek)
#
# SELF-TEST (run alone, tune CLAP_SENSITIVITY):
#     python -m tony_core.triggers
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - _STATE: privileged-mode flags (privileged / engaged_at / last_activity)
# =====================================================================

import os
import math
import time
import struct
import threading
import datetime

from tony_core import config

_STATE = {
    "privileged": False,
    "engaged_at": None,
    "last_activity": None,
    "suppress_until": 0.0,   # monotonic time: ignore claps (TTS echo guard)
    "running": False,
    "debug": False,
}

_SPEAK = None        # speak callback injected by app.py
_IS_SPEAKING = None  # optional callable -> True while tony is talking
_MIC_INDEX = None


# ---------------------------------------------------------------------
# Config helpers (config.py attribute -> .env -> default)
# ---------------------------------------------------------------------

def _cfg(name, default):
    v = getattr(config, name, None)
    if v not in (None, ""):
        return v
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _cfg_float(name, default):
    try:
        return float(_cfg(name, default))
    except (TypeError, ValueError):
        return float(default)


def _log_path():
    for attr in ("ACTIONS_LOG", "ACTION_LOG", "LOG_FILE"):
        p = getattr(config, attr, None)
        if p:
            return p
    return os.path.join(str(getattr(config, "HERE", ".")), "tony_actions.log")


def _log(line):
    text = "[{0}] {1}".format(
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line)
    print(text)
    try:
        with open(_log_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write(text + "\n")
    except Exception:
        pass


def _say(text):
    if _SPEAK:
        try:
            _SPEAK(text)
            return
        except Exception:
            pass
    print("(tony) {0}".format(text))


# ---------------------------------------------------------------------
# Privileged state API (used by safety.py / brain.py / app.py)
# ---------------------------------------------------------------------

def init(speak=None, is_speaking=None, mic_index=None):
    """Wire tony's voice into the trigger module. Call once before start()."""
    global _SPEAK, _IS_SPEAKING, _MIC_INDEX
    _SPEAK = speak
    _IS_SPEAKING = is_speaking
    _MIC_INDEX = mic_index


def is_privileged():
    return _STATE["privileged"]


def status():
    if _STATE["privileged"]:
        when = _STATE["engaged_at"]
        return "privileged since {0}".format(when.strftime("%H:%M")) if when else "privileged"
    return "normal"


def touch():
    """Reset the idle timer. Call once per user turn while privileged."""
    if _STATE["privileged"]:
        _STATE["last_activity"] = time.monotonic()


def engage(source="clap"):
    if _STATE["privileged"]:
        return
    _STATE["privileged"] = True
    _STATE["engaged_at"] = datetime.datetime.now()
    _STATE["last_activity"] = time.monotonic()
    _STATE["suppress_until"] = time.monotonic() + 3.5
    _log("PRIVILEGED MODE engaged via {0}".format(source))
    _say("Privileged mode engaged, sir.")


def stand_down(reason="clap"):
    if not _STATE["privileged"]:
        return
    _STATE["privileged"] = False
    _STATE["engaged_at"] = None
    _STATE["last_activity"] = None
    _STATE["suppress_until"] = time.monotonic() + 3.5
    _log("PRIVILEGED MODE stood down ({0})".format(reason))
    if reason.startswith("idle"):
        _say("Privileged mode timed out. Standing down, sir.")
    else:
        _say("Standing down, sir.")


# ---------------------------------------------------------------------
# get_secret tool (registered in tools/__init__.py)
# ---------------------------------------------------------------------

def get_secret(name: str = "") -> str:
    if not _STATE["privileged"]:
        return "Refused: get_secret only works in privileged mode (double-clap)."
    key = (name or "").strip().upper().replace(" ", "_")
    if not key:
        return "Which variable should I read, sir?"
    val = os.getenv(key)
    if val is None:
        g = getattr(config, "_get_env", None)
        if g:
            try:
                val = g(key)
            except Exception:
                val = None
    if val is None:
        return "No secret named {0} found in .env.".format(key)
    return "{0} = {1}".format(key, val)


# ---------------------------------------------------------------------
# Clap detection
# ---------------------------------------------------------------------

class _ClapDetector(object):
    """Transient detector: flags sharp short spikes (claps),
    rejects sustained sound (speech, music, TV)."""

    def __init__(self, sensitivity=0.6):
        s = min(1.0, max(0.1, float(sensitivity)))
        self.ratio = 8.0 - 5.0 * s          # spike must exceed ambient floor x ratio
        self.abs_min = 900.0 - 500.0 * s    # absolute RMS floor (int16 scale)
        self.attack = 2.2                   # must jump vs previous block
        self.max_ring = 0.45                # ringing longer than this = sustained
        self.floor = None
        self.prev_e = 0.0
        self.ringing = False
        self.ring_start = 0.0
        self.spike_e = 0.0
        self.last_clap = 0.0

    def feed(self, rms, t):
        if self.floor is None:
            self.floor = rms
        self.floor += 0.04 * (rms - self.floor)
        evt = None
        if self.ringing:
            decayed = (rms < self.spike_e * 0.25) or (rms < self.floor * 2.5)
            if decayed and (t - self.ring_start) <= self.max_ring:
                evt = "clap"
                self.ringing = False
            elif (t - self.ring_start) > self.max_ring:
                self.ringing = False        # sustained sound -> discard
        elif (rms > max(self.floor * self.ratio, self.abs_min)
              and rms > self.attack * self.prev_e):
            self.ringing = True
            self.ring_start = t
            self.spike_e = rms
        self.prev_e = rms
        return evt


def _rms(pcm_bytes):
    n = len(pcm_bytes) // 2
    if n == 0:
        return 0.0
    acc = 0
    for s in struct.unpack("<{0}h".format(n), pcm_bytes[: n * 2]):
        acc += s * s
    return math.sqrt(acc / n)


# ---------------------------------------------------------------------
# Microphone stream (own stream, independent of ears.py)
# ---------------------------------------------------------------------

def _pa_find_by_name(p, name):
    """Find a PyAudio input device index by name substring."""
    low = name.lower()
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and low in str(info.get("name", "")).lower():
                return i
        except Exception:
            continue
    return p.get_default_input_device_info()["index"]


def _pa_candidates(p):
    """PyAudio device indices to try, best first: CLAP_DEVICE, system
    default input, ears.py's mic, then every other input device."""
    order = []
    env_dev = _cfg("CLAP_DEVICE", "")
    if env_dev not in (None, ""):
        try:
            if str(env_dev).lstrip("-").isdigit():
                order.append(int(env_dev))
            else:
                order.append(int(_pa_find_by_name(p, str(env_dev))))
        except Exception:
            pass
    try:
        order.append(int(p.get_default_input_device_info()["index"]))
    except Exception:
        pass
    if _MIC_INDEX not in (None, ""):
        try:
            order.append(int(_MIC_INDEX))
        except (TypeError, ValueError):
            pass
    try:
        for i in range(p.get_device_count()):
            try:
                if p.get_device_info_by_index(i).get("maxInputChannels", 0) > 0:
                    order.append(i)
            except Exception:
                continue
    except Exception:
        pass
    seen, out = set(), []
    for i in order:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _wanted_mic_names():
    """Names of the physical mic we should prefer (for sounddevice)."""
    names = []
    env_dev = _cfg("CLAP_DEVICE", "")
    if env_dev not in (None, "") and not str(env_dev).lstrip("-").isdigit():
        names.append(str(env_dev))
    if _MIC_INDEX not in (None, ""):
        try:
            import pyaudio
            p = pyaudio.PyAudio()
            info = p.get_device_info_by_index(int(_MIC_INDEX))
            names.append(str(info.get("name", "")))
            p.terminate()
        except Exception:
            pass
    return [n for n in names if n]


def _sd_candidates(sd):
    """sounddevice input device indices to try: name-matched mics first,
    then MME, WASAPI, DirectSound; WDM-KS dead last (it rejects blocking
    reads with 'Blocking API not supported yet')."""
    wanted = [w.lower() for w in _wanted_mic_names()]
    try:
        apis = [str(h["name"]).lower() for h in sd.query_hostapis()]
    except Exception:
        apis = []
    named, mme, was, ds, rest = [], [], [], [], []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            api = d.get("hostapi")
            api_name = apis[api] if isinstance(api, int) and api < len(apis) else ""
            name = str(d.get("name", "")).lower()
            if any(w in name for w in wanted) and "wdm" not in api_name:
                named.append(i)
            elif "mme" in api_name:
                mme.append(i)
            elif "wasapi" in api_name:
                was.append(i)
            elif "directsound" in api_name:
                ds.append(i)
            else:
                rest.append(i)
    except Exception:
        pass
    out = named + mme + was + ds + rest
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def _open_stream():
    """Return (read_fn, block, rate, close_fn). Tries pyaudio (probing every
    candidate device until one opens), then sounddevice."""
    errors = []
    # --- backend 1: pyaudio (tony already ships with it for speech input) ---
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        stream = None
        rate = block = 0
        last_err = None
        for idx in _pa_candidates(p):
            name = "?"
            try:
                name = str(p.get_device_info_by_index(idx).get("name", "?"))
            except Exception:
                pass
            for try_rate in (16000, 0):     # 0 = device default rate
                try:
                    r = try_rate or int(p.get_device_info_by_index(idx)["defaultSampleRate"]) or 16000
                    b = max(256, int(r * 0.032))
                    stream = p.open(format=pyaudio.paInt16, channels=1, rate=int(r),
                                    input=True, frames_per_buffer=b,
                                    input_device_index=idx)
                    rate, block = int(r), b
                    _log("clap mic: using device {0} ({1}) @ {2}Hz".format(idx, name, rate))
                    break
                except Exception as e:
                    last_err = e
            if stream is not None:
                break
            _log("clap mic: device {0} ({1}) refused: {2}".format(idx, name, str(last_err)[:90]))
        if stream is None:
            try:
                p.terminate()
            except Exception:
                pass
            raise RuntimeError("no input device would open (last: {0})".format(last_err))

        def read():
            return stream.read(block, exception_on_overflow=False)

        def close():
            try:
                stream.close()
            finally:
                p.terminate()
        return read, block, rate, close
    except ImportError:
        errors.append("pyaudio missing")
    except Exception as e:
        errors.append("pyaudio: {0}".format(e))
    # --- backend 2: sounddevice (candidate list, WDM-KS last) ---
    try:
        import sounddevice as sd
        stream = None
        last_err = None
        for dev in _sd_candidates(sd):
            try:
                stream = sd.InputStream(samplerate=16000, blocksize=512, channels=1,
                                        dtype="int16", device=dev)
                stream.start()
                _log("clap mic: sounddevice device {0} @ 16000Hz".format(dev))
                break
            except Exception as e:
                last_err = e
        if stream is None:
            raise RuntimeError("no input device would open (last: {0})".format(last_err))

        def read():
            data, _over = stream.read(512)
            return data.tobytes()

        def close():
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        return read, 512, 16000, close
    except ImportError:
        errors.append("sounddevice missing")
    except Exception as e:
        errors.append("sounddevice: {0}".format(e))
    raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------------
# Listener loop
# ---------------------------------------------------------------------

def _listen_loop(read, rate, close, debug=False):
    det = _ClapDetector(_cfg_float("CLAP_SENSITIVITY", 0.6))
    idle_min = _cfg_float("PRIVILEGED_IDLE_MINUTES", 15)
    if debug:
        _log("clap self-test: spike>x{0:.1f} floor, abs_min {1:.0f} - clap twice!".format(
            det.ratio, det.abs_min))
    while _STATE["running"]:
        try:
            data = read()
        except Exception as e:
            _log("clap mic read failed: {0}".format(e))
            time.sleep(1.0)
            continue
        t = time.monotonic()
        rms = _rms(data)
        evt = det.feed(rms, t)
        if _IS_SPEAKING and _IS_SPEAKING():
            evt = None                       # self-echo guard: tony is talking
        if debug:
            bar = "#" * min(60, int(rms / 200))
            print("\r{0:<60} rms={1:>7.0f} floor={2:>7.0f}".format(
                bar, rms, det.floor or 0.0), end="")
        if evt == "clap":
            gap = t - det.last_clap
            if t < _STATE["suppress_until"]:
                pass
            elif 0.12 <= gap <= 0.8:
                det.last_clap = 0.0
                if _STATE["privileged"]:
                    stand_down("clap")
                else:
                    engage("clap")
            else:
                det.last_clap = t
        if _STATE["privileged"] and _STATE["last_activity"]:
            if (time.monotonic() - _STATE["last_activity"]) > idle_min * 60:
                stand_down("idle {0:.0f}min".format(idle_min))
    close()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------

def _bootstrap():
    """Keep trying to open the mic until it works; a busy device at boot
    must not kill the trigger for the whole session."""
    while _STATE["running"]:
        try:
            read, block, rate, close = _open_stream()
        except Exception as e:
            if not _STATE["running"]:
                return
            _log("clap mic unavailable, retrying in 30s: {0}".format(e))
            time.sleep(30.0)
            continue
        _log("clap trigger armed ({0}Hz) - double-clap toggles privileged mode".format(rate))
        _listen_loop(read, rate, close, debug=_STATE["debug"])
        return


def start(debug=False):
    if _STATE["running"]:
        return
    if str(_cfg("CLAP_ENABLED", "1")).strip().lower() in ("0", "false", "no", "off"):
        _log("clap trigger disabled (CLAP_ENABLED=0)")
        return
    _STATE["debug"] = bool(debug)
    _STATE["running"] = True
    threading.Thread(target=_bootstrap, daemon=True).start()


def stop():
    _STATE["running"] = False


if __name__ == "__main__":
    print("CLAP SELF-TEST - clap twice: expect DOUBLE CLAP -> engage, twice more -> stand down.")
    print("Tune CLAP_SENSITIVITY in .env if spikes are missed (higher) or speech triggers (lower).")
    _STATE["running"] = True
    _STATE["debug"] = True
    _bootstrap()