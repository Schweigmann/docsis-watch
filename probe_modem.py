#!/usr/bin/env python3
"""One-shot, read-only probe of a cable modem: what does it serve without login?

Use it (a) on hardware docsis-watch doesn't support yet, to capture what a
parser would have to read, or (b) cabled straight into a modem/NTU that can't be
reached through the router (the house is offline then, so nothing here needs
the internet).

Read-only GETs only, through the same FORBIDDEN guard as the collector. Bodies
are redacted (MAC, serial, public IPs) before they are written to
data/modem-probe-<time>/ — review them once, then they are safe to attach to an
issue. See docs/ADDING_A_MODEM.md.

    python3 probe_modem.py                      # default: 192.168.100.1
    python3 probe_modem.py 192.168.178.1 192.168.100.1
"""
import json, os, re, socket, subprocess, sys, time
from collectors import modem

HERE = os.path.dirname(os.path.abspath(__file__))
# Discovery paths plus the cablemodem resources the Sagemcom UI bundle calls.
PATHS = sorted({p for ps in modem.DISCOVERY.values() for p in ps} | {
    "/", "/rest/v1/cablemodem/serviceflows", "/rest/v1/cablemodem/downstream/primary_",
    "/rest/v1/cablemodem/upstream/primary_", "/rest/v1/cablemodem/state_",
    "/rest/v1/system/info", "/rest/v1/system/modemmode"})
RE_PUBLIC_IP = re.compile(r"\b(?!(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.)(?:\d{1,3}\.){3}\d{1,3}\b")


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def tcp(host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return "timeout" if isinstance(e, socket.timeout) else type(e).__name__
    finally:
        s.close()


def main(hosts):
    out = os.path.join(HERE, "data", time.strftime("modem-probe-%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    # Which interface has which address: tells us whether the NTU handed out DHCP.
    ifaces = re.sub(r"ether [0-9a-f:]+", "ether <mac>", sh(["ifconfig"]))
    with open(os.path.join(out, "ifconfig.txt"), "w") as f:
        f.write(RE_PUBLIC_IP.sub("<public-ip>", ifaces))
    summary = []
    for h in hosts:
        up = "ping ok" if subprocess.run(["ping", "-c1", "-t2", h], capture_output=True).returncode == 0 else "no ping"
        # ARP answered + ping + no TCP = UI disabled/ACL; no ARP = other segment/VLAN.
        arp = "resolved" if re.search(r"at [0-9a-f]{1,2}:", sh(["arp", "-n", h])) else "unresolved"
        ports = {p: tcp(h, p) for p in (80, 443)}
        print(f"\n== {h}: {up} · ARP {arp} · tcp/80 {ports[80]} · tcp/443 {ports[443]}")
        summary.append({"host": h, "ping": up, "arp": arp, "tcp": ports, "results": []})
        for scheme in ("http", "https"):
            dead = 0
            for p in PATHS:
                url = f"{scheme}://{h}{p}"
                try:
                    status, body = modem._get(url, timeout=3)
                except Exception as e:
                    status = getattr(e, "code", None)
                    body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                    if status is None:
                        dead += 1
                        print(f"  {url:62} {type(e).__name__}")
                        summary[-1]["results"].append({"url": url, "error": type(e).__name__})
                        if dead >= 2:  # host/scheme not answering: stop burning timeouts
                            print(f"  … {scheme} not answering, skipping rest")
                            break
                        continue
                body = RE_PUBLIC_IP.sub("<public-ip>", modem.redact(body))
                # keep a trailing "_": `state` and `state_` are different resources
                name = f"{scheme}_{h}{p.rstrip('/')}".replace("/", "_")
                with open(os.path.join(out, name + ".txt"), "w") as f:
                    f.write(body)
                chans = len(modem.parse_channels(body, "downstream" if "down" in p else "upstream")) \
                    if "stream" in p else ""
                print(f"  {url:62} {status} {len(body)}B" + (f"  channels={chans}" if chans != "" else ""))
                summary[-1]["results"].append({"url": url, "status": status, "bytes": len(body),
                                               "channels": chans})
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(f"\nSaved to {out}\nIf you re-cabled for this, plug the router back in now.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["192.168.100.1"])
