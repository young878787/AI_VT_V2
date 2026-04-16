# AI VTuber

An AI-driven VTuber system that connects a large language model to a Live2D character in real time. The AI understands conversation context, expresses emotions through facial parameters, and builds a persistent memory of the user over time.

> This project is actively maintained and under continuous development.

---

## What It Does

- The AI responds to chat input and simultaneously drives the Live2D model's expressions (eyes, eyebrows, mouth, blush, head movement).
- Every reply is paired with a unique expression crafted by the AI itself using tool calls.
- A persistent memory system lets the AI remember user preferences, personality traits, and shared events across sessions.
- When conversation history grows large, the system automatically summarizes and compresses older context to stay within token limits.

---

## Architecture

```
AI_VT_V2/
├── backend/                   # Python FastAPI backend
│   ├── main.py                # WebSocket server, LLM orchestration, memory system
│   ├── requirements.txt       # Python dependencies
│   └── memory/                # Persistent memory (gitignored, auto-created)
│       ├── user_profile.json  # User personality & preference store
│       └── memory.md          # Timestamped event log
│
└── vtuber-web-app/            # React + TypeScript + Vite frontend
    └── src/
        ├── components/        # UI panels and overlays
        │   ├── AIChatPanel    # Chat input and streaming text display
        │   ├── ControlPanel   # Manual expression and parameter controls
        │   ├── HitAreaOverlay # Interactive click zones on the Live2D model
        │   ├── Live2DCanvas   # WebGL canvas that renders the Live2D model
        │   └── ModelParamPanel # Real-time parameter inspection
        ├── live2d/            # Cubism SDK integration layer
        │   ├── LAppModel.ts   # Core model controller (expressions, physics)
        │   ├── LAppDelegate.ts
        │   ├── LAppView.ts
        │   └── MotionController.ts
        ├── services/
        │   └── wsService.ts   # WebSocket client connecting to backend
        └── store/
            └── appStore.ts    # Global state management (Zustand)
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| Frontend | React 19, TypeScript, Vite (rolldown), Zustand |
| Live2D | Cubism SDK for Web 5 |
| Backend | Python, FastAPI, WebSocket |
| LLM | OpenRouter / NVIDIA / Google AI Studio (configurable model) |
| Memory | JSON + Markdown flat files |

---

## How It Works

1. The user types a message in the chat panel.
2. The frontend sends it over WebSocket to the Python backend.
3. The backend assembles a dynamic system prompt incorporating the user's profile and shared memory, then calls the LLM via the selected provider (OpenRouter / NVIDIA / Google AI Studio).
4. The LLM uses structured tool calls to determine expression parameters (`set_ai_behavior`) and optionally update memory (`update_user_profile`, `save_memory_note`).
5. The backend sends expression data and streamed text back to the frontend simultaneously.
6. The frontend applies the expression parameters to the Live2D model with smooth interpolated transitions.

---

## Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- Cubism SDK for Web (place in project root as `CubismSdkForWeb-5-r.5-beta.3/`)
- An API key for one provider ([OpenRouter](https://openrouter.ai), NVIDIA, or Google AI Studio)

### Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=your_key_here
# or Google AI Studio
# AI_PROVIDER=google
# GOOGLE_API_KEY=your_key_here
```

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py
```

The WebSocket server starts at `ws://localhost:${BACKEND_PORT}/ws/chat`.

### Frontend

```bash
cd vtuber-web-app
npm install
npm run dev
```

Open `http://localhost:${FRONTEND_PORT}` in your browser.

---

## Memory System

The AI maintains two persistent files under `backend/memory/` (excluded from version control):

- `user_profile.json` — stores the user's core traits, communication style, interests, and dislikes.
- `memory.md` — an append-only log of significant shared events with timestamps.

When the conversation history approaches the model's token limit (~230,000 tokens), the system automatically summarizes older messages and appends the summary to `memory.md`, keeping the context window manageable.

---

## License

No license has been declared. All rights reserved by the author.
