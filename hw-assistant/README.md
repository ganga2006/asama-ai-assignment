# Hardware Q&A Assistant for a Linux Laptop

**Author:** Gangadhar Korakuppa · GitHub: [ganga2006](https://github.com/ganga2006)

A chatbot that runs locally on a Linux machine and answers plain-language
questions about **that machine's** hardware — *"How much RAM do I have?"*,
*"Is my SSD NVMe?"*, *"Can I add more RAM, and how much?"* — by calling
real system-inspection tools instead of guessing.

Zero third-party dependencies. Python 3.10+, stdlib only.

## Quick start

```bash
# 1. Fully local (private) — with Ollama running and a tool-capable model:
python3 main.py --backend ollama          # uses llama3.1 by default

# 2. With the Anthropic API:
export ANTHROPIC_API_KEY=sk-ant-...
python3 main.py

# 3. No LLM at all — offline mode proves the tool layer works anywhere:
python3 main.py --backend offline
echo "is my ssd nvme?" | python3 main.py --backend offline   # scriptable
```

Example session:

```
you> how much ram do I have?
  [inspecting: get_memory_info]
assistant> You have 16 GB of RAM (15.4 GiB usable), with 11.2 GiB currently
available. Slot details need root — run me with sudo to see whether you can
add more.

you> and what about the GPU?          <- follow-ups keep context
  [inspecting: get_gpu_info]
assistant> ...
```

## How it works

```
 user question
      │
      ▼
 ┌──────────┐   tool_use (names only,      ┌─────────────────────────┐
 │   LLM    │ ───────────────────────────► │ hwtools.py registry     │
 │ (API or  │                              │  fixed argv, shell=False│
 │  Ollama) │ ◄─────────────────────────── │  lscpu lsblk /proc /sys │
 └──────────┘   structured JSON results    │  dmidecode (if root)    │
      │                                    └─────────────────────────┘
      ▼
 plain-language answer, grounded ONLY in the gathered data
```

1. **Ask** — the CLI (`main.py`) keeps a running conversation history, so
   follow-ups like *"and what about the GPU?"* resolve naturally.
2. **Gather** — the LLM is handed a registry of 9 inspection tools
   (`tool_schemas()`), decides which to call, and `hwtools.py` executes
   them and **parses raw output into structured JSON** (e.g. `lsblk -J`
   → disks with `is_nvme`, `kind: SSD/HDD`, sizes in GiB).
3. **Answer** — the system prompt forbids answering from memory: the model
   must use tool results or say "I don't know", and must show arithmetic
   for advice questions (free slots × max stick size, etc.).

## Design decisions (the "things to think about")

**Safety.** The LLM never composes shell commands. It can only name a tool
from a fixed registry; each tool runs a **hard-coded argv list** with
`shell=False` and a timeout. User text is never interpolated into any
command, so prompt injection can at worst call a different *read-only*
inspection tool. Everything is read-only by construction (inspection
binaries, `/proc`, `/sys`); an unknown tool name is refused.

**Permissions.** `dmidecode` (RAM slot layout, max capacity) needs root.
Running the whole agent as root would be a serious risk, so the assistant
climbs a privilege ladder instead: direct (already root) → a **scoped
sudoers rule** tried non-interactively (`sudo -n`, never prompts) → an
honest structured "unavailable" note telling the user their options.
Non-root fallbacks are used where they exist (`/sys/class/dmi` for machine
model, `/proc/meminfo` for totals). To enable the deep DMI data without
running the agent as root, install a rule scoped to exactly one binary:

```
# /etc/sudoers.d/hw-assistant
yourusername ALL=(root) NOPASSWD: /usr/sbin/dmidecode
```

(The other production option is a small root helper daemon behind a Unix
socket exposing fixed endpoints like `GetDmiInfo`; the sudoers route is the
right weight for a CLI tool.)

**Accuracy.** Hallucination is handled at three levels: (a) the system
prompt bans answering hardware facts from general knowledge, (b) every tool
result carries its `source` (`lscpu`, `/proc/meminfo`, ...) so answers are
traceable, and (c) missing data comes back as an explicit
`unavailable`/`error` field the model is instructed to surface as "I don't
know / here's how to get it" rather than fill with typical values.

**Portability.** Two layers. At startup, **capability discovery**
(`discover_capabilities()`) builds a *dynamic tool registry*: tools that
cannot produce any data on this machine (e.g. `lsusb` absent) are simply
not offered to the LLM, so it never wastes a round trip on a guaranteed
failure. Within each tool, missing commands degrade to sysfs/procfs
fallbacks (`lscpu`→`/proc/cpuinfo`, `lsblk`→`/sys/block`,
`lspci`→`/sys/class/drm`). Tested in a minimal Ubuntu container with no
`lspci`/`lsusb`/`dmidecode` present — the assistant still answers what it
can and reports what it can't.

**Follow-ups & efficiency.** Conversation history plus a **session cache
of gathered hardware JSON**: hardware doesn't change mid-chat, so "and
what about the GPU?" after a RAM question reuses anything already
gathered (`[cached: ...]` in the trace) and only inspects what's new.

**Privacy.** All inspection happens locally in both modes. With the
**Ollama backend, nothing ever leaves the machine**. With the Anthropic
backend, only the conversation and tool results are sent. Offline mode
sends nothing anywhere. No logs are written.

## Files

| File | Role |
|---|---|
| `hwtools.py` | Allow-listed read-only tools + parsers + registry |
| `llm.py` | Anthropic / Ollama backends behind one interface (stdlib HTTP) |
| `main.py` | CLI chat, agent loop, offline fallback router |

## Testing without an API key

- `python3 main.py --backend offline` exercises every tool and parser.
- The agent loop (tool_use → execute → tool_result → answer, unknown-tool
  refusal, history growth) is verified with a mock backend — see the test
  snippet in the project notes; it runs without network access.
