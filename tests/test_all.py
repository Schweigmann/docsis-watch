"""Run:  python3 -m unittest discover -s tests -v   (from app/)"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from store import Store, hash_id
from collectors import modem, wifi, ping
import analysis, export

MIN = 60_000


class ModemParsing(unittest.TestCase):
    F3896_DS = json.dumps({"downstream": {"channels": [
        {"channelType": "sc_qam", "channelId": 14, "frequency": 602000000, "power": -1.2,
         "modulation": "qam256", "snr": 38.4, "correctedErrors": 1200, "uncorrectedErrors": 4, "lockStatus": "Locked"},
        {"channelType": "ofdm", "channelId": 33, "channelWidth": 96, "fftType": "4K", "modulation": "qam4096",
         "rxMer": 410, "power": -20, "correctedErrors": 55, "uncorrectedErrors": 0, "lockStatus": "Locked"}]}})
    F3896_US = json.dumps([
        {"channelType": "atdma", "channelId": 1, "lockStatus": "Locked", "power": 44.5, "modulation": "qam64",
         "frequency": 36800000, "symbolRate": 5120, "t1Timeout": 0, "t2Timeout": 0, "t3Timeout": 2, "t4Timeout": 0},
        {"channelType": "ofdma", "channelId": 9, "lockStatus": "Locked", "power": 410, "t3Timeout": 0, "t4Timeout": 1}])

    def test_downstream_fields(self):
        rows = modem.parse_channels(self.F3896_DS, "downstream")
        self.assertEqual(len(rows), 2)
        qam = next(r for r in rows if r["channel_id"] == 14)
        self.assertEqual((qam["snr"], qam["corrected"], qam["uncorrected"], qam["lock_status"]), (38.4, 1200, 4, "Locked"))
        ofdm = next(r for r in rows if r["channel_id"] == 33)
        self.assertEqual(ofdm["snr"], 41.0, "OFDM rxMer (tenths) must map onto snr in dB")
        self.assertEqual(ofdm["power"], -2.0, "OFDM power is in tenths of a dBmV")
        self.assertEqual(ofdm["channel_type"], "ofdm")

    def test_upstream_timeouts(self):
        rows = modem.parse_channels(self.F3896_US, "upstream")
        self.assertEqual(rows[0]["t3_timeouts"], 2)
        self.assertEqual(rows[1]["t4_timeouts"], 1)
        self.assertIsNone(rows[0]["snr"], "upstream has no SNR: must be None, not 0")
        self.assertEqual((rows[0]["power"], rows[1]["power"]), (44.5, 41.0), "ATDMA plain, OFDMA tenths")

    def test_unknown_shape_yields_nothing_not_zeros(self):
        self.assertEqual(modem.parse_channels('{"status":"ok"}', "downstream"), [])
        self.assertEqual(modem.parse_channels("<html>login</html>", "downstream"), [])

    def test_redaction_strips_identifiers(self):
        body = json.dumps({"macAddress": "AA:BB:CC:DD:EE:FF", "serialNumber": "SN12345678", "snr": 38,
                           "nested": {"cmMacAddress": "11:22:33:44:55:66"}})
        out = modem.redact(body)
        self.assertNotIn("AA:BB", out); self.assertNotIn("SN12345678", out); self.assertNotIn("11:22", out)
        self.assertIn('"snr":38', out)

    def test_forbidden_paths_refused(self):
        for p in ("/rest/v1/user/login", "/rest/v1/system/reboot", "/rest/v1/system/factoryreset",
                  "/rest/v1/user/3/token/x", "/setValue"):
            with self.assertRaises(ValueError):
                modem._get("http://192.168.100.1" + p)

    def test_state(self):
        st = modem.parse_state(json.dumps({"cablemodem": {"docsisVersion": "3.1", "status": "Operational", "upTime": 86400}}))
        self.assertEqual((st["docsis_version"], st["uptime_s"]), ("3.1", 86400))


class WifiParsing(unittest.TestCase):
    SP_JSON = json.dumps({"SPAirPortDataType": [{"spairport_airport_interfaces": [{
        "_name": "en0", "spairport_status_information": "spairport_status_connected",
        "spairport_current_network_information": {
            "_name": "MyHomeNet", "spairport_network_bssid": "aa:bb:cc:dd:ee:ff",
            "spairport_network_channel": "44 (5GHz, 80MHz)", "spairport_network_phymode": "802.11ax",
            "spairport_network_rate": 866, "spairport_network_mcs": 9,
            "spairport_signal_noise": "-58 dBm / -92 dBm"}}]}]})
    SP_TEXT = """Wi-Fi:
      Interfaces:
        en0:
          Current Network Information:
            MyHomeNet:
              PHY Mode: 802.11ax
              Channel: 44 (5GHz, 80MHz)
              Signal / Noise: -61 dBm / -90 dBm
              Transmit Rate: 780
              MCS Index: 8
"""

    def test_json_form(self):
        w = wifi.parse_system_profiler_json(self.SP_JSON)
        self.assertEqual((w["rssi_dbm"], w["noise_dbm"], w["tx_rate_mbps"], w["mcs"]), (-58.0, -92.0, 866.0, 9))
        self.assertEqual(w["ssid"], "MyHomeNet"); self.assertTrue(w["connected"])

    def test_status_connected_without_details_is_not_disconnected(self):
        # what system_profiler returned for three days on 2026-09-29: link up, no details
        text = json.dumps({"SPAirPortDataType": [{"spairport_airport_interfaces": [
            {"_name": "en0", "spairport_status_information": "spairport_status_connected"},
            {"_name": "awdl0"}]}]})
        w = wifi.parse_system_profiler_json(text)
        self.assertIs(w["connected"], True)
        self.assertIsNone(w.get("rssi_dbm"))
        off = text.replace("spairport_status_connected", "spairport_status_disconnected")
        self.assertIs(wifi.parse_system_profiler_json(off)["connected"], False)

    def test_text_form(self):
        w = wifi.parse_system_profiler_text(self.SP_TEXT)
        self.assertEqual((w["rssi_dbm"], w["noise_dbm"], w["tx_rate_mbps"]), (-61.0, -90.0, 780.0))

    def test_redacted_json_degrades(self):
        j = json.loads(self.SP_JSON)
        cur = j["SPAirPortDataType"][0]["spairport_airport_interfaces"][0]["spairport_current_network_information"]
        del cur["spairport_signal_noise"]
        w = wifi.parse_system_profiler_json(json.dumps(j))
        self.assertTrue(w["connected"]); self.assertIsNone(w["rssi_dbm"])

    def test_ssid_never_stored_in_clear(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.sqlite"))
            s.add_wifi(1000, ssid="MyHomeNet", bssid="aa:bb:cc:dd:ee:ff", connected=True, rssi_dbm=-58)
            row = s.latest_one("wifi")
            self.assertNotIn("MyHomeNet", json.dumps(row)); self.assertNotIn("aa:bb", json.dumps(row))
            self.assertEqual(row["ssid_hash"], hash_id("MyHomeNet", s.salt))

    def test_corewlan(self):
        # Shape captured from macOS 26 (osascript CoreWLAN), Location access denied.
        w = wifi.parse_corewlan_json('{"interface":"en0","power":true,"rssi":-41,"noise":-97,"tx":2161,'
                                     '"phy":6,"mode":1,"channel":108,"band":2,"width":4,"ssid":null,"bssid":null}')
        self.assertEqual((w["rssi_dbm"], w["noise_dbm"], w["tx_rate_mbps"]), (-41.0, -97.0, 2161.0))
        self.assertEqual((w["channel"], w["phy_mode"], w["mcs"]), ("108 (5GHz, 160MHz)", "802.11ax", None))

    def test_corewlan_not_associated(self):
        w = wifi.parse_corewlan_json('{"interface":"en0","power":true,"rssi":0,"noise":0,"tx":0,'
                                     '"phy":0,"mode":0,"channel":null,"band":null,"width":null}')
        self.assertIs(w["connected"], False)
        self.assertIsNone(wifi.parse_corewlan_json("null"))

    def test_iw(self):
        w = wifi.parse_iw_link("Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: X\n\tfreq: 5220\n\tsignal: -55 dBm\n\ttx bitrate: 866.7 MBit/s")
        self.assertEqual((w["rssi_dbm"], w["tx_rate_mbps"]), (-55.0, 866.7))


class PingParsing(unittest.TestCase):
    def test_formats(self):
        for line, want in (("64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=11.234 ms", (0, 11.234)),
                           ("64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=11.2 ms", (1, 11.2)),
                           ("64 bytes from 8.8.8.8: icmp_seq=7 ttl=110 time=1284 ms", (7, 1284.0))):
            m = ping.RE_REPLY.search(line); self.assertEqual((int(m.group(1)), float(m.group(2))), want)
        for line in ("Request timeout for icmp_seq 12", "PING 1.1.1.1 (1.1.1.1): 56 data bytes",
                     "round-trip min/avg/max/stddev = 10.1/12.4/20.7/3.0 ms"):
            self.assertIsNone(ping.RE_REPLY.search(line))


def _seed(s, t, gw_bad=False, ext_bad=True, docsis=True, wifi_bad=False, gap=False):
    """60 min of 1s pings for gw + 2 ext, with a 6s episode at t."""
    import random
    r = random.Random(1)
    for tgt, base, is_gw in (("192.168.178.1", 2.0, True), ("1.1.1.1", 11.0, False), ("8.8.8.8", 13.0, False)):
        seq = 0
        for s_ in range(-3600, 300):
            ts = t + s_ * 1000
            if gap and -60 <= s_ <= 60:
                continue
            seq += 1
            hit = 0 <= s_ < 6 and ((is_gw and gw_bad) or (not is_gw and ext_bad))
            if hit and r.random() < 0.4:
                s.add_ping(tgt, seq, None, True, ts); continue
            s.add_ping(tgt, seq, round(base + r.gauss(0, .4) + (r.uniform(80, 250) if hit else 0), 3), False, ts)
    if docsis:
        for k in range(-10, 4):
            ts = t + k * 60_000 + 20_000
            past = ts > t
            s.add_channels(ts, [{"direction": "downstream", "channel_id": 33, "channel_type": "ofdm",
                                 "snr": 41.0 - (5 if past else 0), "power": -2.0, "lock_status": "Locked",
                                 "corrected": 1000 + k * 3, "uncorrected": 1300 if past else 2},
                                {"direction": "downstream", "channel_id": 14, "channel_type": "sc_qam",
                                 "snr": 38.0, "power": -1.0, "lock_status": "Locked", "corrected": 50, "uncorrected": 0},
                                {"direction": "upstream", "channel_id": 1, "channel_type": "atdma",
                                 "power": 44.0, "lock_status": "Locked", "t3_timeouts": 0, "t4_timeouts": 0}])
    for k in range(-240, 20):
        ts = t + k * 15_000
        during = 0 <= k * 15 < 20
        s.add_wifi(ts, interface="en0", connected=not (wifi_bad and during),
                   rssi_dbm=-58 - (20 if wifi_bad and during else 0), noise_dbm=-92, tx_rate_mbps=866)
    for k in range(-360, 30):
        s.add_host(t + k * 10_000, load1=1.5, cpu_count=8, mem_used_pct=60, default_iface="en0", iface_is_wifi=True)


class Analysis(unittest.TestCase):
    def _run(self, **kw):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        s = Store(os.path.join(d.name, "t.sqlite"))
        t = 1_800_000_000_000
        _seed(s, t, **kw)
        ann_id = s.add_annotation(t + 2000, "video_freeze", 4, 6, "teams_desktop", "wifi", "froze")
        return analysis.analyse(s, s.annotation(ann_id)), s

    def test_access_line_with_docsis_is_high(self):
        a, _ = self._run()
        self.assertEqual(a["fault_domain"]["code"], "access_line")
        self.assertEqual(a["overall"]["confidence"], "high")
        self.assertIn("docsis", a["overall"]["layers_agreeing"])
        unc = [d for d in a["channel_deltas"] if d["channel_id"] == "33"][0]
        self.assertEqual(unc["uncorrected"], 1298)

    def test_access_line_without_docsis_is_medium(self):
        a, _ = self._run(docsis=False)
        self.assertEqual(a["fault_domain"]["code"], "access_line")
        self.assertEqual(a["overall"]["confidence"], "medium")
        self.assertTrue(any("DOCSIS" in m for m in a["missing_signals"]))

    def test_gateway_degraded_is_local(self):
        a, _ = self._run(gw_bad=True, docsis=False)
        self.assertEqual(a["fault_domain"]["code"], "lan_wifi_gateway")
        self.assertIn("Local", a["overall"]["hypothesis"])

    def test_wifi_disconnect_found(self):
        a, _ = self._run(wifi_bad=True, gw_bad=True, docsis=False)
        self.assertTrue(any("Wi-Fi disconnected" in f["hypothesis"] for f in a["findings"]))
        self.assertEqual(a["overall"]["confidence"], "high")

    def test_quiet_is_not_corroborated(self):
        a, _ = self._run(ext_bad=False, docsis=False)
        self.assertEqual(a["fault_domain"]["code"], "clean")
        self.assertEqual(a["overall"]["confidence"], "low")

    def test_gap_blocks_conclusion(self):
        a, _ = self._run(gap=True, docsis=False)
        self.assertEqual(a["fault_domain"]["code"], "gap")
        self.assertEqual(a["overall"]["confidence"], "none")

    def test_counter_reset_reported_not_negative(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        s = Store(os.path.join(d.name, "t.sqlite")); t = 1_800_000_000_000
        s.add_channels(t - 120_000, [{"direction": "downstream", "channel_id": 1, "corrected": 50000, "uncorrected": 900}])
        s.add_channels(t + 5_000, [{"direction": "downstream", "channel_id": 1, "corrected": 12, "uncorrected": 0}])
        deltas = analysis.channel_deltas(s.channels(t - 300_000, t - 30_000), s.channels(t - 15_000, t + 60_000))
        self.assertEqual((deltas[0]["corrected"], deltas[0]["uncorrected"]), ("reset", "reset"))


class Export(unittest.TestCase):
    def test_bundle_and_redaction(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        s = Store(os.path.join(d.name, "t.sqlite")); t = 1_800_000_000_000
        _seed(s, t)
        ann_id = s.add_annotation(t + 2000, "video_freeze", 4, 6, "teams_desktop", "wifi", "froze")
        a = analysis.analyse(s, s.annotation(ann_id))
        md = export.summary_md(a)
        self.assertIn("Most likely:", md); self.assertIn("Deltas, not absolutes", md)
        z = export.bundle_zip(a)
        self.assertTrue(z.startswith(b"PK"))
        # a MAC in the user's free-text note must block the export
        bad = s.add_annotation(t + 2000, "other", None, None, None, None, "modem AA:BB:CC:DD:EE:FF acting up")
        with self.assertRaises(ValueError):
            export.bundle_zip(analysis.analyse(s, s.annotation(bad)))

    def test_violation_patterns(self):
        self.assertIn("MAC address", export.redaction_violations("x aa:bb:cc:dd:ee:ff y"))
        self.assertIn("public IPv4 address", export.redaction_violations("from 84.12.3.4"))
        self.assertEqual(export.redaction_violations("gw 192.168.178.1 and 10.0.0.1 and 1.1.1.1 loss 0%"),
                         ["public IPv4 address"], "an unlisted public IP is a leak")
        self.assertEqual(export.redaction_violations("gw 192.168.178.1 and 1.1.1.1 loss 0%", allow_ips=("1.1.1.1",)),
                         [], "configured probe targets are allowed")
        self.assertEqual(export.redaction_violations("target 1.1.1.1 but also 84.12.3.4", allow_ips=("1.1.1.1",)),
                         ["public IPv4 address"], "the subscriber's own public IP must still be caught")
        self.assertIn("credential/token", export.redaction_violations("password: hunter2"))


FIX = os.path.join(os.path.dirname(__file__), "fixtures", "f3896_live")


def _fix(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


class LiveF3896(unittest.TestCase):
    """Redacted bodies captured unauthenticated from a live F3896 over
    HTTPS on 2026-10-02 — the real shapes, not guessed ones."""

    def test_downstream(self):
        rows = modem.parse_channels(_fix("downstream.json"), "downstream")
        self.assertEqual(len(rows), 34)
        ofdm = [r for r in rows if r["channel_type"] == "ofdm"]
        self.assertEqual(len(ofdm), 2)
        self.assertEqual(sorted(r["power"] for r in ofdm), [-0.3, 2.7], "OFDM power is tenths of a dBmV")
        self.assertTrue(all(r["snr"] is None for r in ofdm), "rxMer 0 means not reported, not 0 dB")
        qam = next(r for r in rows if r["channel_id"] == 1)
        self.assertEqual((qam["power"], qam["snr"], qam["uncorrected"]), (5.5, 40, 0))

    def test_upstream(self):
        rows = modem.parse_channels(_fix("upstream.json"), "upstream")
        self.assertEqual(len(rows), 6)
        ofdma = [r for r in rows if r["channel_type"] == "ofdma"]
        self.assertTrue(all(30 < r["power"] < 60 for r in rows), [r["power"] for r in rows])
        self.assertEqual(len(ofdma), 2)
        self.assertEqual({r["channel_id"]: r["t3_timeouts"] for r in rows}[4], 1, "real line: one T3 on US ch 4")

    def test_state_underscore_has_uptime(self):
        st = modem.parse_state(_fix("state_.json"))
        self.assertEqual((st["docsis_version"], st["status"], st["uptime_s"]), ("3.1", "operational", 2785628))
        reg = modem.parse_state('{"registration":{"registrationComplete":true,"downstreamLocked":true}}')
        self.assertFalse(any(v is not None for v in reg.values()), "registration must not pass as state")

    def test_eventlog(self):
        ev = modem.parse_events(_fix("eventlog.json"))
        self.assertEqual(len(ev), 32)
        self.assertTrue(all("CM-MAC" not in e["message"] and "xx:xx" not in e["message"] for e in ev))
        self.assertTrue(any("profile assignment change" in e["message"] for e in ev))
        self.assertEqual(analysis.iso(ev[0]["ts_ms"]), "2026-09-07T12:55:22Z")


class Discovery(unittest.TestCase):
    """A fake gateway serving the live fixtures with the F3896's real status
    codes: `state` 401, `registration` two booleans, `state_` the real object."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        routes = {"/rest/v1/cablemodem/downstream": (200, _fix("downstream.json")),
                  "/rest/v1/cablemodem/upstream": (200, _fix("upstream.json")),
                  "/rest/v1/cablemodem/eventlog": (200, _fix("eventlog.json")),
                  "/rest/v1/cablemodem/state": (401, '{"status":401,"message":"Unauthorized"}'),
                  "/rest/v1/cablemodem/state_": (200, _fix("state_.json")),
                  "/rest/v1/cablemodem/registration":
                      (200, '{"registration":{"registrationComplete":true,"downstreamLocked":true}}')}
        self.routes = routes
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = routes.get(self.path, (404, '{"status":404}'))
                self.send_response(code); self.end_headers(); self.wfile.write(body.encode())
            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)   # cleanups run LIFO: shutdown first
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def test_finds_all_resources_and_prefers_state_underscore(self):
        dead = "http://127.0.0.1:1"   # refused: must be skipped after two failures, not 10
        caps, tried = modem.discover([dead, self.url])
        self.assertEqual(set(caps), {"downstream", "upstream", "eventlog", "state"})
        self.assertTrue(caps["state"].endswith("/state_"))
        self.assertEqual(sum(1 for u, *_ in tried if u.startswith(dead)), 2)

    def test_missing_downstream_is_not_green_and_is_rediscovered(self):
        import threading
        ds = self.routes.pop("/rest/v1/cablemodem/downstream")
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        s = Store(os.path.join(d.name, "t.sqlite")); stop, caps = threading.Event(), {}
        old = modem.REDISCOVER_EVERY; modem.REDISCOVER_EVERY = 2
        self.addCleanup(lambda: setattr(modem, "REDISCOVER_EVERY", old))
        th = threading.Thread(target=modem.run, args=(s, [self.url], 0.05, stop, caps), daemon=True)
        th.start()
        import time
        time.sleep(0.4)
        first = s.health_rows(0, 2**62)
        self.assertTrue(first and not first[0]["ok"] and "MISSING downstream" in first[0]["message"])
        self.routes["/rest/v1/cablemodem/downstream"] = ds   # the modem answers again
        time.sleep(0.6); stop.set(); th.join(2)
        self.assertIn("downstream", caps["caps"])
        self.assertTrue(s.health_rows(0, 2**62)[-1]["ok"])
        self.assertEqual(len(s.latest_channels()), 40)

    def test_router_without_cablemodem_handlers_finds_nothing(self):
        caps, tried = modem.discover([self.url + "/nope"])   # every path 404, like an unsupported router
        self.assertEqual(caps, {})
        self.assertTrue(all(code == 404 for _, code, _ in tried))


class CountersAndEvents(unittest.TestCase):
    def _store(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        return Store(os.path.join(d.name, "t.sqlite"))

    def test_32bit_wrap_without_reboot_is_a_delta(self):
        a = [{"ts_ms": 0, "direction": "downstream", "channel_id": "33", "corrected": 4_294_000_000, "uncorrected": 18}]
        b = [{"ts_ms": 60_000, "direction": "downstream", "channel_id": "33", "corrected": 1_000, "uncorrected": 18}]
        d = analysis.channel_deltas(a, b, rebooted=False)[0]
        self.assertEqual(d["corrected"], 1_000 + 2**32 - 4_294_000_000)
        self.assertEqual(analysis.channel_deltas(a, b, rebooted=True)[0]["corrected"], "reset")
        self.assertEqual(analysis.channel_deltas(a, b)[0]["corrected"], "reset", "unknown uptime: stay conservative")

    def test_events_dedupe(self):
        s = self._store()
        ev = modem.parse_events(_fix("eventlog.json"))
        s.add_modem_events(1, ev); s.add_modem_events(2, ev)
        # the live log repeats one line within the same second; that collapses to one row
        self.assertEqual(len(s.modem_events(0, 2**62)), len({(e["ts_ms"], e["message"]) for e in ev}))
        self.assertEqual(len(s.modem_events(0, 2**62)), 31)

    def test_fault_event_in_window_corroborates_access_line(self):
        s = self._store(); t = 1_800_000_000_000
        _seed(s, t, docsis=False)
        s.add_modem_events(t, [{"ts_ms": t + 3000, "priority": "critical",
                                "message": "No Ranging Response received - T3 time-out"},
                               {"ts_ms": t - 3 * 3600_000, "priority": "notice", "message": "old, outside window"}])
        a = analysis.analyse(s, s.annotation(s.add_annotation(t + 2000, "call_dropped", 4, 6, None, None, None)))
        f = next(f for f in a["findings"] if "fault events" in f["hypothesis"])
        self.assertIn("T3 time-out", f["evidence"][0])
        self.assertEqual(len(a["modem_events"]), 1)
        self.assertEqual(a["overall"]["confidence"], "high", "probes + modem log = two layers")
        self.assertFalse(any("event log" in m for m in a["missing_signals"]))

    def test_profile_change_is_medium_and_no_events_is_missing(self):
        s = self._store(); t = 1_800_000_000_000
        _seed(s, t, docsis=False, ext_bad=False)
        ann = s.annotation(s.add_annotation(t + 2000, "video_freeze", 3, 6, None, None, None))
        self.assertTrue(any("event log" in m for m in analysis.analyse(s, ann)["missing_signals"]))
        s.add_modem_events(t, [{"ts_ms": t + 1000, "priority": "notice",
                                "message": "US profile assignment change. US Chan ID: 26; Previous Profile: 10 13; New Profile: 9 13."}])
        f = next(f for f in analysis.analyse(s, ann)["findings"] if "profile" in f["hypothesis"])
        self.assertEqual(f["confidence"], "medium")



class LineHealth(unittest.TestCase):
    def _store(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        return Store(os.path.join(d.name, "t.sqlite"))

    def test_live_line_reads_healthy_after_two_readings(self):
        s = self._store(); t = 1_800_000_000_000
        ds = modem.parse_channels(_fix("downstream.json"), "downstream")
        us = modem.parse_channels(_fix("upstream.json"), "upstream")
        s.add_channels(t - 60_000, ds + us); s.add_channels(t, ds + us)
        h = analysis.line_health(s, t)
        self.assertEqual(h["verdict"], "good", h["items"])
        lost = next(i for i in h["items"] if i["key"] == "lost")
        self.assertTrue(lost["value"].startswith("0"))
        self.assertIn("40 of 40", next(i for i in h["items"] if i["key"] == "lock")["value"])

    def test_first_reading_waits_instead_of_claiming_zero(self):
        s = self._store(); t = 1_800_000_000_000
        s.add_channels(t, modem.parse_channels(_fix("downstream.json"), "downstream"))
        lost = next(i for i in analysis.line_health(s, t)["items"] if i["key"] == "lost")
        self.assertEqual(lost["status"], "unknown")

    def test_bad_values_and_errors_are_named_in_the_headline(self):
        s = self._store(); t = 1_800_000_000_000
        row = {"direction": "downstream", "channel_id": 1, "channel_type": "sc_qam", "power": 12.5,
               "snr": 31.0, "lock_status": True, "corrected": 10, "uncorrected": 5}
        s.add_channels(t - 60_000, [row]); s.add_channels(t, [{**row, "uncorrected": 2500}])
        h = analysis.line_health(s, t)
        self.assertEqual(h["verdict"], "bad")
        for words in ("signal strength", "signal quality", "lost data"):
            self.assertIn(words, h["headline"])
        self.assertEqual(analysis.grade(-8.5, "ds_power"), "watch")

    def test_a_handful_of_lost_codewords_is_not_an_alarm(self):
        s = self._store(); t = 1_800_000_000_000
        row = {"direction": "downstream", "channel_id": 34, "channel_type": "ofdm", "power": 0.1,
               "lock_status": True, "corrected": 10, "uncorrected": 215}
        s.add_channels(t - 60_000, [row]); s.add_channels(t, [{**row, "uncorrected": 217}])
        lost = next(i for i in analysis.line_health(s, t)["items"] if i["key"] == "lost")
        self.assertEqual((lost["status"], lost["value"][:1]), ("good", "2"))

    def test_events_in_plain_words(self):
        ev = [analysis.plain_event(e) for e in modem.parse_events(_fix("eventlog.json"))]
        words = {e["plain"] for e in ev}
        self.assertIn("Upload request went unanswered (T3)", words)
        self.assertIn("Modem restarted — reason: HW or Power-On Reset", words)
        self.assertIn("Lost the download signal", words)


if __name__ == "__main__":
    unittest.main()
