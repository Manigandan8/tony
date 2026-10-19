# =====================================================================
# tony_core/memory.py
# Memory store, history, core_reply formatting, fact persistence.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - MEMORY_LOCK: threading.Lock protecting HISTORY and memory file writes
# - HISTORY: list of conversation messages (restored from tony_memory.json)
# =====================================================================

import os
import re
import json
import threading
from tony_core import config

MEMORY_FILE = config.MEMORY_FILE
MEMORY_LOCK = threading.Lock()
HISTORY = []

def init_memory():
    global HISTORY
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                HISTORY = json.load(f)[-12:]
            print(f"[memory] restored {len(HISTORY)} messages from your last session")
        except Exception:
            HISTORY = []

_URL_RE = re.compile(r"https?://\S+")

def core_reply(text, detail=False):
    t = (text or "").strip()
    t = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", t)
    t = _URL_RE.sub("", t)
    t = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*`#]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t: return "Done."
    max_s, max_c = (6, 700) if detail else (config.REPLY_MAX_SENTENCES, config.REPLY_MAX_CHARS)
    out, seen_sentences = "", set()
    for s in re.split(r"(?<=[.!?])\s+", t):
        s_clean = s.strip()
        if not s_clean: continue
        norm_s = s_clean.lower().rstrip(".!?")
        if norm_s in seen_sentences: continue
        seen_sentences.add(norm_s)
        if len(seen_sentences) > max_s: break
        if out and len(out) + len(s_clean) + 1 > max_c: break
        out = (out + " " + s_clean).strip()
    if len(out) > max_c:
        out = out[:max_c].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out or "Done."

def wants_detail(msg):
    return bool(re.search(r"\b(in detail|detailed|explain|elaborate|step by step|full answer|tell me everything)\b",
                          (msg or "").lower()))

def remember(role, content):
    content = content or ""
    if role == "assistant":
        m = re.search(r"\s*\[Done this turn:[^\]]*\]", content)
        note = m.group(0) if m else ""
        body = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", content)
        content = core_reply(body, detail=True) + note
    with MEMORY_LOCK:
        HISTORY.append({"role": role, "content": content})
        del HISTORY[:-20]
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(HISTORY, f, indent=2)
        except Exception:
            pass

def _load_facts():
    if not os.path.exists(config.FACTS_FILE): return []
    try:
        with open(config.FACTS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return []

def facts_block():
    facts = _load_facts()
    if not facts: return ""
    return "\nUSER FACTS TO REMEMBER:\n" + "\n".join(f"- {f}" for f in facts[-30:])

def remember_fact(fact: str) -> str:
    fact = (fact or "").strip()[:300]
    if not fact: return "Tell me the fact to remember."
    try:
        facts = _load_facts(); facts.append(fact)
        with open(config.FACTS_FILE, "w", encoding="utf-8") as f: json.dump(facts[-100:], f)
        return f"Remembered: '{fact[:60]}'."
    except Exception as e: return f"Couldn't save the fact: {str(e)[:60]}"
