"""
hwtools.py — allow-listed, read-only hardware inspection tools.

Author: Gangadhar Korakuppa (ganga2006)

Safety model:
  * Every tool runs a FIXED argv list (shell=False). User text is never
    interpolated into a command, so there is no injection surface.
  * Only commands in ALLOWED_COMMANDS can ever be executed.
  * Everything is read-only: we only run inspection binaries and read
    /proc and /sys. Nothing writes, mounts, or configures.
  * Timeouts on every subprocess so a hung tool can't hang the chat.

Portability model:
  * Every tool degrades gracefully: missing binary, missing sysfs path,
    or insufficient permissions produce a structured note instead of a
    crash, so the LLM can tell the user honestly what it couldn't see.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess

# The ONLY external binaries this program may ever execute, each with the
# exact argv it is run with. Read-only inspection commands.
ALLOWED_COMMANDS = {
    "lscpu": ["lscpu"],
    "lsblk": ["lsblk", "-J", "-b", "-o",
              "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,MOUNTPOINT,FSTYPE"],
    "lspci": ["lspci"],
    "lsusb": ["lsusb"],
    "dmidecode_memory": ["dmidecode", "-t", "memory"],
    # Scoped privilege escalation: works ONLY if the admin installed a
    # sudoers rule for exactly this binary (see README). -n = never prompt.
    "dmidecode_memory_sudo": ["sudo", "-n", "dmidecode", "-t", "memory"],
}


def _run(key: str, timeout: int = 10):
    """Run one allow-listed command. Returns (ok, output, note)."""
    argv = ALLOWED_COMMANDS[key]          # KeyError = programming error, good.
    binary = argv[0]
    if shutil.which(binary) is None:
        return False, "", f"`{binary}` is not installed on this system"
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return False, "", f"`{binary}` timed out after {timeout}s"
    except OSError as e:
        return False, "", f"`{binary}` failed to run: {e}"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return False, p.stdout, f"`{binary}` exited {p.returncode}: {err[0] if err else 'unknown error'}"
    return True, p.stdout, ""


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except (OSError, PermissionError):
        return None


def _kv_lines(text: str) -> dict:
    """Parse 'Key: value' lines (lscpu, /proc/meminfo style)."""
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _gib(kb_or_bytes: float, unit: str = "bytes") -> float:
    div = 1024 ** 3 if unit == "bytes" else 1024 ** 2
    return round(kb_or_bytes / div, 1)


# ---------------------------------------------------------------- tools --

def get_cpu_info() -> dict:
    """CPU model, cores/threads, architecture, frequency, virtualization."""
    ok, out, note = _run("lscpu")
    if ok:
        kv = _kv_lines(out)
        return {"source": "lscpu", "cpu": {
            "model": kv.get("Model name"),
            "architecture": kv.get("Architecture"),
            "cpus_logical": kv.get("CPU(s)"),
            "cores_per_socket": kv.get("Core(s) per socket"),
            "threads_per_core": kv.get("Thread(s) per core"),
            "sockets": kv.get("Socket(s)"),
            "max_mhz": kv.get("CPU max MHz"),
            "min_mhz": kv.get("CPU min MHz"),
            "virtualization": kv.get("Virtualization"),
            "l3_cache": kv.get("L3 cache"),
        }}
    # Fallback: /proc/cpuinfo
    raw = _read("/proc/cpuinfo")
    if raw:
        models = re.findall(r"model name\s*:\s*(.+)", raw)
        return {"source": "/proc/cpuinfo (lscpu unavailable)",
                "note": note,
                "cpu": {"model": models[0] if models else None,
                        "cpus_logical": str(len(models)) if models else None}}
    return {"error": f"could not read CPU info ({note})"}


def get_memory_info() -> dict:
    """RAM total/available and swap from /proc/meminfo; plus physical slot
    details (slots used/free, max capacity, stick sizes) from dmidecode,
    which usually needs root — degrades gracefully without it."""
    result: dict = {}
    raw = _read("/proc/meminfo")
    if raw:
        kv = _kv_lines(raw)

        def kb(key):
            v = kv.get(key, "")
            m = re.match(r"(\d+)", v)
            return int(m.group(1)) if m else None

        total, avail = kb("MemTotal"), kb("MemAvailable")
        result["memory"] = {
            "total_gib": _gib(total, "kb") if total else None,
            "available_gib": _gib(avail, "kb") if avail else None,
            "swap_total_gib": _gib(kb("SwapTotal") or 0, "kb"),
            "source": "/proc/meminfo",
        }
    else:
        result["memory"] = {"error": "could not read /proc/meminfo"}

    # Physical layout — needs root on most systems. Escalation ladder:
    # direct (we're root) -> scoped sudoers rule (sudo -n) -> honest "unavailable".
    ok, out, note = _run("dmidecode_memory")
    if not ok and os.geteuid() != 0 and shutil.which("sudo") and shutil.which("dmidecode"):
        ok2, out2, _ = _run("dmidecode_memory_sudo")
        if ok2:
            ok, out, note = ok2, out2, ""
    if ok:
        devices = []
        max_cap = None
        for block in out.split("\n\n"):
            if "Maximum Capacity:" in block:
                m = re.search(r"Maximum Capacity:\s*(.+)", block)
                if m:
                    max_cap = m.group(1).strip()
            if block.strip().startswith("Memory Device") or "Memory Device" in block.split("\n")[0:2]:
                kv = _kv_lines(block)
                devices.append({
                    "slot": kv.get("Locator"),
                    "size": kv.get("Size"),
                    "type": kv.get("Type"),
                    "speed": kv.get("Speed"),
                    "manufacturer": kv.get("Manufacturer"),
                })
        populated = [d for d in devices if d["size"] and "No Module" not in d["size"]]
        result["physical"] = {
            "source": "dmidecode -t memory",
            "max_capacity": max_cap,
            "slots_total": len(devices),
            "slots_used": len(populated),
            "modules": populated,
        }
    else:
        hint = ""
        if os.geteuid() != 0 and shutil.which("dmidecode"):
            hint = (" (dmidecode needs root — install the scoped sudoers rule from "
                    "the README, or re-run with sudo, to see slot layout and max capacity)")
        result["physical"] = {"unavailable": f"{note}{hint}"}
    return result


def get_storage_info() -> dict:
    """Disks and partitions: size, SSD vs HDD, NVMe vs SATA, model, mounts."""
    ok, out, note = _run("lsblk")
    if ok:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return {"error": "could not parse lsblk output"}
        disks = []
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            size = dev.get("size")
            disks.append({
                "name": dev.get("name"),
                "size_gib": _gib(int(size)) if size else None,
                "kind": "HDD (rotational)" if dev.get("rota") in (True, "1", 1) else "SSD / non-rotational",
                "interface": dev.get("tran") or "unknown",
                "is_nvme": (dev.get("tran") == "nvme") or str(dev.get("name", "")).startswith("nvme"),
                "model": (dev.get("model") or "").strip() or None,
                "partitions": [
                    {"name": c.get("name"),
                     "size_gib": _gib(int(c["size"])) if c.get("size") else None,
                     "fstype": c.get("fstype"),
                     "mountpoint": c.get("mountpoint")}
                    for c in dev.get("children", []) or []
                ],
            })
        return {"source": "lsblk", "disks": disks}
    # Fallback: /sys/block
    disks = []
    for path in sorted(glob.glob("/sys/block/*")):
        name = os.path.basename(path)
        if name.startswith(("loop", "ram", "zram")):
            continue
        rota = _read(f"{path}/queue/rotational")
        size = _read(f"{path}/size")  # 512-byte sectors
        disks.append({
            "name": name,
            "size_gib": _gib(int(size) * 512) if size and size.isdigit() else None,
            "kind": "HDD (rotational)" if rota == "1" else "SSD / non-rotational",
            "is_nvme": name.startswith("nvme"),
            "model": _read(f"{path}/device/model"),
        })
    if disks:
        return {"source": "/sys/block (lsblk unavailable)", "note": note, "disks": disks}
    return {"error": f"could not read storage info ({note})"}


def get_gpu_info() -> dict:
    """Graphics adapters (from PCI VGA/3D/Display entries)."""
    ok, out, note = _run("lspci")
    if ok:
        gpus = [ln.strip() for ln in out.splitlines()
                if re.search(r"VGA compatible|3D controller|Display controller", ln)]
        return {"source": "lspci", "gpus": gpus or ["no discrete/integrated GPU rows found in lspci"]}
    # Fallback: DRM subsystem
    cards = glob.glob("/sys/class/drm/card?")
    if cards:
        return {"source": "/sys/class/drm (lspci unavailable)", "note": note,
                "gpus": [os.path.basename(c) for c in cards]}
    return {"unavailable": f"no GPU info available ({note})"}


def get_pci_devices() -> dict:
    """All PCI devices (network, storage controllers, USB controllers...)."""
    ok, out, note = _run("lspci")
    if ok:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"source": "lspci", "count": len(lines), "devices": lines[:60]}
    return {"unavailable": f"lspci not available ({note})"}


def get_usb_devices() -> dict:
    """USB devices currently attached."""
    ok, out, note = _run("lsusb")
    if ok:
        return {"source": "lsusb", "devices": [ln for ln in out.splitlines() if ln.strip()]}
    return {"unavailable": f"lsusb not available ({note})"}


def get_system_info() -> dict:
    """Machine make/model, OS, kernel, hostname."""
    dmi = "/sys/class/dmi/id"
    os_release = {}
    raw = _read("/etc/os-release")
    if raw:
        for line in raw.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                os_release[k] = v.strip('"')
    return {
        "machine": {
            "vendor": _read(f"{dmi}/sys_vendor"),
            "product": _read(f"{dmi}/product_name"),
            "board": _read(f"{dmi}/board_name"),
            "bios_version": _read(f"{dmi}/bios_version"),
            "source": "/sys/class/dmi/id (null fields need root or aren't exposed on this machine)",
        },
        "os": {
            "distro": os_release.get("PRETTY_NAME"),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
        },
    }


def get_battery_info() -> dict:
    """Battery presence, capacity and health from /sys/class/power_supply."""
    batteries = []
    for path in sorted(glob.glob("/sys/class/power_supply/*")):
        if (_read(f"{path}/type") or "").lower() != "battery":
            continue
        full = _read(f"{path}/energy_full") or _read(f"{path}/charge_full")
        design = _read(f"{path}/energy_full_design") or _read(f"{path}/charge_full_design")
        health = None
        if full and design and full.isdigit() and design.isdigit() and int(design) > 0:
            health = f"{round(100 * int(full) / int(design))}% of design capacity"
        batteries.append({
            "name": os.path.basename(path),
            "status": _read(f"{path}/status"),
            "percent_now": _read(f"{path}/capacity"),
            "health": health,
            "source": "/sys/class/power_supply",
        })
    if not batteries:
        return {"batteries": [], "note": "no battery found (desktop, VM, or container?)"}
    return {"batteries": batteries}


def get_network_info() -> dict:
    """Network interfaces: type, state, MAC, link speed. Read from sysfs."""
    nics = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        wireless = os.path.isdir(f"{path}/wireless")
        nics.append({
            "name": name,
            "type": "loopback" if name == "lo" else ("wifi" if wireless else "ethernet/other"),
            "state": _read(f"{path}/operstate"),
            "mac": _read(f"{path}/address"),
            "speed_mbps": _read(f"{path}/speed"),
        })
    return {"source": "/sys/class/net", "interfaces": nics}


# ------------------------------------------------------------- registry --

TOOLS = {
    "get_cpu_info": get_cpu_info,
    "get_memory_info": get_memory_info,
    "get_storage_info": get_storage_info,
    "get_gpu_info": get_gpu_info,
    "get_pci_devices": get_pci_devices,
    "get_usb_devices": get_usb_devices,
    "get_system_info": get_system_info,
    "get_battery_info": get_battery_info,
    "get_network_info": get_network_info,
}


def discover_capabilities() -> tuple[dict, list[str]]:
    """Startup capability discovery (dynamic tool registry): work out which
    tools can actually produce data on THIS machine, so the LLM is only
    offered tools that exist — it never wastes a round trip invoking a tool
    that is guaranteed to fail. Returns (available_tools, excluded_notes)."""
    available: dict = {}
    excluded: list[str] = []
    for name, fn in TOOLS.items():
        if name == "get_pci_devices" and not shutil.which("lspci"):
            excluded.append(f"{name} (lspci not installed)")
            continue
        if name == "get_usb_devices" and not shutil.which("lsusb"):
            excluded.append(f"{name} (lsusb not installed)")
            continue
        if (name == "get_gpu_info" and not shutil.which("lspci")
                and not glob.glob("/sys/class/drm/card*")):
            excluded.append(f"{name} (no lspci and no /sys/class/drm)")
            continue
        available[name] = fn
    return available, excluded


def tool_schemas() -> list[dict]:
    """Tool definitions in the shape LLM tool-calling APIs expect — only the
    tools this machine can actually serve (see discover_capabilities)."""
    available, _ = discover_capabilities()
    return [
        {"name": name,
         "description": fn.__doc__.strip(),
         "input_schema": {"type": "object", "properties": {}, "required": []}}
        for name, fn in available.items()
    ]


def call_tool(name: str) -> dict:
    """Execute one registered tool by name. Unknown names are refused —
    the LLM can only reach the allow-listed registry above."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}' — not in the allow-list"}
    try:
        return fn()
    except Exception as e:  # a tool bug must never kill the chat
        return {"error": f"tool '{name}' raised {type(e).__name__}: {e}"}
