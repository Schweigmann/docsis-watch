"""
Read-only DOCSIS modem adapter with capability discovery.

Primary target: Sagemcom F3896LG-ZG (Ziggo "SmartWifi"). Its
/rest/v1/cablemodem/* resources are served WITHOUT authentication, and the
device allows exactly ONE authenticated session — so this adapter never logs
in. A held session would lock the owner out of their own modem UI, and the
data we want does not need one.

Verified field names (ties/sagemcom-f3896-py models):
  downstream SC-QAM: channelType, channelId, frequency, power, modulation, snr,
                     rxMer, correctedErrors, uncorrectedErrors, lockStatus
  downstream OFDM:   + channelWidth, fftType, numberOfActiveSubCarriers
  upstream ATDMA:    channelType, channelId, lockStatus, power, modulation,
                     frequency, symbolRate, t1Timeout..t4Timeout
  upstream OFDMA:    + channelWidth, fftType, t3Timeout, t4Timeout

Units, verified against a live F3896 on 2026-10-02 and the same client:
SC-QAM/ATDMA power and snr are plain dBmV / dB, but OFDM power + rxMer and
OFDMA power are integers in TENTHS (OFDMA 405 next to ATDMA 45.3). A locked
OFDM channel reporting rxMer 0 means "not reported", not 0 dB.

Behind a Ziggo router (gateway on 192.168.178.1) the REST paths answer over
HTTPS; over plain HTTP the larger bodies time out. `state` needs login (401);
`state_` serves the same object unauthenticated.

The path is documented as both singular and plural by different sources, so
discovery probes both. Anything not found is `unsupported`, never zero.

Other modems (e.g. DOCSIS 4.0 hardware) may be DIFFERENT. Discovery will tell you;
`generic` mode then captures raw bodies so a parser can be written from them.
"""
import hashlib, json, re, ssl, subprocess, urllib.error, urllib.request
from datetime import datetime
from store import now_ms

# Endpoints that are safe: GET-only, read-only, no session. Nothing else is ever
# requested, so reboot/reset/setValue cannot happen by construction.
DISCOVERY = {
    "downstream": ["/rest/v1/cablemodem/downstream", "/rest/v1/cablemodem/downstreams"],
    "upstream":   ["/rest/v1/cablemodem/upstream", "/rest/v1/cablemodem/upstreams"],
    "state":      ["/rest/v1/cablemodem/state_", "/rest/v1/cablemodem/state",
                   "/rest/v1/cablemodem/registration"],
    "eventlog":   ["/rest/v1/cablemodem/eventlog"],
    "generic":    ["/getRouterStatus", "/cmconnectionstatus.html", "/cgi-bin/status_cgi"],
}
FORBIDDEN = re.compile(r"login|token|reboot|reset|factory|password|softwareupdate|"
                       r"firstinstall|set", re.I)

RE_MAC = re.compile(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
RE_DROP_KEYS = re.compile(r"macAddress|serialNumber|bootFilename", re.I)

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _get(url, timeout=8):
    if FORBIDDEN.search(url):
        raise ValueError(f"refusing non-read-only path: {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.status, r.read(600_000).decode("utf-8", "replace")


def redact(body):
    """Strip identifiers before anything is stored or shown."""
    body = RE_MAC.sub("xx:xx:xx:xx:xx:xx", body)
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body
    def scrub(o):
        if isinstance(o, dict):
            return {k: ("[redacted]" if RE_DROP_KEYS.search(k) else scrub(v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [scrub(v) for v in o]
        return o
    return json.dumps(scrub(obj), separators=(",", ":"))


def find_channel_list(obj):
    """Locate the list of channel dicts wherever the firmware nests it."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and \
            any(k in obj[0] for k in ("channelId", "channel_id", "id")):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = find_channel_list(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = find_channel_list(v)
            if found:
                return found
    return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"-?\d+(\.\d+)?", str(v))
    return float(m.group()) if m else None


TENTHS_TYPES = ("ofdm", "ofdma")


def parse_channels(body, direction):
    """Normalise one firmware's channel objects into our schema.
    Missing fields stay None — the store and analysis treat None as unsupported."""
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    lst = find_channel_list(obj) or []
    out = []
    for c in lst:
        if not isinstance(c, dict):
            continue
        cid = c.get("channelId", c.get("channel_id", c.get("id")))
        if cid is None:
            continue
        ctype = c.get("channelType", c.get("type"))
        scale = 10 if str(ctype).lower() in TENTHS_TYPES else 1
        power = _num(c.get("power", c.get("powerLevel")))
        # SC-QAM reports snr; OFDM reports rxMer. Same physical meaning here.
        snr = _num(c.get("snr", c.get("rxMer", c.get("signalToNoise"))))
        out.append({
            "direction": direction,
            "channel_id": cid,
            "channel_type": ctype,
            "frequency": _num(c.get("frequency")),
            "power": round(power / scale, 1) if power is not None else None,
            # 0 dB (or less) on a channel that carries data is "not reported".
            "snr": round(snr / scale, 1) if snr is not None and snr > 0 else None,
            "modulation": c.get("modulation"),
            "lock_status": c.get("lockStatus", c.get("lock")),
            "corrected": _int(c.get("correctedErrors", c.get("corrected"))),
            "uncorrected": _int(c.get("uncorrectedErrors", c.get("uncorrectableErrors",
                                                                c.get("uncorrected")))),
            "t3_timeouts": _int(c.get("t3Timeout", c.get("t3Timeouts"))),
            "t4_timeouts": _int(c.get("t4Timeout", c.get("t4Timeouts"))),
        })
    return out


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def parse_state(body):
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    flat = {}
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    flat.setdefault(k, v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return {
        "docsis_version": flat.get("docsisVersion"),
        "status": flat.get("status", flat.get("operational")),
        "uptime_s": _int(flat.get("upTime", flat.get("uptime"))),
        "model": flat.get("modelName"),
        "software": flat.get("softwareVersion"),
    }


RE_EVENT_IDS = re.compile(r";?\s*(CM-MAC|CMTS-MAC|CM-QOS|CM-VER)=[^;]*", re.I)


def _event_ms(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v * 1000 if v < 1e12 else v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def parse_events(body):
    """Event log → [{ts_ms, priority, message}]. Timestamps are the modem's own
    clock (UTC, set from the CMTS). The CM-MAC/CMTS-MAC suffix is dropped: it
    is redacted anyway and only adds noise to a report."""
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    def find(o):
        if isinstance(o, list) and o and isinstance(o[0], dict) and "message" in o[0]:
            return o
        for v in (o.values() if isinstance(o, dict) else o if isinstance(o, list) else []):
            r = find(v)
            if r:
                return r
        return None
    out = []
    for e in find(obj) or []:
        ts = _event_ms(e.get("time", e.get("timestamp", e.get("date"))))
        msg = RE_EVENT_IDS.sub("", str(e.get("message", ""))).strip(" ;")
        if ts is not None and msg:
            out.append({"ts_ms": ts, "priority": e.get("priority"), "message": msg[:400]})
    return out


def discover(hosts):
    """Find which read-only resources this modem serves. Returns a capability
    map: {kind: url} plus a list of everything tried, for the report."""
    caps, tried = {}, []
    for host in hosts:
        dead = 0
        for kind, paths in DISCOVERY.items():
            if kind in caps or dead >= 2:
                continue
            for p in paths:
                url = host.rstrip("/") + p
                try:
                    status, body = _get(url, timeout=TIMEOUT.get(kind, 5))
                except urllib.error.HTTPError as e:
                    tried.append((url, e.code, "HTTPError"))
                    e.close()
                    continue
                except Exception as e:
                    tried.append((url, None, type(e).__name__))
                    dead += 1
                    if dead >= 2:  # host/scheme not answering: don't burn 5s per path
                        break
                    continue
                tried.append((url, status, f"{len(body)}B"))
                if status == 200 and len(body) > 40 and "<html" not in body[:200].lower():
                    if kind in ("downstream", "upstream") and not parse_channels(body, kind):
                        continue
                    if kind == "state" and not any(v is not None for v in parse_state(body).values()):
                        continue  # e.g. `registration`: two booleans, no version/uptime
                    caps[kind] = url
                    break
        if caps:
            break  # first host that answers is the modem
    return caps, tried


RAW_EVERY_MS = 3600_000
# The F3896 builds the downstream table on demand: ~6s on a cold request,
# ~0.2s while cached. At a 60s poll interval it is usually cold.
TIMEOUT = {"downstream": 20, "upstream": 20}
CORE = ("downstream", "upstream")
REDISCOVER_EVERY = 10   # polls; retry resources discovery missed (e.g. a timeout)


def run(store, hosts, interval, stop, caps_holder):
    """Poll discovered resources every `interval` seconds. Re-discover on
    persistent failure (firmware update, mode change, different modem)."""
    caps, tried = discover(hosts)
    caps_holder.update({"caps": caps, "tried": tried, "discovered_ms": now_ms()})
    if not caps:
        store.add_health("modem", False, "no read-only DOCSIS endpoint found "
                         f"({len(tried)} probed) — Layer 1 unavailable")
    fails, last_raw, polls = 0, {}, 0
    while not stop.is_set():
        if not caps:
            stop.wait(min(interval * 4, 600))
            caps, tried = discover(hosts)
            caps_holder.update({"caps": caps, "tried": tried, "discovered_ms": now_ms()})
            continue
        polls += 1
        if polls % REDISCOVER_EVERY == 0 and any(k not in caps for k in CORE):
            host = re.match(r"https?://[^/]+", next(iter(caps.values()))).group()
            more, tried = discover([host])
            caps = {**more, **caps}
            caps_holder.update({"caps": caps, "tried": tried, "discovered_ms": now_ms()})
        ts = now_ms()
        ok_any = False
        for kind, url in caps.items():
            try:
                status, body = _get(url, timeout=TIMEOUT.get(kind, 8))
                body = redact(body)
                # Raw bodies are for writing parsers later, not a time series:
                # keep one per hour per resource, and the event log whenever it changes.
                # The modem reorders same-second events between polls, so compare
                # the event *set*, not the body bytes.
                events = parse_events(body) if kind == "eventlog" else None
                h = hashlib.sha1((json.dumps(sorted((e["ts_ms"], e["message"]) for e in events))
                                  if events is not None else body).encode()).hexdigest()
                prev = last_raw.get(kind)
                if prev is None or ts - prev[1] >= RAW_EVERY_MS or (kind == "eventlog" and h != prev[0]):
                    store.add_modem_raw(ts, url, status, body[:20000] if kind == "eventlog" else body, None)
                    last_raw[kind] = (h, ts)
                if kind in ("downstream", "upstream"):
                    rows = parse_channels(body, kind)
                    if rows:
                        store.add_channels(ts, rows)
                elif kind == "eventlog":
                    store.add_modem_events(ts, events)
                elif kind == "state":
                    st = parse_state(body)
                    if any(v is not None for v in st.values()):
                        store.add_modem_state(ts, **st)
                ok_any = True
            except Exception as e:
                store.add_modem_raw(ts, url, None, None, f"{type(e).__name__}: {e}")
        if ok_any:
            fails = 0
            gaps = [k for k in CORE if k not in caps]
            # Green only with channel data: a modem chip that is green while the
            # downstream table is missing hides the layer the analysis needs most.
            store.add_health("modem", not gaps, f"polling {len(caps)} resource(s) every "
                             f"{interval}s, unauthenticated" + (f"; MISSING {', '.join(gaps)} "
                             f"(timed out or not served; retrying)" if gaps else ""))
        else:
            fails += 1
            store.add_health("modem", False, f"all polls failed ({fails}x)")
            if fails >= 3:
                caps = {}
        stop.wait(interval)
