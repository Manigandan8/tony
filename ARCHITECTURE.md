# Tony Architecture & Symbol Map

## 1. Module Map & Responsibilities
- `tony.py`: Thin entrypoint launcher (~5 lines) invoking `tony_core.app.main()`.
- `tony_core/config.py`: Root paths (`HERE`, `TONY_HOME`), API keys, model lists, thresholds, timeouts, and `SYSTEM` prompt.
- `tony_core/net.py`: Shared `requests.Session` (`HTTP`) with custom User-Agent.
- `tony_core/providers/`: Modular LLM/VLM providers (`gemini`, `groqai`, `zai`, `cohereai`, `openrouter`, `github_models`, `mistralai`, `cerebras`, `sambanova`).
- `tony_core/vision.py`: Screenshot capture, OCR (`_match_ocr`, `_ocr_find_xy`), coordinate finder (`_find_element_xy`), and multi-provider VLM chain (`_vision_describe`).
- `tony_core/memory.py`: Persistent conversation store (`HISTORY`, `MEMORY_LOCK`), `core_reply`, and facts storage (`remember_fact`).
- `tony_core/safety.py`: Command and filesystem verification (`_write_gate`, `_path_verdict`, `confirm`, `log_action`), turn failure tracker (`TURN`), and busy lock (`TONY_BUSY`).
- `tony_core/tools/`: 40+ modular tools split into `files.py`, `web.py`, `ui.py`, `system.py`, `info.py`, `routines.py`, and `__init__.py` (`TOOLS` dispatch registry and `_wrap`).
- `tony_core/brain.py`: `GROQ_TOOLS` schemas, `_tool_loop`, `_run_brain_with_nudge`, 9-brain fallback cascade (`ask_tony`), and regex `fast_path`.
- `tony_core/speech.py`: Edge-TTS audio playback (`speak`, `say_async`), audio caching, and offline fallback.
- `tony_core/ears.py`: Speech recognition (`listen`), Whisper STT, WebRTC VAD, microphone calibration, and device management.
- `tony_core/wake.py`: OpenWakeWord engine (`_get_wake_model`) and wake loop daemon (`_wake_loop`).
- `tony_core/overlay.py`: Tkinter floating HUD (`_overlay_app`) and queue-based UI state dispatchers.
- `tony_core/telegram_link.py`: Remote Telegram polling loop (`_telegram_poll`).
- `tony_core/triggers.py`: Clap-activated privileged mode audio stream, detector, and `get_secret` tool.
- `tony_core/app.py`: Clean boot sequence, thread launcher, and interactive console loop (`main`).

## 2. Import Direction (Strict DAG, No Cycles)
```
config -> net -> providers -> (safety, memory, vision, tools) -> brain -> (speech, ears, wake, overlay, telegram_link, triggers) -> app
```

## 3. Shared Mutable Globals Registry
- `groqai.py`: `GROQ_VISION_MODEL`, `AVAILABLE_MODELS`, `BRAIN_MODEL`
- `zai.py`: `ZAI_BRAIN_MODELS`, `ZAI_VISION_MODELS`, `ZAI_BRAIN_MODEL`, `_ZAI_EP`, `_ZAI_VISION_WAIT`
- `cohereai.py`: `COHERE_BRAIN_MODELS`, `COHERE_VISION_MODELS`
- `github_models.py`: `GITHUB_BRAIN_MODELS`, `GITHUB_VISION_MODELS`
- `mistralai.py`: `MISTRAL_BRAIN_MODELS`, `MISTRAL_VISION_MODELS`
- `cerebras.py`: `CEREBRAS_BRAIN_MODELS`
- `openrouter.py`: `OR_VISION_MODELS`, `OR_BRAIN_MODELS`
- `gemini.py`: `GEMINI_VISION_MODELS`
- `memory.py`: `HISTORY`, `MEMORY_LOCK`
- `safety.py`: `CTX`, `TURN`, `APPROVED`, `TONY_BUSY`
- `speech.py`: `SPEAKING`, `SPEAK_LOCK`, `STOP`, `_CANCEL`, `_SPEECH_THREAD`, `_offline_engine`
- `tools/ui.py`: `STATE` (tracks live Chrome state and profile)
- `ears.py`: `LISTENING`, `MIC_DEVICE_INDEX`, `_MIC_FAIL`, `_ROOM`, `_VAD`, `STT_MODEL`
- `wake.py`: `WAKE`, `_WAKE_MODEL`
- `overlay.py`: `UI`
- `triggers.py`: `_STATE` (privileged mode status, idle timer)

## 4. How to Extend
- **Add a Provider**: Add `tony_core/providers/<name>.py`, add constants to `config.py`, chain in `tony_core/brain.py` (`ask_tony` / `_vision_describe`), and call discovery in `tony_core/app.py:boot()`.
- **Add a Tool**: Implement function in relevant `tony_core/tools/<category>.py`, register in `tony_core/tools/__init__.py:TOOLS`, and append JSON schema to `tony_core/brain.py:GROQ_TOOLS`.
- **Add a Trigger Module**: Implement standalone audio/signal listener, initialize in `tony_core/app.py:main()` under `# --- TRIGGER THREADS ---`, and bind lifecycle hooks (`start`, `stop`, `touch`).

## 5. Symbol Map Summary
- **Original functions**: 153 | **Mapped**: 153 | **Missing**: 0 | **Duplicates**: 0
- **Original variables**: 125 | **Mapped**: 125 | **Missing**: 0 | **Duplicates**: 0
| Module | Primary Mapped Symbols |
|---|---|
| `config.py` | `_get_env`, all API keys (`GROQ_API_KEY`, etc.), base URLs, file paths (`HERE`, `TONY_HOME`), `APPS`, `EXES`, `SYSTEM` |
| `net.py` | `HTTP` |
| `providers/` | `_groq_model_set`, `pick_groq_model`, `_zai_probe`, `_zai_headers`, `_or_models`, `pick_openrouter_vision` |
| `safety.py` | `_remote`, `log_action`, `confirm`, `_normpath`, `_path_verdict`, `_resolve_path`, `_write_gate`, `_cmd_needs_confirm`, `TONY_BUSY` |
| `memory.py` | `core_reply`, `wants_detail`, `remember`, `_load_facts`, `facts_block`, `remember_fact`, `HISTORY` |
| `vision.py` | `_screenshot_image`, `_vision_shot`, `_setup_tesseract`, `_active_region`, `_match_ocr`, `_ocr_find_xy`, `_vision_describe`, `_screenshot_text`, `_find_element_xy` |
| `tools/` | `calculator_math`, `_eval_ast`, file ops (`read_file`, `write_file`, etc.), web ops (`web_search`, `deep_research`, etc.), UI ops (`click_screen`, `open_app`, etc.), system ops (`run_command`, `set_volume`), `TOOLS`, `run_tool`, `_wrap` |
| `brain.py` | `system_prompt`, `GROQ_TOOLS`, `_tool_loop`, `_run_brain_with_nudge`, `brain_*` (all 9 LLM funcs), `ask_tony`, `fast_path` |
| `speech.py` | `speak`, `say_async`, `stop_speech`, `wait_speech`, `ack_async`, `prepare_voice_cache`, `_speak_edge`, `_speak_offline` |
| `ears.py` | `listen`, `_listen_inner`, `_get_vad`, `calibrate_mic`, `mic_doctor`, `mic_list`, `_set_mic`, `_resolve_mic`, `_fix_mishears` |
| `wake.py` | `_get_wake_model`, `_listen_for_wake`, `_wake_loop`, `WAKE` |
| `overlay.py` | `ui_state`, `ui_reply`, `ui_level`, `_overlay_app`, `UI` |
| `telegram_link.py` | `_telegram_poll` |
| `triggers.py` | `init`, `start`, `stop`, `is_privileged`, `touch`, `engage`, `stand_down`, `get_secret` |
| `app.py` | `boot`, `main`, `_briefing_loop` |
