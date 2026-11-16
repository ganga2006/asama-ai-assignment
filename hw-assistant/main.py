#!/usr/bin/env python3
"""
main.py — CLI chat for the Linux hardware Q&A assistant.

Author: Gangadhar Korakuppa (ganga2006)

    python3 main.py                  # auto-pick backend (Anthropic -> Ollama -> offline)
    python3 main.py --backend ollama # force the fully-local model
    python3 main.py --backend offline
    echo "how much ram do I have?" | python3 main.py   # one-shot / scriptable

Design in one paragraph: the LLM never guesses hardware facts. It is given a
registry of read-only, allow-listed inspection tools (hwtools.py); it decides
which to call, the tools return STRUCTURED data parsed from lscpu/lsblk/
/proc//sys/dmidecode, and the model must answer only from those results —
or say it doesn't know. Conversation history is kept, so follow-ups like
"and what about the GPU?" work. With no LLM available at all, an offline
keyword router still answers common questions from the same tools, so the
tool layer is testable and demoable anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys

import hwtools
import llm

SYSTEM_PROMPT = """\
You are a hardware assistant running locally on the user's Linux machine.
You answer questions about THIS machine's hardware in plain language.

Rules:
- Never answer a hardware question from memory or general knowledge about
  product lines. Always call the inspection tools and answer ONLY from the
  data they return.
- If the tools cannot provide a fact (missing command, needs root, not
  exposed on this machine), say exactly that and, where relevant, how the
  user could get it (e.g. "run me with sudo for memory slot details").
  Do not guess or fill gaps with typical values.
- Be concise and concrete: numbers with units, model names as reported.
- For comparative or advice questions ("can I add more RAM?"), compute from
  the gathered data (slots free, max capacity) and show your arithmetic.
- If a question is not about this machine's hardware/OS, say that's outside
  what you can inspect.
"""

MAX_TOOL_ROUNDS = 6

# Session cache of gathered hardware JSON: hardware doesn't change mid-chat,
# so follow-ups ("and what about the GPU?") reuse already-gathered data
# instead of re-running commands — and repeated questions cost nothing.
_hw_cache: dict[str, dict] = {}


def gather(tool_name: str) -> dict:
    if tool_name in _hw_cache:
        print(f"  [cached: {tool_name}]", file=sys.stderr)
        return _hw_cache[tool_name]
    print(f"  [inspecting: {tool_name}]", file=sys.stderr)
    _hw_cache[tool_name] = hwtools.call_tool(tool_name)
    return _hw_cache[tool_name]


def agent_turn(backend, history: list[dict], user_text: str) -> str:
    """One user turn: let the model call tools until it produces an answer."""
    history.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
    tools = hwtools.tool_schemas()

    for _ in range(MAX_TOOL_ROUNDS):
        blocks = backend.chat(SYSTEM_PROMPT, history, tools)
        if not blocks:
            return "(the model returned an empty response)"
        history.append({"role": "assistant", "content": blocks})

        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not tool_uses:
            return "\n".join(b["text"] for b in blocks if b.get("type") == "text").strip()

        results = []
        for tu in tool_uses:
            data = gather(tu["name"])
            results.append({"type": "tool_result", "tool_use_id": tu["id"],
                            "content": json.dumps(data)})
        history.append({"role": "user", "content": results})

    return ("I stopped after several tool rounds without converging — "
            "try asking a more specific question.")


# ------------------------------------------------- offline fallback mode --

OFFLINE_ROUTES = [
    (("ram", "memory", "swap"), ["get_memory_info"]),
    (("cpu", "processor", "core"), ["get_cpu_info"]),
    (("disk", "ssd", "nvme", "storage", "drive", "partition"), ["get_storage_info"]),
    (("gpu", "graphics", "video"), ["get_gpu_info"]),
    (("battery", "charge"), ["get_battery_info"]),
    (("wifi", "network", "ethernet", "interface"), ["get_network_info"]),
    (("usb",), ["get_usb_devices"]),
    (("pci",), ["get_pci_devices"]),
    (("os", "distro", "kernel", "machine", "model", "laptop", "vendor", "system"),
     ["get_system_info"]),
]


def offline_turn(user_text: str) -> str:
    """No-LLM mode: keyword-route to tools and print their structured data.
    Clearly labelled — it proves the tool layer works without any model."""
    q = user_text.lower()
    names = [n for keys, tools_ in OFFLINE_ROUTES if any(k in q for k in keys)
             for n in tools_]
    if not names:
        names = ["get_system_info"]
    seen, picked = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            picked.append(n)
    parts = []
    for n in picked:
        parts.append(f"## {n}\n" + json.dumps(gather(n), indent=2))
    return ("(offline mode — no LLM configured, showing structured tool data)\n\n"
            + "\n\n".join(parts))


# ----------------------------------------------------------------- main --

def main() -> int:
    ap = argparse.ArgumentParser(description="Linux hardware Q&A assistant")
    ap.add_argument("--backend", choices=["anthropic", "ollama", "offline"],
                    help="force a backend (default: auto-detect)")
    args = ap.parse_args()

    try:
        backend = llm.pick_backend(args.backend)
    except llm.BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    mode = backend.name if backend else "offline"
    available, excluded = hwtools.discover_capabilities()
    print(f"Hardware assistant ready (backend: {mode}; "
          f"{len(available)} tools registered"
          + (f"; excluded: {', '.join(excluded)}" if excluded else "")
          + "). Ask about this machine — Ctrl-D or 'quit' to exit.",
          file=sys.stderr)

    history: list[dict] = []
    interactive = sys.stdin.isatty()
    while True:
        try:
            if interactive:
                user_text = input("\nyou> ").strip()
            else:
                user_text = sys.stdin.readline()
                if not user_text:
                    break
                user_text = user_text.strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_text:
            continue
        if user_text.lower() in ("quit", "exit"):
            break

        try:
            answer = (agent_turn(backend, history, user_text) if backend
                      else offline_turn(user_text))
        except llm.BackendError as e:
            print(f"\n[backend error: {e}]", file=sys.stderr)
            print("Falling back to offline mode for this question:", file=sys.stderr)
            answer = offline_turn(user_text)
        print(f"\nassistant> {answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
