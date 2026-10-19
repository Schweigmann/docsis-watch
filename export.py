"""
Incident exports: a human summary (Markdown), the technical data (JSON), and a
ZIP of both — refused outright if the redaction check finds an identifier.

The store never holds MACs, serials, SSIDs or BSSIDs, so this check is a
backstop for free-text notes and anything a future collector might add.
"""
import io, json, re, zipfile
from datetime import datetime, timezone
from analysis import iso

VERSION = "0.1.0"

RE_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
RE_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
RE_TOKEN = re.compile(r"(?i)\b(bearer|token|password|passwd|pwd)\b\s*[:=]\s*\S+")
RE_SERIAL = re.compile(r"(?i)\bserial(?:number)?\b\s*[:=]?\s*[A-Z0-9]{8,}")
RE_CANDIDATE = re.compile(r"\bcandidate:\S+")


def _public_ip(m):
    a, b = int(m.group(1)), int(m.group(2))
    if a == 10 or a == 127 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
        return False
    if a == 0 or a >= 224:
        return False
    return True


def redaction_violations(text, allow_ips=()):
    """`allow_ips`: the configured probe targets. Well-known resolvers we ping
    are not sensitive; the subscriber's own public address is. Anything public
    that is not a configured target is treated as a leak."""
    v = []
    if RE_MAC.search(text):
        v.append("MAC address")
    if any(_public_ip(m) and m.group(0) not in allow_ips for m in RE_IPV4.finditer(text)):
        v.append("public IPv4 address")
    if RE_TOKEN.search(text):
        v.append("credential/token")
    if RE_SERIAL.search(text):
        v.append("serial number")
    if RE_CANDIDATE.search(text):
        v.append("WebRTC ICE candidate")
    return v


def _fmt(s):
    if not s:
        return "no data"
    parts = [f"{s['loss_pct']}% loss"]
    if s["rtt_med"] is not None:
        parts.append(f"{s['rtt_med']}ms med / {s['rtt_p95']}ms p95")
    if s["jitter"] is not None:
        parts.append(f"±{s['jitter']}ms")
    return ", ".join(parts)


def summary_md(a, cm_mac_hint="<fill in from modem label>", trial_id="<fill in>"):
    ann = a["annotation"]
    t = ann["ts_ms"]
    local = datetime.fromtimestamp(t / 1000).astimezone()
    L = []
    w = L.append
    w(f"### Incident {iso(t)[:10]} #{ann['id']}")
    w("")
    w(f"**Timestamp (UTC):**  {iso(t)}")
    w(f"**Local time:**       {local.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    w(f"**CM MAC:**           {cm_mac_hint}")
    w(f"**Ticket / ref:**      {trial_id}")
    w("")
    w("**What I experienced (subjective)**")
    w("")
    w(f"- Symptom: **{ann['category'].replace('_', ' ')}**"
      + (f", severity {ann['severity']}/5" if ann.get("severity") else ""))
    if ann.get("duration_s"):
        w(f"- Duration: ~{ann['duration_s']}s")
    if ann.get("call_app"):
        w(f"- App: {ann['call_app']}")
    if ann.get("connection"):
        w(f"- Computer connected via: {ann['connection']}")
    if ann.get("note"):
        w(f"- Note: {ann['note']}")
    w("")
    o = a["overall"]
    w(f"**Most likely: {o['hypothesis']}** — confidence **{o['confidence']}**"
      + (f" ({len(o['layers_agreeing'])} layer(s) agree: {', '.join(o['layers_agreeing'])})"
         if o["layers_agreeing"] else ""))
    w("")
    w("**Active probes (baseline → during → after)**")
    w("")
    w("| Target | Baseline | During | After |")
    w("|---|---|---|---|")
    for tgt, p in a["probes"].items():
        tag = " (gateway)" if tgt == a["gateway"] else ""
        w(f"| `{tgt}`{tag} | {_fmt(p['baseline'] or p['before'])} | **{_fmt(p['during'])}** | {_fmt(p['after'])} |")
    w("")
    w(f"Fault domain from probes: **{a['fault_domain']['label']}**"
      + (f" — {'; '.join(a['fault_domain']['evidence'])}" if a["fault_domain"]["evidence"] else ""))
    w("")
    if a["channel_deltas"]:
        w("**DOCSIS counters (change from last sample before → last sample during)**")
        w("")
        w("| Dir | Ch | Type | Corrected Δ | Uncorrectable Δ | T3 Δ | T4 Δ | SNR before→during | Lock |")
        w("|---|---|---|---|---|---|---|---|---|")
        for d in a["channel_deltas"]:
            def v(x):
                return "n/a" if x is None else x
            snr = f"{v(d['snr_before'])}→{v(d['snr_during'])}" if d["snr_before"] is not None or d["snr_during"] is not None else "n/a"
            lock = d["lock_during"] if d["lock_before"] == d["lock_during"] else f"{d['lock_before']}→{d['lock_during']}"
            w(f"| {d['direction'][:2]} | {d['channel_id']} | {d.get('channel_type') or ''} | {v(d['corrected'])} | "
              f"**{v(d['uncorrected'])}** | {v(d['t3_timeouts'])} | {v(d['t4_timeouts'])} | {snr} | {lock or ''} |")
        w("")
        w("*Deltas, not absolutes. A backwards counter is reported as `reset`, never as a rate.*")
        w("")
    w("**Findings**")
    w("")
    for f in a["findings"]:
        w(f"- **{f['hypothesis']}** — confidence {f['confidence']}")
        for e in f["evidence"]:
            w(f"  - evidence: {e}")
        for c in f["contradictions"]:
            w(f"  - contradicts: {c}")
        w(f"  - next: {f['next_step']}")
    w("")
    if a["missing_signals"]:
        w("**Not available**")
        w("")
        for m in a["missing_signals"]:
            w(f"- {m}")
        w("")
    w("**Request to the ISP**")
    w("")
    wd = a["windows"]["during"]
    w(f"- Pull CMTS-side telemetry and events for this CM between "
      f"{wd['start']} and {wd['end']} (UTC), ±1 minute.")
    w("- Upstream RxMER as received by the CMTS is not measurable locally; please compare.")
    w("")
    w("**Method**")
    w("")
    w("- Continuous 1s ICMP from the agent; every reply timestamped on arrival; "
      "gaps in icmp_seq counted as loss. Baseline = this line's own previous 60 min.")
    w("- DOCSIS values read unauthenticated from the modem's local REST API every 60s; "
      "counters compared as deltas.")
    w("- Symptom recorded by keypress at the time, not from recall. Clock NTP-synced.")
    w(f"- Agent docsis-watch {VERSION}. No audio, video or screen content collected.")
    return "\n".join(L) + "\n"


def data_json(a):
    return json.dumps({"version": VERSION, "generated": iso(int(datetime.now(timezone.utc).timestamp() * 1000)),
                       **a}, indent=1, default=str)


def bundle_zip(a, **kw):
    md, js = summary_md(a, **kw), data_json(a)
    allow = tuple(a.get("probes", {}).keys())
    viol = redaction_violations(md, allow) + redaction_violations(js, allow)
    if viol:
        raise ValueError("export blocked — redaction check failed: " + ", ".join(sorted(set(viol))))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("incident-summary.md", md)
        z.writestr("incident-data.json", js)
    return buf.getvalue()
