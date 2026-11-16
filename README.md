# Asama.ai — AI Engineering Assignment

**Author:** Gangadhar Korakuppa · [ganga2006](https://github.com/ganga2006)

Two working systems, Python 3 stdlib only — zero dependencies, one-command demos.

| Project | Problem | Run |
|---|---|---|
| [`agent-network/`](agent-network/) | A social network for AI agents — 3-stage relevance routing, signed webhooks, feedback loop | `python3 demo.py` |
| [`hw-assistant/`](hw-assistant/) | Local Linux hardware Q&A assistant — safe allow-listed tool-calling, dynamic registry | `python3 main.py` |

Each folder's README covers architecture, design decisions, and production mapping.

LLM backends (both projects): Anthropic API (`ANTHROPIC_API_KEY`), fully-local Ollama, or offline mode with no model at all.
