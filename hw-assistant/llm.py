"""
llm.py — pluggable LLM backends for the hardware assistant.

Author: Gangadhar Korakuppa (ganga2006)

Two real backends, zero third-party dependencies (stdlib urllib only):

  * AnthropicBackend — cloud model via the Anthropic Messages API
      needs:  ANTHROPIC_API_KEY  (optional: HW_ASSISTANT_MODEL)
  * OllamaBackend   — a fully LOCAL model via Ollama's /api/chat
      needs:  Ollama running on localhost (optional: OLLAMA_MODEL,
              default llama3.1 — must be a tool-calling capable model)

Privacy: with Ollama, nothing ever leaves the machine. With Anthropic,
only the conversation and the tool RESULTS you saw printed are sent —
never raw credentials or files; all inspection happens locally either way.

Both backends speak one normalized message format:
  {"role": "user"|"assistant", "content": [block, ...]}
  blocks: {"type":"text","text":...}
          {"type":"tool_use","id":...,"name":...,"input":{...}}
          {"type":"tool_result","tool_use_id":...,"content":...}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class BackendError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise BackendError(f"HTTP {e.code} from {url}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        raise BackendError(f"cannot reach {url}: {e.reason}")


class AnthropicBackend:
    name = "anthropic"

    def __init__(self):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise BackendError("ANTHROPIC_API_KEY not set")
        self.model = os.environ.get("HW_ASSISTANT_MODEL", "claude-sonnet-4-5")

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> list[dict]:
        resp = _post_json(
            "https://api.anthropic.com/v1/messages",
            {"model": self.model, "max_tokens": 1024, "system": system,
             "messages": messages, "tools": tools},
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        return resp.get("content", [])


class OllamaBackend:
    name = "ollama"

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        # Fail fast if Ollama isn't there.
        try:
            urllib.request.urlopen(self.host + "/api/tags", timeout=3).read()
        except Exception as e:
            raise BackendError(f"Ollama not reachable at {self.host}: {e}")

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> list[dict]:
        resp = _post_json(
            self.host + "/api/chat",
            {"model": self.model, "stream": False,
             "messages": [{"role": "system", "content": system}]
                         + [self._to_ollama(m) for m in messages],
             "tools": [{"type": "function",
                        "function": {"name": t["name"],
                                     "description": t["description"],
                                     "parameters": t["input_schema"]}}
                       for t in tools]})
        msg = resp.get("message", {})
        blocks: list[dict] = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            blocks.append({"type": "tool_use",
                           "id": tc.get("id", f"call_{i}"),
                           "name": fn.get("name"),
                           "input": fn.get("arguments") or {}})
        return blocks

    @staticmethod
    def _to_ollama(m: dict) -> dict:
        """Flatten our normalized blocks into Ollama's message shape."""
        if isinstance(m["content"], str):
            return {"role": m["role"], "content": m["content"]}
        texts, tool_results = [], []
        for b in m["content"]:
            if b["type"] == "text":
                texts.append(b["text"])
            elif b["type"] == "tool_result":
                tool_results.append(b["content"] if isinstance(b["content"], str)
                                    else json.dumps(b["content"]))
            elif b["type"] == "tool_use":
                texts.append(f"[called tool {b['name']}]")
        if tool_results:
            return {"role": "tool", "content": "\n".join(tool_results)}
        return {"role": m["role"], "content": "\n".join(texts)}


def pick_backend(preference: str | None = None):
    """Choose a backend: explicit --backend flag, else Anthropic if a key is
    set, else local Ollama, else None (the CLI falls back to offline mode)."""
    order = [preference] if preference else ["anthropic", "ollama"]
    errors = []
    for name in order:
        try:
            if name == "anthropic":
                return AnthropicBackend()
            if name == "ollama":
                return OllamaBackend()
            if name in ("offline", "none"):
                return None
        except BackendError as e:
            errors.append(f"{name}: {e}")
    if preference:
        raise BackendError("; ".join(errors) or f"unknown backend '{preference}'")
    return None
