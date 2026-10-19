# Tony — Advanced Desktop AI Voice Assistant

Tony is a modular, multi-provider Windows desktop AI voice assistant with wake word detection, speech-to-text, natural text-to-speech, computer vision, UIA desktop automation, and a cascading 9-provider LLM brain.

---

## Key Features

- **Voice Pipeline**:
  - **Wake Word**: Continuous low-power listening via `openwakeword`.
  - **Speech-to-Text**: High-accuracy local Whisper STT with WebRTC VAD voice activity detection and mic calibration.
  - **Text-to-Speech**: Natural, low-latency synthesis via `edge-tts` with intelligent local caching and offline fallback (`pyttsx3`).
- **9-Brain Cascade**:
  - Automatic fallback cascade across **Groq**, **Gemini**, **Cohere**, **GitHub Models**, **Mistral**, **Cerebras**, **OpenRouter**, **SambaNova**, and **Z.ai**.
  - Rapid regex fast-path for instant execution of common queries.
- **Vision & Screen OCR**:
  - Real-time screenshot capture and multi-model Vision-Language Model (VLM) chain.
  - Tesseract OCR coordinate mapping and element finder (`ui_click`, `look_at_screen`).
- **40+ Built-In Desktop Tools**:
  - **Filesystem**: File read/write/append, directory management, sandboxed path checks.
  - **Web**: Tavily live search, browser search, webpage fetching, deep research.
  - **UI Automation**: Windows UIA tree inspection, app launch/close, mouse clicks, keyboard input, clipboard management.
  - **System Control**: Volume control, media keys, battery/system status, shutdown/restart confirmation gates, shell execution.
- **Safety & Permissions**:
  - Interactive confirmation gates for destructive actions (file overwrites, system commands, restarts).
  - Central action auditing and structured logging (`tony_actions.log`).
- **Triggers & Privileged Mode**:
  - Double-clap audio detection stream for toggling privileged / secret mode.
- **Remote Telegram Bridge**:
  - Secure remote interaction and proactive notification loop via Telegram bot API.
- **Floating Desktop HUD**:
  - Minimalist Tkinter visual overlay displaying active state (Listening, Thinking, Speaking).

---

## Repository Structure

```
tony/
├── tony.py                 # Thin launcher entrypoint
├── tony_core/              # Core modular package
│   ├── config.py           # Configuration, paths, thresholds & system prompt
│   ├── net.py              # Shared resilient HTTP session
│   ├── memory.py           # Multi-turn conversation and fact memory
│   ├── safety.py           # Safety gate, confirm prompts, audit log
│   ├── speech.py           # Edge-TTS / pyttsx3 speech synthesis
│   ├── ears.py             # Whisper STT, VAD, audio calibration
│   ├── wake.py             # OpenWakeWord background loop
│   ├── vision.py           # Screenshot analysis, OCR, VLM chain
│   ├── brain.py            # Tool calling loop and 9-LLM brain cascade
│   ├── overlay.py          # Tkinter floating status HUD
│   ├── telegram_link.py    # Remote Telegram bridge
│   ├── triggers.py         # Clap-triggered privileged mode listener
│   ├── app.py              # Application lifecycle, boot, console loop
│   ├── providers/          # Modular LLM / VLM API adapters
│   │   ├── cerebras.py
│   │   ├── cohereai.py
│   │   ├── gemini.py
│   │   ├── github_models.py
│   │   ├── groqai.py
│   │   ├── mistralai.py
│   │   ├── openrouter.py
│   │   ├── sambanova.py
│   │   └── zai.py
│   └── tools/              # 40+ system, file, web & UI tools
│       ├── files.py
│       ├── info.py
│       ├── routines.py
│       ├── system.py
│       ├── ui.py
│       └── web.py
├── ARCHITECTURE.md         # Detailed symbol map & dependency DAG
├── requirements.txt        # Python package dependencies
├── routines.json           # Customizable voice routine presets
├── .env.example            # Environment variables template
└── .gitignore              # Git ignore rules for credentials & caches
```

---

## Getting Started

### 1. Prerequisites
- **OS**: Windows 10 / 11
- **Python**: 3.10+ (recommended 3.11)
- Working Microphone & Speakers
- *(Optional)* [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) for screen text OCR detection

### 2. Installation
Clone the repository:
```bash
git clone https://github.com/Manigandan8/tony.git
cd tony
```

Create and activate a virtual environment:
```bash
python -m venv venv
.\venv\Scripts\activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

### 3. Environment Configuration
Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```

Open `.env` and configure your API keys:
- `GROQ_API_KEY`: *(Required)* Primary fast brain.
- Optional multi-provider keys: `GEMINI_API_KEY`, `TAVILY_API_KEY`, `OPENROUTER_API_KEY`, `COHERE_API_KEY`, `MISTRAL_API_KEY`, `CEREBRAS_API_KEY`, `GITHUB_TOKEN`, `ZAI_API_KEY`.
- Optional Telegram bot settings: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

### 4. Running Tony
Run the application from the root directory:
```bash
python tony.py
```

- Speak "Hey Tony" or press `Enter` to speak.
- Type any query directly into the console prompt.
- Type `exit` or `quit` to cleanly shut down.

---

## Documentation
For in-depth details on the module dependencies, shared globals, symbol mapping, and guidelines for adding custom tools or providers, refer to [ARCHITECTURE.md](ARCHITECTURE.md).
