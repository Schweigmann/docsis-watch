"""
Local HTTP server: dashboard, JSON API, SSE live feed, incident exports.
Stdlib only. Binds to 127.0.0.1 by default — nothing here is meant for a LAN.
"""
import json, mimetypes, os, statistics, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import analysis, export
from store import now_ms

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Context:
    """Shared between handler instances (the server makes one per request)."""
    def __init__(self, store, demo, caps_holder, started_ms, targets):
        self.store, self.demo, self.caps, self.started_ms, self.targets = \
            store, demo, caps_holder, started_ms, targets
        self.stop = threading.Event()


def status(ctx, include_tried=True):
    st = ctx.store
    latest_ch = st.latest_channels()
    modem = {"caps": ctx.caps.get("caps", {}), "discovered": ctx.caps.get("discovered_ms"),
             "channels": latest_ch, "state": st.latest_one("modem_state"),
             "health": analysis.line_health(st, now_ms())}
    if include_tried:
        modem["tried"] = ctx.caps.get("tried", [])
    return {"demo": ctx.demo, "uptime_s": (now_ms() - ctx.started_ms) // 1000,
            "now_ms": now_ms(), "health": st.latest_health(), "modem": modem,
            "wifi": st.latest_one("wifi"), "host": st.latest_one("host"),
            "targets": ctx.targets,
            "gateway": next((t for t in ctx.targets if analysis.is_private(t)), None)}


def series(ctx, minutes):
    st = ctx.store
    hi, lo = now_ms(), now_ms() - minutes * 60_000
    bucket = 5_000 if minutes <= 30 else 30_000
    pings = {}
    for r in st.pings(lo, hi):
        b = (r["ts_ms"] // bucket) * bucket
        d = pings.setdefault(r["target"], {}).setdefault(b, {"rtts": [], "lost": 0, "n": 0})
        d["n"] += 1
        if r["lost"]:
            d["lost"] += 1
        elif r["rtt_ms"] is not None:
            d["rtts"].append(r["rtt_ms"])
    out_p = {}
    for tgt, buckets in pings.items():
        out_p[tgt] = [{"t": b, "rtt": round(statistics.median(d["rtts"]), 1) if d["rtts"] else None,
                       "max": round(max(d["rtts"]), 1) if d["rtts"] else None,
                       "lost": d["lost"], "n": d["n"]} for b, d in sorted(buckets.items())]
    wifi = [{"t": r["ts_ms"], "rssi": r["rssi_dbm"], "noise": r["noise_dbm"],
             "rate": r["tx_rate_mbps"], "connected": r["connected"]} for r in st.wifi_rows(lo, hi)]
    # Uncorrectable delta per modem sample, summed over downstream channels.
    per_ts = {}
    for r in st.channels(lo - 5 * 60_000, hi):
        if r["direction"] == "downstream" and r["uncorrected"] is not None:
            per_ts.setdefault(r["ts_ms"], {})[r["channel_id"]] = r["uncorrected"]
    modem, prev = [], None
    for ts in sorted(per_ts):
        cur = per_ts[ts]
        if prev is not None:
            common = set(cur) & set(prev)
            diffs = [cur[c] - prev[c] for c in common]
            reset = any(x < 0 for x in diffs)
            if ts >= lo:
                modem.append({"t": ts, "uncorr_delta": None if reset else sum(diffs), "reset": reset})
        prev = cur
    host = [{"t": r["ts_ms"], "load1": r["load1"], "cpus": r["cpu_count"]} for r in st.host_rows(lo, hi)]
    anns = [a for a in st.annotations(200) if a["ts_ms"] >= lo]
    return {"from_ms": lo, "to_ms": hi, "bucket_ms": bucket, "pings": out_p, "wifi": wifi,
            "modem": modem, "host": host, "annotations": anns}


class Handler(BaseHTTPRequestHandler):
    ctx: Context = None
    server_version = "docsis-watch/" + export.VERSION

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- helpers -------------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        try:
            if p == "/":
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/status":
                return self._send(200, status(self.ctx))
            if p == "/api/series":
                return self._send(200, series(self.ctx, max(1, min(int(q.get("minutes", 15)), 720))))
            if p == "/api/annotations":
                return self._send(200, self.ctx.store.annotations(100))
            if p.startswith("/api/incidents/"):
                return self._incident(p[len("/api/incidents/"):], q)
            if p == "/events":
                return self._sse()
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if self.path != "/api/annotations":
            return self._send(404, {"error": "not found"})
        try:
            b = self._json_body()
            ts = int(b.get("ts_ms") or (now_ms() - int(b.get("ago_s") or 0) * 1000))
            cat = str(b.get("category") or "other")[:40]
            sev = b.get("severity"); dur = b.get("duration_s")
            ann_id = self.ctx.store.add_annotation(
                ts, cat, int(sev) if sev not in (None, "") else None,
                int(dur) if dur not in (None, "") else None,
                (b.get("call_app") or None), (b.get("connection") or None),
                (str(b.get("note") or "")[:500] or None))
            return self._send(201, self.ctx.store.annotation(ann_id))
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, name):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            return self._send(200, f.read(), ctype)

    def _incident(self, rest, q):
        parts = rest.split("/")
        try:
            ann = self.ctx.store.annotation(int(parts[0]))
        except ValueError:
            ann = None
        if not ann:
            return self._send(404, {"error": "no such annotation"})
        a = analysis.analyse(self.ctx.store, ann)
        if len(parts) == 1:
            return self._send(200, a)
        if self.ctx.demo:
            return self._send(403, {"error": "exports are disabled in demo mode: "
                                             "synthetic data must never leave as a report"})
        kw = {"cm_mac_hint": q.get("cm", "<fill in from modem label>"),
              "trial_id": q.get("trial", "<fill in>")}
        if parts[1] == "summary.md":
            return self._send(200, export.summary_md(a, **kw), "text/markdown; charset=utf-8")
        if parts[1] == "data.json":
            return self._send(200, export.data_json(a))
        if parts[1] == "bundle.zip":
            try:
                z = export.bundle_zip(a, **kw)
            except ValueError as e:
                return self._send(422, {"error": str(e)})
            return self._send(200, z, "application/zip",
                              {"Content-Disposition": f'attachment; filename="incident-{ann["id"]}.zip"'})
        return self._send(404, {"error": "unknown export"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while not self.ctx.stop.is_set():
                payload = json.dumps({"type": "tick", "status": status(self.ctx, include_tried=False)},
                                     default=str)
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                self.ctx.stop.wait(2)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(ctx, host, port):
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
