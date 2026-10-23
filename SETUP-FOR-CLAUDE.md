# Setting up docsis-watch — instructions for Claude Code

The person asked you to set this up on their computer. Do these steps in order,
tell them in one line what you are doing at each step, and stop at the first
hard failure. Most people have this running in about ten minutes.

Read `CLAUDE.md` first if you need background: it has the modem quirks and analysis rules.

## Rules

- **No sudo, no pip, no Homebrew.** It is Python standard library only. If
  something seems to need an install, stop and tell the person.
- **Never log in to the modem**, and never send a request to it other than what
  the app and `probe_modem.py` already do. These modems allow one admin session;
  holding it locks the owner out of their own modem.
- **Nothing leaves the computer.** Don't upload the database, logs or probe
  output anywhere. The only thing meant to be shared is an incident export (step 7).
- Keep the person's data in `data/` (git-ignored).

## 1. Check the machine

```bash
uname -s && sw_vers -productVersion 2>/dev/null    # macOS (verified) or Linux (Wi-Fi untested)
python3 --version                                   # needs 3.9+
python3 -m unittest discover -s tests               # all tests must pass
```

- On a fresh Mac, `python3` may pop up an "install command line developer tools"
  dialog. Tell the person to click *Install* and wait, then re-run.
- Windows is not supported. Say so and stop.

## 2. Is the laptop on Wi-Fi or a cable?

```bash
route -n get default 2>/dev/null | grep -E 'gateway|interface' || ip route show default
```

Note the gateway IP (often `192.168.178.1` or `192.168.0.1`) and the
interface. Tell the person: on Wi-Fi, every number includes their Wi-Fi; the
report separates "my house" from "the line" by comparing the gateway with the
internet, so that still works, but a cable gives cleaner evidence.

## 3. Can we read the modem?

```bash
python3 probe_modem.py <gateway-ip> 192.168.100.1
```

Read-only and redacted; writes to `data/modem-probe-<time>/`. Then compare with
the *Supported modems* table in `README.md`:

- `cablemodem/downstream` and `cablemodem/upstream` answered 200 → full support.
- Nothing answered (a modem model that isn't supported yet, or one the router
  doesn't expose) → **that's fine, carry on.** Pings, Wi-Fi and host data still
  give a verdict; the report will say DOCSIS was unavailable. Mention
  `docs/ADDING_A_MODEM.md` if they want to help add their modem.

Ask the person which modem they have (model sticker on the bottom) and
note it for them.

## 4. Start it, and keep it running

First check where the folder is. If it is under `~/Downloads`, `~/Desktop` or
`~/Documents` (or iCloud Drive), move it to the home folder and continue from
there: macOS privacy protection stops a login item from reading those folders,
and the monitor then fails silently with "Operation not permitted" in
`data/docsis-watch.log`.

```bash
python3 autostart.py install
```

This starts it now and at every login, and restarts it if it crashes (macOS:
LaunchAgent; Linux: systemd user unit). It keeps a Mac awake while plugged in,
but lets it sleep on battery. **Closing the lid stops measuring.**

If port 8787 is taken: `python3 autostart.py install --port 8788`.

## 5. Verify it really collects

Wait about 90 seconds, then:

```bash
curl -s http://127.0.0.1:8787/api/status | python3 -c "import json,sys; [print(h['ok'], h['collector'], h['message']) for h in json.load(sys.stdin)['health']]"
```

- `ping:<target>` (one per target), `host` and `wifi` must be 1 (ok). `modem` is ok only if step 3 found it.
- `wifi` not ok on macOS with "signal fields missing" → still usable; say so.
- Then open the dashboard for them: `open http://127.0.0.1:8787` (macOS) or
  `xdg-open` (Linux).

## 6. Tell the person how to use it (keep it short)

1. **When a call stutters or drops: press "Something went wrong just now" immediately**, then fill
   in what happened. The exact second matters more than anything else.
2. Leave the laptop plugged in with the lid open during calls you care about.
3. Click a report under "Your reports" to see what was found. *Outside your home* means
   their provider's side; *Inside your home* means their own Wi-Fi/router; *Nothing we measure changed* means
   nothing measured moved.
4. `python3 autostart.py status` checks it is running;
   `python3 autostart.py uninstall` removes it (data stays).

## 7. Later: reviewing an incident

When they come back with "I had a drop at <time>":

1. Times are local; the database stores UTC milliseconds. Query read-only:
   `sqlite3 "file:data/docsis-watch.sqlite?mode=ro"`. Tables: `pings`,
   `modem_channels`, `modem_state`, `modem_events`, `wifi`, `host`,
   `annotations`, `collector_health`.
2. First check the monitor was running then (gaps in `pings`). A reboot or a
   closed lid means no data. Say so instead of "nothing happened".
3. Look at loss/latency per minute per target: gateway vs. 1.1.1.1/8.8.8.8 is
   the house-vs-line split. Then modem channels/events and Wi-Fi around the time.
4. macOS keeps its own evidence for a few days, useful even if the monitor was off:
   - Wi-Fi quality per call, every 5 s (5 = best):
     `log show --start "<date time>" --end "<date time>" --style compact --predicate 'process == "airportd" AND eventMessage CONTAINS "channel score"'`
     (use `/usr/bin/log` in zsh)
   - Call start/end from Chrome/Edge (Teams on the web):
     `pmset -g log | grep "WebRTC has active PeerConnections"`
5. To share with their provider: the incident's export (`summary.md`,
   `bundle.zip`) from the report page. It is redaction-checked. Never share the raw database.
