# First-run test plan — about 30 minutes, in this order

Each step tests one assumption. Stop at the first hard fail and note it; the
order is chosen so the cheapest, most decisive checks come first.

## 0. Before you start (2 min)

- Plug the Mac into **Ethernet** if you possibly can. If not, note it — every
  number you collect includes Wi-Fi noise, and the report will say so.
- System Settings → General → Date & Time → *Set automatically* ON. Timestamps
  that don't match Ziggo's CMTS logs make everything else worthless.
- Open Terminal:
  ```bash
  cd docsis-watch
  python3 --version           # 3.9+ is fine; 3.11+ ideal
  python3 -m unittest discover -s tests   # expect: Ran 40 tests ... OK
  ```
  **Fail:** tests error → something about your Python differs from the build
  environment. Paste the output back to me before going further.

## 1. Demo mode — does the UI work on your machine? (3 min)

```bash
python3 docsis_watch.py --demo --open
```
- Yellow banner, six green health chips, charts fill within ~30s.
- Press **Problem now** → form appears *below the button only after pressing*.
  Save a mark → an incident report opens with findings.
- After ~90s a synthetic episode occurs; mark it and the report should say
  *Access line / DOCSIS impairment — confidence high*.
- Export links must read *exports disabled in demo*.

**Proves:** browser, canvas, SSE, SQLite all work on your macOS. Nothing about
your network yet. Ctrl-C to stop.

## 2. Real mode — the decisive minute (5 min)

```bash
python3 docsis_watch.py --open
```
Watch the health chips top-right. Within about 30 seconds you will know:

| Chip | Green means | Red — hover it, then |
|---|---|---|
| `ping:<gateway>` | ICMP works | firewall/VPN blocking ICMP; check `--targets` |
| `ping:1.1.1.1` `ping:8.8.8.8` | off-net reachable | as above |
| `host` | load/mem/interface read | almost never red |
| `wifi` | `system_profiler` returned signal fields | *"signal fields missing"* → Location Services redaction; *"no Wi-Fi interface"* → you are wired (good) |
| `modem` | **channel tables are being read** (see "Your cable line") | hover it: *no endpoint found* → step 3; *MISSING downstream* → it retries by itself |

Then open `http://127.0.0.1:8787/api/status` and look at `modem.caps` and
`modem.tried`. This is the single most informative view in the app.

## 3. If `modem` is red (10 min) — you are probably not on an F3896

Your modem may be different hardware. Find out what it serves:

```bash
python3 probe_modem.py <your-gateway-ip> 192.168.100.1
```
Then follow `docs/ADDING_A_MODEM.md`.
- Look at the sticker on the modem: model name, and note it.
- Open `http://192.168.100.1/` in Safari → DevTools → Network → reload. Any XHR
  that returns JSON with words like `snr`, `power`, `downstream`, `lock` is the
  endpoint we want. Copy its path (not the token) and send it to me with the
  model name. **Do not log in from a script** — one session, no expiry.
- Meanwhile the app still works on probes + Wi-Fi + host. Layer 1 is just
  `unavailable`, and the report says so honestly.

If `modem` is green: read the "Your cable line" card. Open *All channels* and
check the counts look like your line (roughly 30 download channels, a couple of
OFDM, 4–6 upload) and that power values are single or low double digits — a
value like 405 means a unit bug. Corrected counters should creep up over minutes.

## 4. Let it run, then make a real mark (rest of the day)

- Leave it running (a `screen`/`tmux` session, or just a Terminal tab). Make
  sure the Mac doesn't sleep: System Settings → Displays → Advanced, or
  `caffeinate -i python3 docsis_watch.py`.
- Next time a call stutters: **press the button first, think second.**
  Category and note can follow 30 seconds later; the timestamp cannot.
- Open the report. Read the *Not available* section before the findings — it
  tells you what the analysis could not see.

## 5. Export one report and read it as Ziggo would (5 min)

Click `bundle.zip`. If it refuses, the message names what it found (a MAC in
your note, most likely) — fix the note, retry. Open `incident-summary.md` and ask:
*would a network engineer know what to look up, and when, from this alone?*
If not, tell me what's missing.

## What "pass" looks like after one day

- `modem` chip green **or** you know the model name and one working JSON path
- ≥1 real mark with a report whose *Not available* list you understand
- You know whether your Mac was on Wi-Fi or Ethernet during collection
- One `bundle.zip` that opened and read sensibly

