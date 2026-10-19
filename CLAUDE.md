# docsis-watch — domain knowledge for future sessions

Encoded here so nobody has to re-learn it. Verify anything marked ⚠️ against
the actual hardware before trusting it.

## The modem (Ziggo)

- Current Ziggo CPE: **Sagemcom F3896LG-ZG** ("SmartWifi modem", black). DOCSIS **3.1**.
- ⚠️ Other modems (older Ziggo hardware, DOCSIS 4.0 equipment, other ISPs) may
  serve different endpoints or none. Run `discover()` and look at `/api/status →
  modem.tried` before assuming any endpoint below applies.
- Management IP `192.168.100.1` in bridge mode. In router mode, use the
  gateway's LAN IP over HTTPS (see "Verified on a live F3896" below).
- REST base `/rest/v1/`. **Unauthenticated** (verified in ties/sagemcom-f3896-py
  `UNAUTHORIZED_ENDPOINTS`): `cablemodem/downstream`, `cablemodem/upstream`,
  `cablemodem/eventlog`, `cablemodem/registration`. Sources disagree on
  singular vs plural — discovery probes both.
- **Authenticated**: `system/info` (model, firmware). We don't fetch it.
- **One concurrent session.** `POST /rest/v1/user/login` returns a token that
  does *not* expire until `DELETE /rest/v1/user/{id}/token/{token}`. A held
  session locks the owner out of their own modem UI. ⇒ **This app never logs in.**
  `collectors/modem.py::FORBIDDEN` refuses login/token/reboot/reset/set paths at
  the request layer.
- Field names (F3896 firmware, from the client's dataclasses):
  - DS SC-QAM: `channelType channelId frequency power modulation snr correctedErrors uncorrectedErrors lockStatus`
  - DS OFDM: same but `rxMer` instead of `snr`, plus `channelWidth fftType numberOfActiveSubCarriers`
  - US ATDMA: `channelType channelId lockStatus power modulation frequency symbolRate t1Timeout..t4Timeout`
  - US OFDMA: `… channelWidth fftType t3Timeout t4Timeout`
  - Never stored: `macAddress serialNumber bootFilename` (redacted at parse).
- **Verified on a live F3896 (2026-10-02), unauthenticated, behind nothing:**
  - Served on the gateway's LAN address (`https://192.168.178.1`; HTTPS tried first).
  - `downstream` is built **on demand: ~6 s cold, ~0.2 s cached**. At a 60 s
    poll it is usually cold. A 5 s timeout made discovery skip it, leaving the
    chip green while blind to uncorrectables. Now: 20 s timeout for channel
    tables, re-discovery of missing core resources every 10 polls, and the
    modem chip is **red while downstream/upstream is missing**.
    `192.168.100.1` answers ping but not HTTP on this unit.
  - `cablemodem/state` → **401**; `cablemodem/state_` → **200** with
    `docsisVersion status upTime maxCPEs …`. `registration` is two booleans and
    must not be accepted as "state" (no uptime ⇒ no reboot detection).
  - Also 200: `eventlog` (≈32 entries), `serviceflows` (provisioned rates).
  - **Units:** OFDM DS `power` + `rxMer` and OFDMA US `power` are integers in
    **tenths** (OFDMA `405` next to ATDMA `45.3`). SC-QAM/ATDMA are plain.
    `parse_channels` divides by 10 for `channelType` ofdm/ofdma.
  - OFDM `rxMer: 0` on a locked channel = **not reported** → stored as None.
  - OFDM `correctedErrors` reach **billions** (1.4e9, 2.9e9 at 32 days uptime):
    expect 32-bit wraps. `channel_deltas` treats a backwards counter as a wrap
    only when `state_` uptime proves no reboot; otherwise "reset".
  - Fixtures from this capture live in `tests/fixtures/f3896_live/` (redacted).
- **Event log** = the modem's own words with its own timestamps (CMTS-synced
  UTC; matched uptime exactly). Stored deduped in `modem_events`; events within
  ±45s of the `during` window become findings: T3/T4/SYNC/reboot ⇒ high,
  "profile assignment change" ⇒ medium (CMTS moved the modem to a more robust
  profile). The `;CM-MAC=…;CMTS-MAC=…` suffix is stripped at parse.
- Counters are cumulative. Report **deltas**; negative delta = **reset** (reboot).
- Poll ≥60s. The v1 feasibility probe showed no lockout at 3s bursts, but be kind.

## macOS Wi-Fi telemetry

- `airport` is **dead** since 14.4. `wdutil info` needs **sudo** and redacts
  SSID/BSSID/MAC on Sequoia.
- **Primary: CoreWLAN via `osascript -l JavaScript`** (`wifi.COREWLAN_JXA`) —
  stock macOS, no sudo, ~50 ms, reads cached link properties only. No MCS.
  Verified on macOS 26 (2026-10-18). Enums: `activePHYMode` 4=n 5=ac 6=ax 7=be;
  `channelBand` 1=2.4 2=5 3=6 GHz; `channelWidth` 1=20 2=40 3=80 4=160 MHz;
  `interfaceMode` 1=station; `rssiValue` 0 = not associated.
- **`system_profiler SPAirPortDataType` is fallback only — it scans.** Verified
  2026-10-12 in the unified log: each call makes airportd run three *live*
  scans (~0.7 + 3.0 + 4.3 s). At a 15 s poll the radio was off-channel about a
  third of the time, adding 60–120 ms spikes to gateway *and* off-net pings
  every ~24 s and dropping the Wi-Fi real-time latency score during calls. The
  monitor was degrading the calls it measured. Never poll it on a timer.
  Keys are `spairport_*`; we match by substring.
- macOS logs its own per-call Wi-Fi quality: during a real-time session (any
  WebRTC/FaceTime call) airportd logs `channel score: chq, tx-lat, rx-lat,
  tx-loss, rx-loss (5 = best), p95-lat` every 5 s. `log show --predicate
  'process == "airportd" AND eventMessage CONTAINS "channel score"'`. And
  `pmset -g log` records Chrome's "WebRTC has active PeerConnections" assertion,
  which gives call start/end times. Both are useful when the monitor wasn't running.
- ⚠️ On 14.4+/15, SSID/BSSID may be redacted without Location Services. Signal,
  noise, channel, rate should remain. If `rssi_dbm` is None while connected,
  the health row says so.
- SSID/BSSID are **hashed with a per-DB salt** before storage. Never in the clear.

## Vantage point

A laptop on Wi-Fi measures the Wi-Fi. `host.iface_is_wifi` is surfaced on the
dashboard and in the report for exactly that reason. The gateway-vs-off-net
comparison in `analysis.fault_domain` is what separates the two — **always
include the gateway as a ping target** or that comparison is impossible.

## Analysis rules that are easy to get wrong

- `during` window is `[t-15s, t+dur+15s]`; dur defaults to 30s.
- Baseline = the line's **own** previous 60 min, not a universal threshold.
- `degraded()` needs ≥5 samples; loss must exceed max(2%, 3× baseline).
- Gateway degraded + off-net degraded ⇒ **local** (everything crosses the gateway).
- Gateway clean + ≥2 off-net degraded ⇒ **access line**.
- Gateway degraded + off-net clean ⇒ **artifact** (routers deprioritise ICMP to self).
- No probe data in `during` ⇒ `gap`, confidence `none`. Never "nothing happened".
- Confidence `high` only when ≥2 independent layers agree.

## Stack decision

Stdlib Python + vanilla HTML/JS, deliberately. No FastAPI, React, Vite, Docker.
Reason: `python3 docsis_watch.py` must work on a stock Mac with nothing
installed, and this must keep working while the internet is what's broken (so
no CDN). Revisit only when a second machine or a second user exists.

## Testing

Built first in an environment with no `ping`, no modem and no macOS, on fixtures
shaped like the documented responses. The first run on real hardware (2026-10)
broke four of those assumptions, so parsers are now tested on redacted captures
from real devices (`tests/fixtures/`). `--demo` exercises the full UI with
synthetic data and **refuses exports** so synthetic numbers can never leave as a report.
