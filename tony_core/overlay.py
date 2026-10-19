# =====================================================================
# tony_core/overlay.py
# UI overlay window and thinking popup.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - UI: dict {"state": ..., "reply": ..., "level": ..., "queue": ...}
# =====================================================================

import queue

UI = {"state": "OFFLINE", "reply": "", "level": 0, "queue": queue.Queue(maxsize=50)}

def ui_state(s):
    UI["state"] = s
    try: UI["queue"].put_nowait(("state", s))
    except Exception: pass

def ui_reply(s):
    UI["reply"] = s
    try: UI["queue"].put_nowait(("reply", s))
    except Exception: pass

def ui_level(v):
    UI["level"] = v
    try: UI["queue"].put_nowait(("level", v))
    except Exception: pass

def _overlay_app():
    from tony_core import speech
    try: import tkinter as tk
    except ImportError: return
    try:
        root = tk.Tk()
        root.title("Tony"); root.geometry("280x170+40+40")
        root.attributes("-topmost", True); root.configure(bg="#0A0F1E"); root.resizable(False, False)
        state_lbl = tk.Label(root, text="BOOTING", font=("Segoe UI", 13, "bold"), fg="#4f7cff", bg="#0A0F1E")
        state_lbl.pack(pady=(10, 2))
        reply_lbl = tk.Label(root, text="Tony is starting...", font=("Segoe UI", 9), fg="#e6e6e6", bg="#0A0F1E", wraplength=250, justify="left", height=3)
        reply_lbl.pack(padx=10, pady=2)
        bar = tk.Canvas(root, width=250, height=8, bg="#12172B", highlightthickness=0)
        bar.pack(pady=4)
        level_rect = bar.create_rectangle(0, 0, 0, 8, fill="#16A34A", width=0)
        def stop_cmd():
            speech.STOP["now"] = True; speech.stop_speech(); print("\n  [STOP requested from overlay]")
        tk.Button(root, text="STOP", command=stop_cmd, bg="#e5484d", fg="white", font=("Segoe UI", 9, "bold"), relief="flat", width=8, cursor="hand2").pack(pady=(2, 8))
        def pump():
            try:
                while True:
                    kind, val = UI["queue"].get_nowait()
                    if kind == "state":
                        colors = {"LISTENING": "#16A34A", "THINKING": "#f59e0b", "SPEAKING": "#4f7cff", "WAKE": "#a855f7", "BRIEFING": "#a855f7", "IDLE": "#555f70"}
                        state_lbl.config(text=val, fg=colors.get(val, "#4f7cff"))
                    elif kind == "reply": reply_lbl.config(text=val)
                    elif kind == "level": bar.coords(level_rect, 0, 0, int(250 * max(0, min(val, 100)) / 100), 8)
            except queue.Empty: pass
            root.after(120, pump)
        def keep_top():
            root.attributes("-topmost", True); root.after(5000, keep_top)
        root.after(5000, keep_top); pump()
        speech.prepare_voice_cache(("Opened camera.", "Opened webcam.", "Closed camera.", "Closed webcam.", "Opened chrome.", "Closed Chrome cleanly.", "YouTube is open, sir."))
        root.mainloop()
    except Exception as e:
        print(f"[overlay] failed: {str(e)[:80]}")
