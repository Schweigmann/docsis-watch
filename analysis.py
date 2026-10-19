"""
Turn an annotation into an explainable incident analysis.

Windows around the mark t (duration d, default 30s):
  baseline  [t-60min, t-5min)      the line's own normal, not a universal threshold
  before    [t-5min,  t-30s)
  during    [t-15s,   t+d+15s)
  after     [t+d+60s, t+d+60s+2min)

Every finding carries evidence, contradictions and missing signals. Confidence
is "high" only when two independent layers (active probes, DOCSIS, Wi-Fi, host)
point the same way. A measurement gap in `during` blocks conclusions for that
layer rather than being read as "nothing happened".
"""
import re, statistics
from datetime import datetime, timezone

MIN, SEC = 60_000, 1_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_private(a):
    return (a.startswith("192.168.") or a.startswith("10.") or a in ("127.0.0.1",)
            or any(a.startswith(f"172.{n}.") for n in range(16, 32)))


# ---------------------------------------------------------------- pings
def summarise_pings(rows):
    if not rows:
        return None
    rtts = [r["rtt_ms"] for r in rows if r["rtt_ms"] is not None]
    lost = sum(1 for r in rows if r["lost"])
    rtts_sorted = sorted(rtts)
    p95 = rtts_sorted[int(0.95 * (len(rtts_sorted) - 1))] if rtts_sorted else None
    return {"n": len(rows), "loss_pct": round(100.0 * lost / len(rows), 1),
            "rtt_med": round(statistics.median(rtts), 1) if rtts else None,
            "rtt_p95": round(p95, 1) if p95 is not None else None,
            "rtt_max": round(max(rtts), 1) if rtts else None,
            "jitter": round(statistics.pstdev(rtts), 1) if len(rtts) > 1 else None}


def degraded(base, cur):
    if not base or not cur or cur["n"] < 5:
        return False, []
    why = []
    if cur["loss_pct"] > max(2.0, base["loss_pct"] * 3):
        why.append(f"loss {base['loss_pct']}%→{cur['loss_pct']}%")
    if base["rtt_med"] and cur["rtt_med"] and cur["rtt_med"] > max(base["rtt_med"] * 2, base["rtt_med"] + 20):
        why.append(f"median RTT {base['rtt_med']}→{cur['rtt_med']}ms")
    if base["jitter"] is not None and cur["jitter"] is not None and cur["jitter"] > max(5.0, base["jitter"] * 3):
        why.append(f"jitter ±{base['jitter']}→±{cur['jitter']}ms")
    return bool(why), why


def fault_domain(gw, externals):
    """Compare the nearest hop against off-net targets. All traffic crosses the
    gateway, so gateway-clean + off-net-degraded places the fault beyond it."""
    gw_bad, gw_why = degraded(gw[1], gw[2]) if gw else (False, [])
    ext = [(n, *degraded(b, d)) for n, b, d in externals]
    bad = [e for e in ext if e[1]]
    if gw_bad and bad:
        return "lan_wifi_gateway", "LAN / Wi-Fi / gateway", gw_why + bad[0][2]
    if not gw_bad and len(bad) >= 2:
        return "access_line", "Access line / DOCSIS / ISP", bad[0][2]
    if not gw_bad and len(bad) == 1:
        return "single_destination", f"Routing / peering / the service behind {bad[0][0]}", bad[0][2]
    if gw_bad and not bad:
        return "artifact", "Inconclusive — gateway ICMP deprioritised?", gw_why
    return "clean", "Not corroborated by active probes", []


# ---------------------------------------------------------------- modem
COUNTER_WRAP = 2 ** 32


def channel_deltas(rows_before, rows_during, rebooted=None):
    """Per-channel counter change from the last sample before to the last
    sample during. A counter going backwards is either a reset (reboot) or a
    32-bit wrap: OFDM corrected-codeword counters reach billions within weeks.
    It is only read as a wrap when modem uptime proves there was no reboot
    (`rebooted is False`) and the old value was in the top half of the range;
    otherwise it stays "reset", never a negative rate."""
    def last_by_channel(rows):
        out = {}
        for r in rows:
            out[(r["direction"], r["channel_id"])] = r
        return out
    a, b = last_by_channel(rows_before), last_by_channel(rows_during)
    deltas = []
    for key in sorted(set(a) & set(b), key=lambda k: (k[0], _cid(k[1]))):
        ra, rb = a[key], b[key]
        d = {"direction": key[0], "channel_id": key[1], "channel_type": rb.get("channel_type"),
             "seconds": round((rb["ts_ms"] - ra["ts_ms"]) / 1000)}
        for f in ("corrected", "uncorrected", "t3_timeouts", "t4_timeouts"):
            if ra.get(f) is None or rb.get(f) is None:
                d[f] = None
            else:
                diff = rb[f] - ra[f]
                if diff < 0 and rebooted is False and ra[f] >= COUNTER_WRAP // 2:
                    diff += COUNTER_WRAP
                d[f] = "reset" if diff < 0 else diff
        for f in ("snr", "power"):
            d[f"{f}_before"], d[f"{f}_during"] = ra.get(f), rb.get(f)
        d["lock_before"], d["lock_during"] = ra.get("lock_status"), rb.get("lock_status")
        deltas.append(d)
    return deltas


def _cid(c):
    try:
        return int(c)
    except (TypeError, ValueError):
        return 10**9


def modem_findings(deltas, states_before, states_during, probes_domain):
    findings, missing = [], []
    if not deltas:
        missing.append("DOCSIS channel counters (modem not reachable or no sample in window)")
        return findings, missing
    uncorr = [d for d in deltas if isinstance(d.get("uncorrected"), int) and d["uncorrected"] > 0]
    resets = [d for d in deltas if "reset" in (d.get("corrected"), d.get("uncorrected"))]
    t3 = [d for d in deltas if isinstance(d.get("t3_timeouts"), int) and d["t3_timeouts"] > 0]
    t4 = [d for d in deltas if isinstance(d.get("t4_timeouts"), int) and d["t4_timeouts"] > 0]
    snr_drop = [d for d in deltas if d.get("snr_before") and d.get("snr_during")
                and d["snr_during"] < d["snr_before"] - 3]
    unlock = [d for d in deltas if d.get("lock_before") and d.get("lock_during")
              and str(d["lock_before"]).lower() != str(d["lock_during"]).lower()]

    if resets:
        findings.append(_f("Modem counters reset — modem likely rebooted or re-registered "
                           "in the window", "high",
                           [f"{len(resets)} channel counter(s) went backwards"],
                           [], [], "Check modem uptime and event log for the same minute.", layer="docsis"))
    if uncorr:
        total = sum(d["uncorrected"] for d in uncorr)
        ev = [f"uncorrectable codewords +{total} across {len(uncorr)} downstream channel(s)"]
        ev += [f"ch {d['channel_id']} ({d.get('channel_type') or '?'}): +{d['uncorrected']}"
               for d in uncorr[:4]]
        conf = "high" if probes_domain == "access_line" else "medium"
        contra = [] if probes_domain in ("access_line", "clean") else \
            [f"active probes point to {probes_domain}, not the access line"]
        findings.append(_f("Downstream RF impairment — uncorrectable errors rose during the "
                           "incident", conf, ev, contra, [],
                           "Ask your ISP to pull CMTS-side RxMER/FEC for this CM and window.", layer="docsis"))
    if t3 or t4:
        ev = [f"T3 timeouts +{sum(d['t3_timeouts'] for d in t3)}" for _ in [0] if t3]
        ev += [f"T4 timeouts +{sum(d['t4_timeouts'] for d in t4)}" for _ in [0] if t4]
        findings.append(_f("Upstream ranging trouble — T3/T4 timeouts in the window",
                           "high" if t4 else "medium", ev, [], [],
                           "T4 means a re-registration: a real outage. Report the exact time.", layer="docsis"))
    if snr_drop:
        findings.append(_f("Downstream SNR/MER dropped ≥3 dB on one or more channels", "medium",
                           [f"ch {d['channel_id']}: {d['snr_before']}→{d['snr_during']} dB" for d in snr_drop[:4]],
                           [], [], "Correlate with ingress/noise at the same minute.", layer="docsis"))
    if unlock:
        findings.append(_f("Channel lock status changed during the incident", "medium",
                           [f"ch {d['channel_id']}: {d['lock_before']}→{d['lock_during']}" for d in unlock[:4]],
                           [], [], "Partial-service events are worth flagging to your ISP.", layer="docsis"))
    if states_before and states_during:
        ub, ud = states_before[-1].get("uptime_s"), states_during[-1].get("uptime_s")
        if ub is not None and ud is not None and ud < ub:
            findings.append(_f("Modem uptime reset — the modem rebooted", "high",
                               [f"uptime {ub}s → {ud}s"], [], [], "This is the incident.", layer="docsis"))
    if not findings:
        findings.append(_f("DOCSIS layer looks stable across the window", "medium",
                           [f"{len(deltas)} channels compared; no uncorrectables, timeouts, "
                            f"SNR drops or lock changes"], [], [],
                           "If probes still show a WAN fault, it is beyond the RF layer.", layer=None))
    return findings, missing


def rebooted_between(states_before, states_during):
    """True/False from modem uptime; None when uptime isn't available."""
    if not states_before or not states_during:
        return None
    ub, ud = states_before[-1].get("uptime_s"), states_during[-1].get("uptime_s")
    if ub is None or ud is None:
        return None
    return ud < ub


# Events that mean the RF/ranging layer actually failed, vs. housekeeping.
EVENT_FAULT = re.compile(r"T[1-4] time-?out|No Ranging Response|Ranging Request Retries|"
                         r"SYNC Timing|Lost MDD|Loss of (Sync|QAM|FEC)|Partial Service|"
                         r"Re-?init MAC|Resetting|reboot|CM-STATUS|lost lock", re.I)
EVENT_PROFILE = re.compile(r"profile", re.I)


def event_findings(events):
    """The modem's own log, in the operator's own vocabulary. A fault event in
    the window is strong evidence on its own; a profile change means the CMTS
    moved this modem to a more robust (slower) modulation profile."""
    if not events:
        return []
    def line(e):
        return f"{iso(e['ts_ms'])[11:19]}Z [{e.get('priority') or '?'}] {e['message'][:160]}"
    faults = [e for e in events if EVENT_FAULT.search(e["message"])]
    profile = [e for e in events if EVENT_PROFILE.search(e["message"]) and e not in faults]
    other = [e for e in events if e not in faults and e not in profile]
    out = []
    if faults:
        out.append(_f("Modem logged DOCSIS fault events during the incident", "high",
                      [line(e) for e in faults[:5]], [], [],
                      "Quote these lines verbatim to your ISP: they are the modem's own words.",
                      layer="docsis"))
    if profile:
        out.append(_f("CMTS changed this modem's modulation profile during the incident", "medium",
                      [line(e) for e in profile[:5]], [], [],
                      "A profile change follows measured signal quality; ask which profile and why.",
                      layer="docsis"))
    if other and not (faults or profile):
        out.append(_f("Modem logged events near the incident (informational)", "low",
                      [line(e) for e in other[:5]], [], [], "Context only; not evidence of a fault.",
                      layer=None))
    return out


# ---------------------------------------------------------------- line health (plain language)
# "Out of range" limits are the ha-ziggo-modem defaults for Ziggo Sagemcom modems
# (DS power -10..+10 dBmV, DS SNR >= 34 dB, US power <= 52 dBmV); "good" is
# those minus a margin. Rules of thumb for a dashboard, not a Ziggo spec.
LIMITS = {
    "ds_power": {"good": (-7, 7), "ok": (-10, 10), "unit": "dBmV", "good_txt": "−7 to +7 dBmV"},
    "ds_snr":   {"good": (37, None), "ok": (34, None), "unit": "dB", "good_txt": "37 dB or more"},
    "us_power": {"good": (35, 48), "ok": (30, 52), "unit": "dBmV", "good_txt": "35 to 48 dBmV"},
}
# Uncorrectable codewords per hour. A healthy line still loses a handful a day
# (this one: ~7/day on its OFDM channels), so "any at all" would cry wolf.
LOST_OK, LOST_BAD = 10, 1000
RANK = {"good": 0, "watch": 1, "bad": 2, "unknown": -1}


def grade(v, key):
    """good / watch / bad for one value against LIMITS[key]."""
    if v is None:
        return "unknown"
    lim = LIMITS[key]
    def inside(rng):
        lo, hi = rng
        return (lo is None or v >= lo) and (hi is None or v <= hi)
    return "good" if inside(lim["good"]) else "watch" if inside(lim["ok"]) else "bad"


def _worst(grades):
    g = [x for x in grades if x != "unknown"]
    return max(g, key=RANK.get) if g else "unknown"


EVENT_PLAIN = [  # (pattern, plain words, status)
    (re.compile(r"T4 time-?out", re.I), "Lost contact with Ziggo and had to reconnect (T4)", "bad"),
    (re.compile(r"T3 time-?out", re.I), "Upload request went unanswered (T3)", "watch"),
    (re.compile(r"SYNC Timing|Lost MDD|Loss of", re.I), "Lost the download signal", "bad"),
    (re.compile(r"Reboot because of - (.+)$|Resetting", re.I), "Modem restarted", "watch"),
    (re.compile(r"profile assignment change", re.I),
     "Ziggo switched the upload modulation (line quality changed)", "watch"),
    (re.compile(r"DHCP", re.I), "IP address renewal warning (usually harmless)", "good"),
    (re.compile(r"REGISTRATION COMPLETE|Operational", re.I), "Modem (re)connected to Ziggo", "good"),
]


def plain_event(e):
    for rx, words, status in EVENT_PLAIN:
        m = rx.search(e["message"])
        if m:
            if words == "Modem restarted" and m.groups() and m.group(1):
                words = f"Modem restarted — reason: {m.group(1).strip()}"
            return {"ts_ms": e["ts_ms"], "plain": words, "status": status, "raw": e["message"]}
    return {"ts_ms": e["ts_ms"], "plain": e["message"], "status": "unknown", "raw": e["message"]}


def line_health(store, now):
    """The modem card in words: one verdict, a few rows, each with what it
    means and what good looks like. Built from data already collected."""
    latest = store.latest_channels()
    if not latest:
        return {"verdict": "unknown", "headline": "No modem data yet", "items": [], "events": []}
    ds = [c for c in latest if c["direction"] == "downstream"]
    us = [c for c in latest if c["direction"] == "upstream"]
    items = []

    def rng(vals):
        vals = [v for v in vals if v is not None]
        return (min(vals), max(vals)) if vals else (None, None)

    lo, hi = rng(c["power"] for c in ds)
    items.append({"key": "ds_power", "label": "Signal strength (download)",
                  "value": f"{lo} to {hi} dBmV" if lo is not None else "not reported",
                  "status": _worst([grade(lo, "ds_power"), grade(hi, "ds_power")]),
                  "good": LIMITS["ds_power"]["good_txt"],
                  "meaning": "How strong the signal arrives at your modem. Too weak and too strong both cause errors."})
    snrs = [c["snr"] for c in ds if c["snr"] is not None]
    no_ofdm = any(c["channel_type"] == "ofdm" and c["snr"] is None for c in ds)
    items.append({"key": "ds_snr", "label": "Signal quality (download)",
                  "value": f"lowest {min(snrs)} dB" if snrs else "not reported",
                  "status": grade(min(snrs) if snrs else None, "ds_snr"),
                  "good": LIMITS["ds_snr"]["good_txt"],
                  "meaning": "How clean the signal is compared with background noise. Higher is better."
                             + (" Your modem doesn't report it for its OFDM channels." if no_ofdm else "")})
    lo, hi = rng(c["power"] for c in us)
    items.append({"key": "us_power", "label": "Transmit power (upload)",
                  "value": f"{lo} to {hi} dBmV" if lo is not None else "not reported",
                  "status": _worst([grade(lo, "us_power"), grade(hi, "us_power")]),
                  "good": LIMITS["us_power"]["good_txt"],
                  "meaning": "How hard your modem has to 'shout' to reach Ziggo. Near the maximum means a weak return path."})

    # Counters over the last hour (or since the app started, if that's shorter).
    window = store.channels(now - 60 * MIN, now + 1)
    since = window[0]["ts_ms"] if window else latest[0]["ts_ms"]
    first = [r for r in window if r["ts_ms"] == since]
    states = store.modem_states(since, now + 1)
    deltas = channel_deltas(first, latest, rebooted_between(states[:1], states[-1:]))         if first and since != latest[0]["ts_ms"] else []
    span = f"since {datetime.fromtimestamp(since / 1000).strftime('%H:%M')}"         if now - since < 55 * MIN else "last hour"
    if deltas:
        unc = sum(d["uncorrected"] for d in deltas if isinstance(d.get("uncorrected"), int))
        reset = any(d.get("uncorrected") == "reset" for d in deltas)
        t3 = sum(d["t3_timeouts"] for d in deltas if isinstance(d.get("t3_timeouts"), int))
        t4 = sum(d["t4_timeouts"] for d in deltas if isinstance(d.get("t4_timeouts"), int))
        unc_v, unc_s = ("counters reset (modem restarted)", "watch") if reset else             (f"{unc} ({span})", "good" if unc < LOST_OK else "watch" if unc < LOST_BAD else "bad")
        to_v = f"T3 {t3} · T4 {t4} ({span})"
        to_s = "bad" if t4 else "watch" if t3 else "good"
    else:
        unc_v, unc_s = "waiting for a second reading (1 min)", "unknown"
        to_v, to_s = unc_v, "unknown"
    items.append({"key": "lost", "label": "Lost data (download)", "value": unc_v, "status": unc_s,
                  "good": f"under {LOST_OK} per hour",
                  "meaning": "Data the modem received but could not repair. Corrected errors are normal; "
                             "uncorrectable ones mean glitches you can notice."})
    items.append({"key": "timeouts", "label": "Upload timeouts", "value": to_v, "status": to_s, "good": "0",
                  "meaning": "The modem asked Ziggo for upload time and got no answer. A T4 means it lost "
                             "contact and had to reconnect."})
    locked = [c for c in latest if str(c["lock_status"]).lower() in ("true", "1", "locked")]
    items.append({"key": "lock", "label": "Channels connected",
                  "value": f"{len(locked)} of {len(latest)} ({len(ds)} download, {len(us)} upload)",
                  "status": "good" if len(locked) == len(latest) else "bad", "good": "all",
                  "meaning": "Your connection is bundled from many channels. A dropped channel means less capacity."})
    st = store.latest_one("modem_state")
    week = [plain_event(e) for e in store.modem_events(now - 7 * 24 * 60 * MIN, now + 1)]
    restarts = [e for e in week if e["plain"].startswith("Modem restarted")]
    up = st and st.get("uptime_s")
    items.append({"key": "restarts", "label": "Restarts (last 7 days)",
                  "value": f"{len(restarts)}" + (f" · running {up // 86400} days" if up else ""),
                  "status": "good" if not restarts else "watch", "good": "0",
                  "meaning": "From the modem's own log. Restarts you didn't cause point to a line or power problem."})

    events = [plain_event(e) for e in reversed(store.modem_events(0, now + 1)[-6:])]
    verdict = _worst([i["status"] for i in items])
    flagged = [i["label"].lower() for i in items if i["status"] == verdict and verdict in ("watch", "bad")]
    headline = {"good": "Line looks healthy",
                "watch": "Line works — keep an eye on " + ", ".join(flagged),
                "bad": "Problem on the line: " + ", ".join(flagged),
                "unknown": "Not enough data yet"}[verdict]
    return {"verdict": verdict, "headline": headline, "items": items, "events": events,
            "limits": LIMITS}


# ---------------------------------------------------------------- wifi / host
def wifi_findings(base_rows, during_rows):
    if not during_rows:
        return [], ["Wi-Fi link telemetry (unsupported on this host, or no sample in window)"]
    def med(rows, f):
        v = [r[f] for r in rows if r.get(f) is not None]
        return statistics.median(v) if v else None
    disc = [r for r in during_rows if r["connected"] == 0]  # None = unknown, not down
    rb, rd = med(base_rows, "rssi_dbm"), med(during_rows, "rssi_dbm")
    tb, td = med(base_rows, "tx_rate_mbps"), med(during_rows, "tx_rate_mbps")
    nb, nd = med(base_rows, "noise_dbm"), med(during_rows, "noise_dbm")
    out = []
    if disc:
        out.append(_f("Wi-Fi disconnected during the incident", "high",
                      [f"{len(disc)} sample(s) with no association"], [], [],
                      "This alone explains a frozen call. Not a Ziggo issue.", layer="wifi"))
    ev = []
    if rb is not None and rd is not None and rd < rb - 8:
        ev.append(f"RSSI {rb:.0f}→{rd:.0f} dBm")
    if tb and td and td < tb * 0.5:
        ev.append(f"tx rate {tb:.0f}→{td:.0f} Mbps")
    if nb is not None and nd is not None and nd > nb + 6:
        ev.append(f"noise floor {nb:.0f}→{nd:.0f} dBm")
    if ev:
        out.append(_f("Wi-Fi link degraded during the incident", "medium", ev, [], [],
                      "Re-test on Ethernet before attributing anything to DOCSIS.", layer="wifi"))
    if rd is not None and rd < -75 and not ev:
        out.append(_f("Wi-Fi signal weak throughout (not just during)", "low",
                      [f"median RSSI {rd:.0f} dBm"], [], [], "Weak but stable; noted as context.", layer=None))
    missing = []
    if during_rows and rd is None:
        missing.append("Wi-Fi RSSI (fields redacted — grant Location Services or use Ethernet)")
    return out, missing


def host_findings(during_rows):
    if not during_rows:
        return [], ["Host load/memory"]
    loads = [r["load1"] for r in during_rows if r.get("load1") is not None]
    cpus = next((r["cpu_count"] for r in during_rows if r.get("cpu_count")), None)
    out = []
    if loads and cpus and max(loads) > cpus * 1.5:
        out.append(_f("Computer was heavily loaded during the incident", "medium",
                      [f"load1 peaked at {max(loads):.1f} on {cpus} cores"], [], [],
                      "A saturated CPU stutters video regardless of the network.", layer="host"))
    return out, []


# ---------------------------------------------------------------- assembly
def _f(hypothesis, confidence, evidence, contradictions, missing, next_step, layer=None):
    """`layer` is one of probes / docsis / wifi / host, or None for a finding
    that reports stability and must not count as a layer 'agreeing'."""
    return {"hypothesis": hypothesis, "confidence": confidence, "evidence": evidence,
            "contradictions": contradictions, "missing": missing, "next_step": next_step,
            "layer": layer}


def analyse(store, ann):
    t = ann["ts_ms"]
    d = (ann.get("duration_s") or 30) * SEC
    W = {"baseline": (t - 60 * MIN, t - 5 * MIN),
         "before":   (t - 5 * MIN, t - 30 * SEC),
         "during":   (t - 15 * SEC, t + d + 15 * SEC),
         "after":    (t + d + 60 * SEC, t + d + 60 * SEC + 2 * MIN)}

    # -- active probes
    targets = store.ping_targets(W["baseline"][0])
    probes = {}
    for tgt in targets:
        probes[tgt] = {k: summarise_pings(store.pings(lo, hi, tgt)) for k, (lo, hi) in W.items()}
    gw_name = next((x for x in targets if is_private(x)), None)
    ext_names = [x for x in targets if x != gw_name]
    def ref(tgt):  # baseline if we have it, else the 5-min "before"
        return probes[tgt]["baseline"] or probes[tgt]["before"]
    gw = (gw_name, ref(gw_name), probes[gw_name]["during"]) if gw_name else None
    externals = [(x, ref(x), probes[x]["during"]) for x in ext_names]
    gap = not any(p["during"] and p["during"]["n"] >= 5 for p in probes.values())
    if gap:
        domain, label, why = "gap", "No conclusion — active probes have a measurement gap here", []
    else:
        domain, label, why = fault_domain(gw, externals)

    findings, missing = [], []
    if domain == "gap":
        missing.append("Active probe data in the incident window (agent not running?)")
    elif domain == "access_line":
        findings.append(_f("Fault beyond the gateway — access line / DOCSIS / ISP", "medium",
                           [f"nearest hop {gw_name} stayed clean", *why], [], [],
                           "Combine with DOCSIS counters below; if they moved too, confidence is high.", layer="probes"))
    elif domain == "lan_wifi_gateway":
        findings.append(_f("Fault at or before the gateway — Wi-Fi, LAN or the gateway itself",
                           "medium", [f"nearest hop {gw_name} degraded: {'; '.join(why)}"],
                           [], [], "Not a Ziggo access-line report. Check Wi-Fi findings.", layer="probes"))
    elif domain == "single_destination":
        findings.append(_f(label, "medium", why, [], [],
                           "Single-destination degradation is routing or the far end.", layer="probes"))
    elif domain == "artifact":
        findings.append(_f("Gateway ICMP degraded while off-net stayed clean — likely artifact",
                           "low", why, ["off-net targets unaffected"], [],
                           "Routers deprioritise ICMP to themselves. Do not report.", layer=None))
    if not gw_name:
        missing.append("A gateway/private-range ping target (needed to separate local from access-line faults)")

    # -- modem
    ch_before = store.channels(W["before"][0], W["before"][1])
    ch_during = store.channels(W["during"][0], W["during"][1])
    if not ch_during:  # fall back to the closest sample after the mark
        ch_during = store.channels(W["during"][0], W["after"][1])
    st_before, st_during = store.modem_states(*W["before"]), store.modem_states(*W["during"])
    if not st_during:
        st_during = store.modem_states(W["during"][0], W["after"][1])
    deltas = channel_deltas(ch_before, ch_during, rebooted_between(st_before, st_during))
    mf, mm = modem_findings(deltas, st_before, st_during, domain)
    # Event timestamps come from the modem's clock: allow ±45s of slack.
    events = store.modem_events(W["during"][0] - 45 * SEC, W["during"][1] + 45 * SEC)
    ef = event_findings(events)
    if ef and len(mf) == 1 and mf[0]["layer"] is None:
        mf = []  # "DOCSIS layer looks stable" contradicts a fault in the modem's own log
    findings += mf + ef; missing += mm
    if not store.has_modem_events():
        missing.append("DOCSIS event log (not exposed by this modem, or not polled yet)")

    # -- wifi / host
    wf, wm = wifi_findings(store.wifi_rows(*W["baseline"]) or store.wifi_rows(*W["before"]),
                           store.wifi_rows(*W["during"]))
    findings += wf; missing += wm
    hf, hm = host_findings(store.host_rows(*W["during"]))
    findings += hf; missing += hm

    # -- collector health gaps
    bad = [h for h in store.health_rows(*W["during"]) if not h["ok"]]
    for h in {b["collector"]: b for b in bad}.values():
        missing.append(f"{h['collector']}: {h['message']}")

    # -- overall
    layers = {f["layer"] for f in findings if f["layer"]}
    if domain == "gap":
        overall, conf = "No conclusion possible: measurement gap", "none"
    elif domain == "access_line" and "docsis" in layers:
        overall, conf = "Access line / DOCSIS impairment", "high"
    elif domain == "access_line":
        overall, conf = "Beyond the gateway (access line or ISP); RF layer did not confirm", "medium"
    elif "wifi" in layers or domain == "lan_wifi_gateway":
        overall, conf = "Local: Wi-Fi / LAN / gateway", "high" if ("wifi" in layers and domain == "lan_wifi_gateway") else "medium"
    elif "host" in layers and domain == "clean":
        overall, conf = "The computer, not the network", "medium"
    elif "docsis" in layers:
        overall, conf = "RF layer moved but active probes did not — weak or transient", "low"
    elif domain == "single_destination":
        overall, conf = label, "medium"
    else:
        overall, conf = "Not corroborated by any measured layer", "low"

    return {
        "annotation": ann,
        "windows": {k: {"start": iso(lo), "end": iso(hi), "start_ms": lo, "end_ms": hi}
                    for k, (lo, hi) in W.items()},
        "probes": probes, "gateway": gw_name,
        "fault_domain": {"code": domain, "label": label, "evidence": why},
        "channel_deltas": deltas,
        "modem_events": events,
        "findings": findings, "missing_signals": missing,
        "overall": {"hypothesis": overall, "confidence": conf,
                    "layers_agreeing": sorted(layers)},
    }
