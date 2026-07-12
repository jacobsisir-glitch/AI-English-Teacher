# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AI English Grammar Teacher — a full-stack application combining:
- **Text chat / Q&A** — streaming AI English grammar tutor (DeepSeek Chat)
- **Micro-lesson (whiteboard) mode** — staged, textbook-driven grammar lessons
- **Voice input** — browser mic → LiveKit WebRTC → Silero VAD → FunASR STT
- **Voice output** — Doubao V3 bidirectional streaming TTS

The teacher persona is a tsundere, sarcastic British-humour English teacher with predefined Live2D action tags.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| LLM | DeepSeek Chat via OpenAI-compatible SDK |
| Database | SQLAlchemy + SQLite (`data/ai_teacher.db`) |
| Voice chat | LiveKit (WebRTC SFU) |
| Speech-to-Text | FunASR WebSocket (2-pass, Paraformer models) |
| VAD | Silero VAD (ONNX runtime) |
| Text-to-Speech | Doubao V3 bidirectional WebSocket TTS |
| TTS fallback | Melo TTS + OpenVoice (legacy, port 8012) |
| Frontend | Vue 3 CDN, Tailwind CSS, ECharts, Axios, marked.js |

## How to run

```bash
conda activate ai_teacher
cd D:\AIEnglish_grammar_teacher
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Frontend: `http://127.0.0.1:8000/frontend/index.html`

One-click startup: `Open_AI_teacher.bat` (starts LiveKit, FunASR, and backend sequentially).

### Voice pipeline requirements

- LiveKit server on `127.0.0.1:7880`
- FunASR WebSocket on `127.0.0.1:10095`
- Doubao TTS uses cloud API — no local TTS server needed

### Verification scripts

```bash
python scripts/verify_doubao_tts_v3.py   # Test Doubao V3 TTS, generates doubao_v3_test.mp3
python scripts/verify_voice_pipeline.py  # Dry-run VAD + audio normalizer
python scripts/check_funasr_scheme.py    # Test FunASR WebSocket connectivity
```

### Run tests

```bash
# The only existing test suite is for hire_solution.py (unrelated, in C:\Users\lenovo)
# No dedicated test suite for this project yet
```

## Configuration

All config lives in `.env` (copy from `.env.example`). Key variables parsed in `config.py`:

- `DEEPSEEK_API_KEY` — LLM API key
- `LIVEKIT_*` — LiveKit server credentials
- `FUNASR_WS_URL` — FunASR WebSocket endpoint (default `wss://127.0.0.1:10095`)
- `DOUBAO_*` — Doubao TTS V3 credentials and settings
- `TTS_PROVIDER` — `doubao` (primary) or `local_melo` (legacy fallback)
- `SILERO_*` — VAD thresholds and timing windows

## Architecture

```
Browser (Vue 3 SPA)
    ├─ SSE ──────────→ FastAPI Backend ──→ DeepSeek Chat API
    ├─ WebSocket ────→ /api/tts/stream ──→ Doubao V3 TTS
    ├─ LiveKit ──────→ LiveKit Room
    │                    └─ voice worker (backend)
    │                         ├─ Silero VAD
    │                         ├─ FunASR WebSocket
    │                         └─ Transcript Publisher → back to LiveKit room
    └─ HTTP ─────────→ SQLite (via SQLAlchemy)
```

## Key source files

| File | Role |
|---|---|
| `main.py` (~2342 lines) | FastAPI app: all endpoints, course tasks, whiteboard system, session state, memory compression |
| `llm_wrapper.py` (~905 lines) | LLM persona, system prompts, tool use (textbook lookup), stream sanitizers |
| `config.py` | All env var loading with typed defaults and validation |
| `tts_client.py` (~280 lines) | TTS provider router with legacy fallback logic |
| `livekit_utils.py` (~144 lines) | LiveKit JWT token generation for participants and workers |
| `frontend/index.html` (~1900 lines) | Complete Vue 3 SPA with whiteboard, voice panel, dashboard |
| `voice/livekit_room_bridge.py` (~904 lines) | Room management, participant lifecycle, audio pipeline orchestration |
| `voice/funasr_client.py` (~736 lines) | FunASR WebSocket client with multi-stage final waiting strategy |
| `voice/vad_controller.py` (~150 lines) | Silero VAD wrapper with speech/silence state machine |
| `voice/audio_buffer.py` (~103 lines) | PCM window buffer, pre-speech ring buffer, audio normalizer |
| `voice/transcript_publisher.py` (~60 lines) | Publish STT results back to LiveKit as text messages |
| `voice/session_state.py` (~79 lines) | Structured JSON logging and utterance telemetry |
| `speech_providers/doubao_tts.py` (~879 lines) | Doubao V3 bidirectional WebSocket TTS + V1 HTTP/WS fallback |
| `speech_providers/doubao_protocol.py` (~201 lines) | ByteDance Volcano Engine binary protocol implementation |
| `database/models.py` | SQLAlchemy models: Student, StudyLog, KnowledgeMastery, ErrorBook, StudentQuestion |
| `tools/textbook_tool.py` | LLM tool: scan textbook index and read chapters |
| `data/textbooks/` | 8 Markdown grammar textbooks with `[STAGE:*]`, `[WB_*]` directives |

## API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /chat_stream` | Streaming text chat (SSE) |
| `POST /class_chat_stream` | Streaming micro-lesson with whiteboard events (SSE) |
| `POST /course/exit` | Reset class session state |
| `GET /api/memory/summary` | Session summary |
| `GET /api/dashboard/data` | Student analytics (errors, radar, questions, mastery) |
| `POST /api/livekit/token` | Issue LiveKit token + ensure voice worker |
| `GET /api/livekit/worker-status` | Voice worker health check |
| `GET /api/tts/voices` | List TTS voices |
| `POST /api/tts/speak` | Full TTS synthesis |
| `WebSocket /api/tts/stream` | Streaming TTS via Doubao V3 |

## Git workflow

- Branch: `feat/voice-streaming`
- Main branch: `main`
- Currently uncommitted: `.vscode/settings.json`, textbook 05 edits, new textbooks 06 and 07

## Current handoff: Two-Channel Commit voice input

Status: implemented and user-tested successfully. Do not reintroduce automatic `partial_fallback` submission in normal chat.

What changed:

- Voice input now uses a Two-Channel Commit model.
- Preview Channel: `stt.partial` is only for live UI preview (`正在识别：...`) and must never call `submitVoiceTranscript()`.
- Commit Channel: only offline/server final transcripts may become `stt.final` and enter `/chat_stream` or `/class_chat_stream`.
- Normal chat blocks `partial_fallback`, `timeout_partial_fallback`, and obvious fragments such as `下你`, `课程`, `her`, `loheris`, `singgoing`.
- No-final cases publish `voice.state = recognition_incomplete`; frontend shows `我听到了一部分，但还没拿到完整识别结果，请再说一遍。` and does not submit to Lumina.
- Micro-lesson pending-question mode allows guarded short-answer fallback only while backend class state is waiting for an answer, with whitelist examples like `Birds`, `主语`, `yes`, `好`.
- `PartialAccumulator` remains for preview/debug/incomplete recognition context, not as a normal-chat commit source.

Key files:

- `voice/utterance_manager.py`: `CommitGate`, `CommitContext`, ack lifecycle helpers.
- `voice/livekit_room_bridge.py`: CommitGate integration before publishing `stt.final`, `recognition_incomplete` state, participant/track processor handling.
- `voice/funasr_client.py`: safer FunASR final routing, explicit `wav_name` handling, longer final wait strategy.
- `voice/transcript_publisher.py`: `stt.final` can include `source`.
- `frontend/index.html`: partial preview only, final-only submit, no-final UI handling, one-submit-per-utterance guard.
- `main.py`: backend class/pending-question state is exposed to voice worker through `VoiceWorkerManager(context_provider=...)`.

Important caveat:

- The frontend `结束本句` button is experimental. It currently uses `setMicrophoneEnabled(false)` then `setMicrophoneEnabled(true)`, which may trigger LiveKit track rebuilds. Prefer a future data topic or HTTP control signal that asks the backend to finish the current utterance without closing the microphone track.

Verification already run:

- `python -m py_compile voice\funasr_client.py voice\livekit_room_bridge.py voice\utterance_manager.py voice\partial_accumulator.py voice\transcript_publisher.py main.py config.py`
- `git diff --check` passes; only Windows LF-to-CRLF warnings appear.
- CommitGate simulation confirms normal chat blocks `下你` / `课程` / `her`, allows offline final Chinese/English long sentences, and allows pending-question short answers `Birds` / `主语` / `yes` / `好`.
- User reports live tests passed for Chinese long sentences, English sentence, no-final prompt, and pending-question short answers.

Current pre-commit state:

- No staged files yet.
- `.env` is not modified/staged.
- New files to include in commit: `voice/partial_accumulator.py`, `voice/utterance_manager.py`.
- Suggested commit message: `fix: enforce final-only voice transcript commit`

## Language

The user and this project are Chinese. Comments, docs, UI strings, prompts are predominantly Chinese or bilingual. English grammar teaching examples are in English.
