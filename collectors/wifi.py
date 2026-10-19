"""
Wi-Fi link telemetry for the machine the agent runs on.

macOS: `airport` is dead since 14.4 and `wdutil info` needs sudo (and redacts
SSID/BSSID on Sequoia). We read CoreWLAN's cached link properties through
`osascript -l JavaScript` (stock macOS, ~50 ms): signal, noise, tx rate, channel
and PHY mode, but no MCS, and SSID/BSSID come back nil without Location access.
`system_profiler SPAirPortDataType` is only the fallback: every call makes
airportd run ~8 s of live off-channel scans, which put 60-120 ms spikes on all
traffic every poll and degraded the very calls being measured.
SSID/BSSID are hashed by the store; they never touch disk in the clear.

Linux: `iw dev <if> link` if present. Anything else: unsupported, with a health
row saying so — never a silent zero.
"""
import json, platform, re, subprocess

RE_SN = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
RE_NUM = re.compile(r"-?\d+(\.\d+)?")

# Reads properties only. Never call scanForNetworks* here: that is the scan we
# are avoiding.
COREWLAN_JXA = """
ObjC.import('CoreWLAN');
const i = $.CWWiFiClient.sharedWiFiClient.interface;
function s(x) { try { const v = ObjC.unwrap(x); return v === undefined ? null : v; } catch (e) { return null; } }
if (!i || i.isNil()) { 'null'; } else {
  const ch = i.wlanChannel;
  JSON.stringify({interface: s(i.interfaceName), power: i.powerOn,
    rssi: Number(i.rssiValue), noise: Number(i.noiseMeasurement), tx: Number(i.transmitRate),
    phy: Number(i.activePHYMode), mode: Number(i.interfaceMode),
    channel: ch.isNil() ? null : Number(ch.channelNumber),
    band: ch.isNil() ? null : Number(ch.channelBand),
    width: ch.isNil() ? null : Number(ch.channelWidth),
    ssid: s(i.ssid), bssid: s(i.bssid)});
}
"""
CW_PHY = {1: "802.11a", 2: "802.11b", 3: "802.11g", 4: "802.11n", 5: "802.11ac",
          6: "802.11ax", 7: "802.11be"}
CW_BAND = {1: "2GHz", 2: "5GHz", 3: "6GHz"}
CW_WIDTH = {1: "20MHz", 2: "40MHz", 3: "80MHz", 4: "160MHz", 5: "320MHz"}


def _find_keys(obj, needle, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in k.lower() and not isinstance(v, (dict, list)):
                out.append(v)
            _find_keys(v, needle, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_keys(v, needle, out)


def _first(obj, needle):
    out = []
    _find_keys(obj, needle, out)
    return out[0] if out else None


def parse_corewlan_json(text):
    """Output of COREWLAN_JXA. Channel is formatted like system_profiler's
    ("44 (5GHz, 80MHz)") so history stays comparable across the switch."""
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    # interfaceMode 1 = station; rssi 0 = not associated.
    connected = bool(d.get("power")) and d.get("mode") == 1 and bool(d.get("rssi"))
    if not connected:
        return {"interface": d.get("interface"), "connected": False}
    ch = d.get("channel")
    extra = ", ".join(x for x in (CW_BAND.get(d.get("band")), CW_WIDTH.get(d.get("width"))) if x)
    return {
        "interface": d.get("interface"), "connected": True,
        "rssi_dbm": float(d["rssi"]),
        "noise_dbm": float(d["noise"]) if d.get("noise") else None,
        "channel": (f"{ch} ({extra})" if extra else str(ch)) if ch else None,
        "tx_rate_mbps": float(d["tx"]) if d.get("tx") else None,
        "phy_mode": CW_PHY.get(d.get("phy")),
        "mcs": None,
        "ssid": d.get("ssid"), "bssid": d.get("bssid"),
    }


def parse_system_profiler_json(text):
    """Key names are prefixed spairport_*; we match by substring so a rename in
    a macOS release degrades to 'field missing' rather than a crash."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    ifaces = []
    _find_keys(obj, "airport_interfaces", ifaces)
    # _find_keys skips dict/list values; fetch the interfaces list directly.
    def find_list(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if "airport_interfaces" in k.lower() and isinstance(v, list):
                    return v
                r = find_list(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find_list(v)
                if r:
                    return r
        return None
    lst = find_list(obj) or []
    for iface in lst:
        cur = None
        for k, v in iface.items():
            if "current_network" in k.lower() and isinstance(v, dict):
                cur = v
        status = str(_first(iface, "status_information") or "").lower()
        if not cur:
            continue
        sn = str(_first(cur, "signal_noise") or "")
        m = RE_SN.search(sn)
        rate = _first(cur, "network_rate")
        mcs = _first(cur, "network_mcs")
        return {
            "interface": iface.get("_name"),
            "connected": True if cur else ("connected" in status),
            "rssi_dbm": float(m.group(1)) if m else None,
            "noise_dbm": float(m.group(2)) if m else None,
            "channel": str(_first(cur, "network_channel") or "") or None,
            "tx_rate_mbps": float(RE_NUM.search(str(rate)).group()) if rate and RE_NUM.search(str(rate)) else None,
            "phy_mode": _first(cur, "phymode"),
            "mcs": int(mcs) if isinstance(mcs, (int, float)) or (isinstance(mcs, str) and mcs.isdigit()) else None,
            "ssid": cur.get("_name"),
            "bssid": _first(cur, "bssid"),
        }
    # No interface exposed its current-network details. macOS drops that block
    # in some privacy states while the link is up, so "no details" is NOT
    # "disconnected": trust the interface's own status. Reading it as
    # disconnected produced three days of false "Wi-Fi disconnected" findings.
    for iface in lst:
        status = str(_first(iface, "status_information") or "").lower()
        if status:
            return {"interface": iface.get("_name"),
                    "connected": "connected" in status and "disconnected" not in status}
    if lst:
        return {"interface": lst[0].get("_name"), "connected": None}
    return None


def parse_system_profiler_text(text):
    """Fallback for the human-readable form."""
    def grab(label):
        m = re.search(rf"^\s*{label}:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else None
    sn = grab("Signal / Noise")
    m = RE_SN.search(sn or "")
    rate = grab("Transmit Rate")
    if not sn and not rate:
        return None
    return {
        "interface": None, "connected": True,
        "rssi_dbm": float(m.group(1)) if m else None,
        "noise_dbm": float(m.group(2)) if m else None,
        "channel": grab("Channel"), "phy_mode": grab("PHY Mode"),
        "tx_rate_mbps": float(RE_NUM.search(rate).group()) if rate and RE_NUM.search(rate) else None,
        "mcs": int(grab("MCS Index")) if (grab("MCS Index") or "").isdigit() else None,
        "ssid": None, "bssid": grab("BSSID"),
    }


def parse_iw_link(text):
    if "Not connected" in text or not text.strip():
        return {"connected": False}
    def grab(pat):
        m = re.search(pat, text, re.M)
        return m.group(1) if m else None
    rssi = grab(r"signal:\s*(-?\d+)")
    rate = grab(r"tx bitrate:\s*([\d.]+)")
    return {"connected": True,
            "rssi_dbm": float(rssi) if rssi else None, "noise_dbm": None,
            "channel": grab(r"freq:\s*(\d+)"), "phy_mode": None,
            "tx_rate_mbps": float(rate) if rate else None, "mcs": None,
            "ssid": grab(r"SSID:\s*(.+)"), "bssid": grab(r"Connected to\s*(\S+)")}


def sample():
    """Return (dict | None, message). None means unsupported on this host."""
    sysname = platform.system()
    if sysname == "Darwin":
        try:
            out = subprocess.run(["osascript", "-l", "JavaScript", "-e", COREWLAN_JXA],
                                 capture_output=True, text=True, timeout=10).stdout
            w = parse_corewlan_json(out)
            if w is not None:
                return w, ("ok" if w["connected"] else "Wi-Fi not associated")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        try:
            # Fallback only: this triggers live Wi-Fi scans (see module docstring).
            out = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                                 capture_output=True, text=True, timeout=20).stdout
            w = parse_system_profiler_json(out)
            if w is None:
                out = subprocess.run(["system_profiler", "SPAirPortDataType"],
                                     capture_output=True, text=True, timeout=20).stdout
                w = parse_system_profiler_text(out)
            if w is None:
                return None, "system_profiler returned no Wi-Fi interface"
            if w.get("connected") is None:
                return w, "link state unknown: system_profiler gave no status"
            if w.get("connected") and w.get("rssi_dbm") is None:
                return w, "connected but signal fields missing (Location Services redaction?)"
            if not w.get("connected"):
                return w, "Wi-Fi not associated"
            return w, "ok"
        except FileNotFoundError:
            return None, "system_profiler not found"
        except subprocess.TimeoutExpired:
            return None, "system_profiler timed out"
    if sysname == "Linux":
        try:
            dev = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"Interface\s+(\S+)", dev)
            if not m:
                return None, "no wireless interface"
            out = subprocess.run(["iw", "dev", m.group(1), "link"], capture_output=True,
                                 text=True, timeout=5).stdout
            w = parse_iw_link(out)
            w["interface"] = m.group(1)
            return w, "ok"
        except FileNotFoundError:
            return None, "iw not installed"
    return None, f"unsupported platform: {sysname}"


def run(store, interval, stop):
    while not stop.is_set():
        try:
            w, msg = sample()
            if w is None:
                store.add_health("wifi", False, f"unsupported: {msg}")
            else:
                from store import now_ms
                store.add_wifi(now_ms(), **w)
                store.add_health("wifi", True, msg)
        except Exception as e:
            store.add_health("wifi", False, f"{type(e).__name__}: {e}")
        stop.wait(interval)
