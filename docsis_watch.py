#!/usr/bin/env python3
"""
docsis-watch — local DOCSIS + Wi-Fi monitor with a "Problem now" button.

    python3 docsis_watch.py            # real collectors, http://127.0.0.1:8787
    python3 docsis_watch.py --demo     # synthetic data, exports disabled
    python3 docsis_watch.py --open     # also open the browser (macOS)

Stdlib only. Read-only towards the modem; never authenticates.
"""
import argparse, os, platform, random, signal, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import Store, now_ms
from collectors import ping, modem, wifi, host
import server

HERE = os.path.dirname(os.path.abspath(__file__))


def demo_collectors(store, stop, targets):
    """Plausible synthetic data with an access-line episode every few minutes,
    so the dashboard and the analysis can be exercised without hardware.
    Never used for exports; the server refuses them in demo mode."""
    rnd = random.Random(42)
    start = time.time()
    base = {targets[0]: 2.0, targets[1]: 11.0, targets[2]: 13.5}
    episode_until, next_episode = 0, time.time() + 90
    seq = {t: 0 for t in targets}
    counters = {c: [1000 + c * 10, 2] for c in range(1, 9)}   # corrected, uncorrected
    last_modem = last_wifi = last_host = 0
    while not stop.is_set():
        now = time.time()
        if now >= next_episode and now > episode_until:
            episode_until, next_episode = now + rnd.uniform(4, 9), now + rnd.uniform(150, 300)
            store.add_modem_events(now_ms(), [{"ts_ms": now_ms(), "priority": "critical",
                                               "message": "No Ranging Response received - T3 time-out"}])
        in_ep = now < episode_until
        for t in targets:
            seq[t] += 1
            is_gw = t == targets[0]
            if in_ep and not is_gw and rnd.random() < 0.4:
                store.add_ping(t, seq[t], None, True)
            else:
                rtt = base[t] + rnd.gauss(0, 0.4 if is_gw else 1.2) + (rnd.uniform(60, 250) if in_ep and not is_gw else 0)
                store.add_ping(t, seq[t], round(max(0.3, rtt), 3), False)
        if now - last_modem >= 30:
            last_modem = now
            ts = now_ms()
            rows = []
            for c, (cor, unc) in counters.items():
                cor += rnd.randint(0, 3)
                if in_ep and c in (1, 2):
                    unc += rnd.randint(100, 400)
                counters[c] = [cor, unc]
                rows.append({"direction": "downstream", "channel_id": c,
                             "channel_type": "ofdm" if c <= 2 else "sc_qam",
                             "frequency": 100e6 + c * 8e6, "power": round(rnd.gauss(-1.5, 0.3), 1),
                             "snr": round(38 - (6 if in_ep and c <= 2 else 0) + rnd.gauss(0, 0.3), 1),
                             "modulation": "qam4096" if c <= 2 else "qam256", "lock_status": "Locked",
                             "corrected": cor, "uncorrected": unc})
            for c in (1, 2, 3, 4):
                rows.append({"direction": "upstream", "channel_id": c, "channel_type": "atdma",
                             "frequency": 30e6 + c * 6.4e6, "power": round(rnd.gauss(44, 0.5), 1),
                             "modulation": "qam64", "lock_status": "Locked",
                             "t3_timeouts": 0, "t4_timeouts": 0})
            store.add_channels(ts, rows)
            store.add_modem_state(ts, docsis_version="3.1", status="Operational",
                                  uptime_s=int(86400 * 3 + (now - start)), model="DEMO-F3896", software="demo")
            store.add_health("modem", True, "DEMO synthetic")
        if now - last_wifi >= 15:
            last_wifi = now
            store.add_wifi(now_ms(), interface="en0", connected=True, rssi_dbm=round(rnd.gauss(-58, 2)),
                           noise_dbm=-92, channel="44 (5 GHz, 80 MHz)", tx_rate_mbps=rnd.choice([866, 780, 702]),
                           phy_mode="802.11ax", mcs=9, ssid="demo", bssid="00:00:00:00:00:00")
            store.add_health("wifi", True, "DEMO synthetic")
        if now - last_host >= 10:
            last_host = now
            store.add_host(now_ms(), load1=round(rnd.uniform(1, 3), 2), cpu_count=8,
                           mem_used_pct=round(rnd.uniform(55, 70), 1), default_iface="en0", iface_is_wifi=True)
            store.add_health("host", True, "DEMO synthetic")
            for t in targets:
                store.add_health(f"ping:{t}", True, "DEMO synthetic")
        stop.wait(1)


def main():
    ap = argparse.ArgumentParser(description="Local DOCSIS + Wi-Fi monitor with incident marking.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=None, help="SQLite path (default data/docsis-watch.sqlite)")
    ap.add_argument("--targets", nargs="*", default=None,
                    help="Ping targets. Default: gateway, 1.1.1.1, 8.8.8.8")
    ap.add_argument("--modem-hosts", nargs="*", default=None,
                    help="Where to look for the modem. Default: http(s)://192.168.100.1 and the gateway")
    ap.add_argument("--modem-interval", type=int, default=60)
    ap.add_argument("--wifi-interval", type=int, default=15)
    ap.add_argument("--host-interval", type=int, default=10)
    ap.add_argument("--demo", action="store_true", help="Synthetic data; exports disabled")
    ap.add_argument("--open", action="store_true", help="Open the dashboard in the browser")
    a = ap.parse_args()

    db = a.db or os.path.join(HERE, "data", "docsis-watch-demo.sqlite" if a.demo else "docsis-watch.sqlite")
    store = Store(db)
    stop = threading.Event()
    caps_holder = {}

    if a.demo:
        targets = ["192.168.178.1", "1.1.1.1", "8.8.8.8"]
        threads = [threading.Thread(target=demo_collectors, args=(store, stop, targets), daemon=True)]
    else:
        gw = ping.default_gateway()
        targets = a.targets or [t for t in (gw, "1.1.1.1", "8.8.8.8") if t]
        targets = list(dict.fromkeys(targets))
        # An all-in-one cable gateway serves the DOCSIS resources on its LAN
        # address over HTTPS (plain HTTP times out on the larger bodies), so
        # try that first; a bridged modem answers on 192.168.100.1.
        gw_hosts = [f"https://{gw}", f"http://{gw}"] if gw and gw != "192.168.100.1" else []
        modem_hosts = a.modem_hosts or gw_hosts[:1] + ["http://192.168.100.1", "https://192.168.100.1"] + gw_hosts[1:]
        threads = [threading.Thread(target=ping.run, args=(store, t, stop), daemon=True) for t in targets]
        threads.append(threading.Thread(target=modem.run,
                                        args=(store, modem_hosts, a.modem_interval, stop, caps_holder), daemon=True))
        threads.append(threading.Thread(target=wifi.run, args=(store, a.wifi_interval, stop), daemon=True))
        threads.append(threading.Thread(target=host.run, args=(store, a.host_interval, stop), daemon=True))

    ctx = server.Context(store, a.demo, caps_holder, now_ms(), targets)
    ctx.stop = stop
    httpd = server.serve(ctx, a.host, a.port)
    for t in threads:
        t.start()

    url = f"http://{a.host}:{a.port}/"
    print(f"docsis-watch {server.export.VERSION} {'[DEMO — synthetic data, exports disabled]' if a.demo else ''}")
    print(f"dashboard: {url}")
    print(f"database:  {db}")
    print(f"targets:   {', '.join(targets)}")
    print("Ctrl-C to stop.")
    if a.open and platform.system() == "Darwin":
        subprocess.Popen(["open", url])

    def bye(*_):
        stop.set()
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    while not stop.is_set():
        time.sleep(0.5)
    httpd.shutdown()
    print("\nstopped.")


if __name__ == "__main__":
    main()
