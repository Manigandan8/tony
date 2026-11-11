# =====================================================================
# tony_core/memory.py
# Memory store, history, core_reply formatting, fact persistence.
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
                raw_hist = json.load(f)[-12:]
            # Strip any legacy [Done this turn: ...] tags so the LLM never imitates them as plain text!
            HISTORY = [
                {
                    "role": m.get("role", "user"),
                    "content": re.sub(r"\s*\[Done this turn:[^\]]*\]", "", m.get("content") or "").strip()
                }
                for m in raw_hist if (m.get("content") or "").strip()
            ]
            print(f"[memory] restored {len(HISTORY)} messages from your last session")
        except Exception:
            HISTORY = []


_URL_RE = re.compile(r"https?://\S+")
_ABBREV_RE = re.compile(r"\b(mr|mrs|ms|dr|prof|sr|jr|vs|etc|e\.g|i\.e|st|no|fig|inc|ltd)\.", re.I)
_DECIMAL_RE = re.compile(r"(?<=\d)\.(?=\d)")


def _split_sentences_safe(text: str) -> list:
    placeholder_dot = "\x00"
    protected = _DECIMAL_RE.sub(placeholder_dot, text)
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", placeholder_dot), protected)
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace(placeholder_dot, ".").strip() for p in parts if p.strip()]


def core_reply(text, detail=False):
    t = (text or "").strip()
    t = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", t)
    t = _URL_RE.sub("", t)
    t = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*`#]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return "Done."
    max_s, max_c = (6, 750) if detail else (config.REPLY_MAX_SENTENCES, config.REPLY_MAX_CHARS)
    out, seen_sentences = "", set()
    for s_clean in _split_sentences_safe(t):
        norm_s = s_clean.lower().rstrip(".!?")
        if norm_s in seen_sentences:
            continue
        seen_sentences.add(norm_s)
        if len(seen_sentences) > max_s:
            break
        if out and len(out) + len(s_clean) + 1 > max_c:
            break
        out = (out + " " + s_clean).strip()
    if len(out) > max_c:
        out = out[:max_c].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out or "Done."


def wants_detail(msg):
    return bool(re.search(
        r"\b(in detail|detailed|explain|elaborate|step by step|full answer|tell me everything|how does|why does)\b",
        (msg or "").lower()
    ))


def remember(role, content):
    """Stores clean message text in HISTORY without [Done this turn: ...] tags so the LLM never fakes tool calls."""
    content = (content or "").strip()
    if role == "assistant":
        content = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", content).strip()
        content = re.sub(r"\s+", " ", content)[:700]
    if not content:
        return
    with MEMORY_LOCK:
        HISTORY.append({"role": role, "content": content})
        del HISTORY[:-20]
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(HISTORY, f, indent=2)
        except Exception:
            pass


def _load_facts():
    if not os.path.exists(config.FACTS_FILE):
        return []
    try:
        with open(config.FACTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def facts_block():
    facts = _load_facts()
    if not facts:
        return ""
    return "\nUSER FACTS TO REMEMBER:\n" + "\n".join(f"- {f}" for f in facts[-30:])


def remember_fact(fact: str) -> str:
    fact = (fact or "").strip()[:300]
    if not fact:
        return "Tell me the fact to remember."
    try:
        facts = _load_facts()
        if fact not in facts:
            facts.append(fact)
        with open(config.FACTS_FILE, "w", encoding="utf-8") as f:
            json.dump(facts[-100:], f)
        return f"Remembered: '{fact[:60]}'."
    except Exception as e:
        return f"Couldn't save the fact: {str(e)[:60]}"