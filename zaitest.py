"""
free_apis_test.py (v3) — quick tester for FREE AI APIs you can add to Tony
Put this file in the SAME folder as your .env, then run:  python free_apis_test.py

What it checks:
  1. GROQ           — auto-detects ALL models on your key; probes vision capability
                      (as of Oct 2026 Groq's free tier is TEXT-ONLY: Llama-4 was retired)
  2. GITHUB MODELS  — text + VISION (gpt-4o-mini / gpt-4.1-mini) [free GitHub PAT, no scopes]
  3. MISTRAL        — text + VISION (pixtral-12b / mistral-small)[free key, needs phone verify]
  4. CEREBRAS       — text only   (gpt-oss-120b / llama3.3-70b)  [free key]
  5. COHERE         — auto-detects models; brain (command-a-03-2025 + command-a-plus-05-2026)
                      + VISION (command-a-vision-07-2025, c4ai-aya-vision-32b) [your existing key]

Add to your .env (only the ones you sign up for):
  GROQ_API_KEY=gsk_...                 # you already have this one (text-only now)
  GITHUB_MODELS_TOKEN=github_pat_...   # github.com -> Settings -> Developer settings -> PAT (no scopes needed)
  MISTRAL_API_KEY=...                  # console.mistral.ai (free tier, phone verify)
  CEREBRAS_API_KEY=csk-...             # cloud.cerebras.ai (free tier)
  COHERE_API_KEY=...                   # you already have this one (has vision!)
"""

import base64
import os
import sys

import requests

TIMEOUT = 45        # model/key listing calls
CHAT_TIMEOUT = 30   # single chat completion (shorter so a hung provider can't stall us)

# 1x1 red PNG used to test vision models
PNG1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG1x1).decode()

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)


def load_env():
    """Tiny .env loader (no python-dotenv needed). OS env vars override."""
    env = {}
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.exists(cand):
            with open(cand, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def chat(url, headers, payload, timeout=CHAT_TIMEOUT):
    """POST a chat completion. Returns (status_code, parsed_body_or_text, err)."""
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return 0, "", "timed out (provider slow/cold — key may still be fine)"
    except Exception as e:
        return 0, "", repr(e)
    try:
        return r.status_code, r.json(), ""
    except Exception:
        return r.status_code, r.text[:300], ""


def reply_text(body):
    """Pull the assistant text out of an OpenAI-style response."""
    try:
        return (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


def first_error(body):
    try:
        if isinstance(body, dict):
            if "error" in body:
                e = body["error"]
                return e.get("message", e)[:160] if isinstance(e, dict) else str(e)[:160]
            return str(body)[:160]
    except Exception:
        pass
    return str(body)[:160]


# ---------------------------------------------------------------- 1. GROQ
def test_groq(key):
    print("\n[1] GROQ  (key you already have)")
    H = {"Authorization": f"Bearer {key}"}
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers=H, timeout=TIMEOUT)
        if r.status_code != 200:
            report("groq key / models list", False, f"HTTP {r.status_code} {r.text[:120]}")
            return
        ids = sorted(m.get("id", "") for m in r.json().get("data", []))
        print(f"  models on this key ({len(ids)}):")
        for i in ids:
            print(f"    - {i}")
        report("groq key valid", True, f"{len(ids)} models visible")
    except Exception as e:
        report("groq key / models list", False, repr(e))
        return

    # Probe whether ANY vision-capable model is enabled (Llama-4 was retired from
    # the free tier in 2026 — expect 404s here unless Groq re-adds vision models).
    vision_candidates = [i for i in ids if ("llama-4" in i) or ("scout" in i) or ("maverick" in i)]
    vision_candidates += ["meta-llama/llama-4-scout-17b-16e-instruct",
                          "meta-llama/llama-4-maverick-17b-128e-instruct"]
    seen, any_vision = set(), False
    for mid in vision_candidates:
        if mid in seen or mid not in ids and "llama-4" not in mid:
            seen.add(mid)
            continue
        seen.add(mid)
        st, body, err = chat("https://api.groq.com/openai/v1/chat/completions", H, {
            "model": mid,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What color is this pixel? Answer in one word."},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ]}],
            "max_tokens": 30,
            "temperature": 0,
        })
        if st == 200 and isinstance(body, dict):
            any_vision = True
            report(f"groq VISION {mid}", True, f"replied: {reply_text(body)[:40]!r}")
    if not any_vision:
        report("groq VISION", False,
               "no vision model on this key (Groq free tier is TEXT-ONLY since the Llama-4 retirement)")


# ------------------------------------------------------- 2. GITHUB MODELS
def test_github(key):
    print("\n[2] GITHUB MODELS  (free with any GitHub PAT, no scopes needed)")
    H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    URL = "https://models.github.ai/inference/chat/completions"

    for mid in ("openai/gpt-4.1-mini", "openai/gpt-4o-mini"):
        st, body, err = chat(URL, H, {
            "model": mid,
            "messages": [{"role": "user", "content": "Say exactly: OK"}],
            "max_tokens": 10,
        })
        if st == 200 and isinstance(body, dict):
            report(f"github brain {mid}", True, f"replied: {reply_text(body)[:40]!r}")
        else:
            report(f"github brain {mid}", False, f"HTTP {st} {err or first_error(body)}")

    st, body, err = chat(URL, H, {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What color is this pixel? Answer in one word."},
            {"type": "image_url", "image_url": {"url": DATA_URL}},
        ]}],
        "max_tokens": 30,
    })
    if st == 200 and isinstance(body, dict):
        report("github VISION gpt-4o-mini", True, f"replied: {reply_text(body)[:40]!r}")
    else:
        report("github VISION gpt-4o-mini", False, f"HTTP {st} {err or first_error(body)}")


# ---------------------------------------------------------- 3. MISTRAL
def test_mistral(key):
    print("\n[3] MISTRAL  (free tier, phone verification required)")
    H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    URL = "https://api.mistral.ai/v1/chat/completions"

    st, body, err = chat(URL, H, {
        "model": "mistral-small-latest",
        "messages": [{"role": "user", "content": "Say exactly: OK"}],
        "max_tokens": 10,
    })
    if st == 200 and isinstance(body, dict):
        report("mistral brain mistral-small-latest", True, f"replied: {reply_text(body)[:40]!r}")
    else:
        report("mistral brain mistral-small-latest", False, f"HTTP {st} {err or first_error(body)}")

    st, body, err = chat(URL, H, {
        "model": "pixtral-12b-2409",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What color is this pixel? Answer in one word."},
            {"type": "image_url", "image_url": {"url": DATA_URL}},
        ]}],
        "max_tokens": 30,
    })
    if st == 200 and isinstance(body, dict):
        report("mistral VISION pixtral-12b-2409", True, f"replied: {reply_text(body)[:40]!r}")
    else:
        report("mistral VISION pixtral-12b-2409", False, f"HTTP {st} {err or first_error(body)}")


# ---------------------------------------------------------- 4. CEREBRAS
def test_cerebras(key):
    print("\n[4] CEREBRAS  (text-only, ~1M tokens/day free)")
    H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    URL = "https://api.cerebras.ai/v1/chat/completions"

    candidates = ["gpt-oss-120b", "llama3.3-70b", "llama-3.3-70b", "qwen-3-32b"]
    try:
        r = requests.get("https://api.cerebras.ai/v1/models", headers=H, timeout=TIMEOUT)
        if r.status_code == 200:
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            print(f"  models on this key: {ids}")
            candidates = [c for c in candidates if c in ids] or ids[:2]
    except Exception as e:
        print(f"  (models list failed: {e} -- will try known IDs)")

    ok_any = False
    for mid in candidates[:2]:
        st, body, err = chat(URL, H, {
            "model": mid,
            "messages": [{"role": "user", "content": "Say exactly: OK"}],
            "max_tokens": 10,
        })
        if st == 200 and isinstance(body, dict):
            report(f"cerebras brain {mid}", True, f"replied: {reply_text(body)[:40]!r}")
            ok_any = True
        else:
            report(f"cerebras brain {mid}", False, f"HTTP {st} {err or first_error(body)}")
    if not ok_any:
        print("  (cerebras is text-only, so it can only serve as an extra BRAIN)")


# ------------------------------------------------------------ 5. COHERE
def _cohere_model_ids(key):
    """Try to list models from the OpenAI-compat endpoint, then the native one."""
    H = {"Authorization": f"Bearer {key}"}
    for url in ("https://api.cohere.ai/compatibility/v1/models",
                "https://api.cohere.com/v1/models"):
        try:
            r = requests.get(url, headers=H, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                items = data.get("models", data.get("data", []))
                ids = []
                for m in items:
                    if isinstance(m, str):
                        ids.append(m)
                    else:
                        ids.append(m.get("name") or m.get("id") or "")
                ids = [i for i in ids if i]
                if ids:
                    return url.split("/v1")[0] + "/v1", sorted(set(ids))
        except Exception:
            continue
    return "https://api.cohere.ai/compatibility/v1", []


def test_cohere(key):
    print("\n[5] COHERE  (your existing free trial key)")
    base, ids = _cohere_model_ids(key)
    if ids:
        print(f"  models on this key ({len(ids)}): {ids}")
    else:
        print("  (could not list models -- will try known IDs)")

    H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    URL = f"{base}/chat/completions"

    # ---- brain: tony.py's current model first, then the newer upgrade
    brains = ["command-a-03-2025", "command-a-plus-05-2026"]
    if ids:
        brains = [b for b in brains if b in ids] or [next(
            (i for i in ids if i.startswith("command-a-")), "command-a-03-2025")]
    for b in brains:
        st, body, err = chat(URL, H, {
            "model": b,
            "messages": [{"role": "user", "content": "Say exactly: OK"}],
            "max_tokens": 10,
        })
        if st == 200 and isinstance(body, dict):
            report(f"cohere brain {b}", True, f"replied: {reply_text(body)[:40]!r}")
        else:
            report(f"cohere brain {b}", False, f"HTTP {st} {err or first_error(body)}")

    # ---- vision: flagship FIRST (aya-vision-32b hung 45s on the compat endpoint once;
    #      if it times out we just move on — the key is still fine)
    visions = ["command-a-vision-07-2025", "c4ai-aya-vision-32b"]
    if ids:
        known = [v for v in visions if v in ids]
        if known:
            visions = known
    any_ok = False
    for v in visions:
        st, body, err = chat(URL, H, {
            "model": v,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What color is this pixel? Answer in one word."},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ]}],
            "max_tokens": 30,
        })
        if st == 200 and isinstance(body, dict):
            any_ok = True
            report(f"cohere VISION {v}", True, f"replied: {reply_text(body)[:40]!r}")
        else:
            report(f"cohere VISION {v}", False, f"HTTP {st} {err or first_error(body)}")
    if not any_ok:
        print("  (if vision timed out: Cohere trial can be slow on first call —"
              " tony.py will retry it at runtime with a short timeout and skip on hang)")


# --------------------------------------------------------------- SUMMARY
def main():
    env = load_env()
    print("=" * 64)
    print(" FREE API TESTER v3  --  python free_apis_test.py")
    print("=" * 64)

    tests = [
        ("GROQ",     ("GROQ_API_KEY",),           test_groq),
        ("GITHUB",   ("GITHUB_MODELS_TOKEN", "GITHUB_TOKEN"), test_github),
        ("MISTRAL",  ("MISTRAL_API_KEY",),        test_mistral),
        ("CEREBRAS", ("CEREBRAS_API_KEY",),       test_cerebras),
        ("COHERE",   ("COHERE_API_KEY",),         test_cohere),
    ]

    for label, vars_, fn in tests:
        key = next((env[v] for v in vars_ if env.get(v)), None)
        if not key:
            print(f"\n[{label}]  no {' / '.join(vars_)} in .env -- SKIPPED")
            continue
        try:
            fn(key)
        except Exception as e:
            report(f"{label} unexpected error", False, repr(e))

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f" SUMMARY: {passed}/{len(RESULTS)} checks passed")
    for name, ok, detail in RESULTS:
        print(f"   {'OK  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 64)
    print(" Tony's new vision order once wired: GitHub gpt-4o-mini -> Z.ai ->")
    print(" Mistral pixtral -> Gemini -> Cohere command-a-vision -> OpenRouter.")
    print(" Send me the tester output + the REST of tony.py (from 'def open_folder'")
    print(" to the end) and I will return the complete updated file.")


if __name__ == "__main__":
    main()