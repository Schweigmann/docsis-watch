"""
Host context: load, memory, and which interface carries the default route.
The point is to rule the computer in or out — a call that stutters while load1
is 9 on an 8-core laptop is not a network report.
"""
import os, platform, re, subprocess
from store import now_ms


def default_iface():
    for cmd, pat in ((["route", "-n", "get", "default"], r"interface:\s*(\S+)"),
                     (["ip", "route", "get", "1.1.1.1"], r"\bdev\s+(\S+)")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        m = re.search(pat, out)
        if m:
            return m.group(1)
    return None


def iface_is_wifi(iface):
    """macOS: ask networksetup which hardware port owns the device. Linux:
    presence in /sys/class/net/<if>/wireless."""
    if not iface:
        return None
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["networksetup", "-listallhardwareports"],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return None
        blocks = out.split("\n\n")
        for b in blocks:
            if f"Device: {iface}" in b:
                return "wi-fi" in b.lower() or "airport" in b.lower()
        return None
    return os.path.isdir(f"/sys/class/net/{iface}/wireless")


def mem_used_pct():
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            def pages(name):
                m = re.search(rf"{name}:\s+(\d+)", out)
                return int(m.group(1)) if m else 0
            free = pages("Pages free") + pages("Pages speculative")
            used = pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
            total = free + used + pages("Pages inactive")
            return round(100.0 * used / total, 1) if total else None
        except Exception:
            return None
    try:
        info = dict(re.findall(r"^(\w+):\s+(\d+)", open("/proc/meminfo").read(), re.M))
        total, avail = int(info["MemTotal"]), int(info["MemAvailable"])
        return round(100.0 * (total - avail) / total, 1)
    except Exception:
        return None


def sample():
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load1 = None
    iface = default_iface()
    return {"load1": load1, "cpu_count": os.cpu_count(), "mem_used_pct": mem_used_pct(),
            "default_iface": iface, "iface_is_wifi": iface_is_wifi(iface)}


def run(store, interval, stop):
    while not stop.is_set():
        try:
            store.add_host(now_ms(), **sample())
            store.add_health("host", True, "ok")
        except Exception as e:
            store.add_health("host", False, f"{type(e).__name__}: {e}")
        stop.wait(interval)
