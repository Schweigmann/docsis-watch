# Adding support for a modem

docsis-watch reads one modem family today (Sagemcom's `/rest/v1/cablemodem/*`
API, as shipped by Ziggo). Every other modem needs someone who owns one. This
is the path from "unknown hardware" to "supported", and the plan for how
detection scales once there are several.

## 1. Find out what your modem serves (10 minutes, no code)

```bash
python3 docsis_watch.py --open
```

If the `modem` chip is green and "Your cable line" shows channels, you are done.
If it is red, open `http://127.0.0.1:8787/api/status` and read `modem.tried`:
every URL probed and what came back.

Then capture what the modem *does* serve:

```bash
python3 probe_modem.py <gateway-ip> 192.168.100.1
```

It prints a line per URL and saves redacted response bodies to
`data/modem-probe-<time>/`. Reading the first line per host:

| Ping | ARP | TCP 80/443 | Meaning |
|---|---|---|---|
| ok | resolved | open | There is a web interface. Look at which paths returned `200`. |
| ok | resolved | timeout/refused | Device is there, its UI is off or filtered. |
| ok | unresolved | timeout | It is behind a router that doesn't pass the traffic back. |
| no | — | — | Wrong address, or not reachable from this network segment. |

If nothing useful returned `200`, open the modem's own status page in a browser,
open DevTools → Network, reload, and note which requests return signal data
(look for `power`, `snr`, `downstream`, `lock`). **Copy the path, not any token.**
Those paths are what an adapter would request.

**Rule: only pages that work without logging in.** Many modems allow a single
admin session; a monitor holding it locks the owner out. If your modem only
shows signal data after login, say so in the issue — that needs the opt-in
design in section 4, not a quick adapter.

## 2. Share the capture

Open an issue with: modem make/model, ISP and country, router or bridge mode,
and the files from `data/modem-probe-<time>/`.

They are redacted (MAC addresses, serial numbers, public IPs), but **read them
before attaching**: an unknown firmware can put an identifier in a field name
the redactor has never seen. `summary.json` alone is enough to start a conversation.

## 3. Write the parser

Today all modem knowledge is in `collectors/modem.py`:

| Piece | What to change |
|---|---|
| `DISCOVERY` | add your read-only paths per kind (`downstream`, `upstream`, `state`, `eventlog`) |
| `parse_channels` | map your field names and **units** onto the normalised schema |
| `parse_state` | DOCSIS version, status, uptime |
| `parse_events` | the modem's log, if it has one |
| `RE_DROP_KEYS` | any new field that holds an identifier |

The target is the schema in `ARCHITECTURE.md`: power in dBmV, SNR/MER in dB,
cumulative counters, `None` for anything not reported. Nothing downstream of the
collector needs to change.

Then:

1. Copy your redacted capture to `tests/fixtures/<vendor>_<model>/`.
2. Add a test class beside `LiveF3896` in `tests/test_all.py` asserting channel
   counts, one known value per channel type, and that nothing unreported became 0.
3. Run it for a day. Check that counters *move* and that units are plausible.
4. Add a row to "Supported modems" in the README and what you learned to `CLAUDE.md`.

### What goes wrong (all of these happened on the first real modem)

- **Units differ by channel type.** OFDM/OFDMA values came in tenths of a dB next to plain-dB SC-QAM values.
- **0 can mean "not reported".** A locked channel with MER 0 is not a 0 dB channel.
- **Slow endpoints.** A table built on demand took 6 s cold, 0.2 s cached, and timed out intermittently.
- **Lookalike resources.** `state` needed login; `state_` did not; `registration` answered but had no uptime.
- **Unstable ordering.** The event log reordered same-second entries between polls, defeating a naive change check.
- **Counters wrap.** 32-bit counters in the billions go backwards without a reboot.

## 4. How this scales to many modems (planned, not built)

With one family, a tolerant parser plus path discovery is enough. With five it
becomes a pile of `c.get("a", c.get("b", …))`. The step to take when the
**second** family arrives — not before — is an adapter per family:

```
collectors/modems/
  __init__.py          registry + detect()
  sagemcom_rest.py     today's code, moved
  arris_surfboard.py   HTML tables at /cmconnectionstatus.html
  generic.py           fallback: capture redacted bodies, parse nothing
```

```python
class Adapter:
    name = "sagemcom-rest"
    auth = "none"                       # "none" | "required"
    def detect(self, probe) -> float    # 0..1, from cheap unauthenticated GETs
    def endpoints(self) -> dict         # kind -> read-only path
    def parse(self, kind, body) -> rows # normalised schema, units converted
```

**Detection** is fingerprinting, cheapest signal first, all local and read-only:

1. Where: the default gateway, then `192.168.100.1` (bridge mode).
2. A known path answering with a known shape (`/rest/v1/…` JSON, a Surfboard
   status table, a FRITZ!Box login page). This is today's `discover()`.
3. `Server` header and page title.
4. Each adapter scores the probe; highest confidence wins; ties and low scores
   fall to `generic`.

Detection must be explainable: `modem.tried` already lists every URL and
result, and should gain "matched adapter X because Y".

**The generic adapter is the growth loop.** An unsupported modem should not be a
dead end: the app keeps working on probes/Wi-Fi/host, says "DOCSIS unavailable"
honestly, and offers the redacted capture as a ready-to-attach bundle. Every
unsupported user becomes a potential fixture contributor.

**Login-only modems** (many Compal/Arris/Netgear/AVM devices) are the real
ceiling. They need an explicit opt-in: adapter declares `auth = "required"`,
the user supplies credentials locally, the adapter logs in and **out** on every
poll, and the UI warns about single-session lockout. Until that exists, such
modems stay on the generic adapter.

**Compatibility is proven by fixtures**, not claims: the README table is only
updated together with a fixture from real hardware and a passing test.

Things deliberately out of scope: SNMP (closed on customer-side interfaces of
most ISP firmware), anything that writes to the modem, and anything that needs
a cloud account.
