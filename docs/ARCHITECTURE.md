# Architecture

One process, standard library only. Four collector threads write to SQLite; an
HTTP server reads from it. Nothing talks to anything except through the store.

```
 ping ─┐                                   ┌─ GET /api/status ──── live status + sensors (SSE /events)
 modem ─┼─► store.py (SQLite, WAL) ◄─ analysis.py ◄─┼─ GET /api/series ──── charts
 wifi ─┤        ▲                          ├─ GET /api/incidents/N ─ incident report
 host ─┘        └── POST /api/annotations  └─ …/summary.md · data.json · bundle.zip (export.py)
                    ("Something went wrong")
```

## The pieces

| File | Job | Knows about |
|---|---|---|
| `collectors/ping.py` | `ping` subprocess per target, one row per second, explicit loss rows | the OS `ping` output format |
| `collectors/modem.py` | discover read-only endpoints, poll them, normalise, redact | one modem family's JSON |
| `collectors/wifi.py` | signal/noise/rate | `system_profiler` (macOS), `iw` (Linux) |
| `collectors/host.py` | load, memory, default-route interface | the OS |
| `store.py` | schema + queries. Timestamps are integer UTC milliseconds everywhere | nothing else |
| `analysis.py` | everything that is an *interpretation* | the store's normalised rows only |
| `export.py` | Markdown/JSON/ZIP and the redaction backstop | an analysis dict |
| `server.py` | routes; no logic | all of the above |
| `static/` | renders what the API returns; decides nothing | the JSON shapes |

The seam that matters: **collectors normalise, analysis interprets.** A collector
turns vendor output into the store's schema (dBmV, dB, cumulative counters,
UTC ms). `analysis.py` never sees vendor JSON, so supporting a new modem never
touches the analysis, and a new finding never touches a collector.

## The normalised modem schema

This is the contract a modem adapter fulfils (`store.py`):

- `modem_channels` — per poll, per channel: `direction` (downstream/upstream),
  `channel_id`, `channel_type` (sc_qam, ofdm, atdma, ofdma), `frequency` Hz,
  `power` **dBmV**, `snr` **dB**, `modulation`, `lock_status`, `corrected`,
  `uncorrected`, `t3_timeouts`, `t4_timeouts` (cumulative counters).
- `modem_state` — `docsis_version`, `status`, `uptime_s`, `model`, `software`.
- `modem_events` — the modem's own log: `ts_ms` (modem clock), `priority`, `message`. Deduplicated.
- `modem_raw` — redacted response bodies, hourly, so a parser can be written or fixed later.

Any field the hardware does not report is `NULL`. Never 0.

## How an incident is analysed (`analysis.analyse`)

Windows around a mark at time `t` with duration `d` (default 30 s):

```
baseline [t-60min, t-5min)   the line's own normal — not a universal threshold
before   [t-5min,  t-30s)
during   [t-15s,   t+d+15s)
after    [t+d+60s, +2min)
```

1. **Probes → fault domain.** Gateway vs. off-net targets, `during` vs. baseline:
   gateway clean + ≥2 off-net degraded ⇒ *access line*; both degraded ⇒ *local*;
   only the gateway degraded ⇒ *artifact* (routers deprioritise ICMP to
   themselves); no samples ⇒ *gap*, and no conclusion at all.
2. **Modem.** Counter deltas between the last sample `before` and `during`
   (resets and 32-bit wraps told apart by uptime), SNR drops, lock changes, and
   event-log entries within ±45 s.
3. **Wi-Fi and host.** Disassociation, RSSI/rate/noise shifts, CPU saturation.
4. **Overall.** `high` confidence only when two independent layers agree.

`analysis.line_health` is separate: the always-on, plain-language card. It uses
the same deltas and the thresholds in `analysis.LIMITS`.

## Rules that keep reports honest

These are the project's actual design constraints. Each exists because breaking
it produced a wrong report at least once.

1. **Unavailable ≠ zero.** Missing data becomes `None`, is listed under
   *Not available*, and blocks conclusions for that layer.
2. **A collector must not look healthy while blind.** Health is red when the
   data the analysis needs is missing, even if some requests succeed.
3. **"No details" ≠ "disconnected".** Absence of a field is not evidence of a state.
4. **Deltas, not absolutes.** Counters are cumulative; a backwards step is a
   reset or a wrap, never a negative rate.
5. **Identifiers are removed at the edge**, in the collector, not by a filter at
   export time. Export has a second check anyway.
6. **Read-only and unauthenticated**, enforced in the request function.
7. **Demo data can never leave.** `--demo` uses a separate database and refuses exports.

## Running and testing

```bash
python3 docsis_watch.py --demo --port 8788    # UI work without hardware; an episode every few minutes
python3 -m unittest discover -s tests -v
```

Tests are plain `unittest`, no network except a throwaway local HTTP server.
`tests/fixtures/` holds redacted captures from real devices; prefer adding a
fixture over hand-writing a JSON shape.

The server serves `static/` straight from disk, so HTML/JS/CSS changes need only
a browser reload. Python changes need a restart.
