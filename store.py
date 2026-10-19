"""
SQLite store — one file, WAL mode, explicit tables.

Timestamps are integer UTC milliseconds (`ts_ms`) so range queries are cheap
and every collector shares the same clock. Identifying fields (MAC, serial,
SSID, BSSID) are never written: collectors hash or drop them before insert,
so redaction is by construction rather than by a filter that can be forgotten.
"""
import hashlib, json, os, sqlite3, threading, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS pings (
  ts_ms INTEGER NOT NULL, target TEXT NOT NULL, seq INTEGER,
  rtt_ms REAL, lost INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS pings_ts ON pings(ts_ms);

CREATE TABLE IF NOT EXISTS modem_channels (
  ts_ms INTEGER NOT NULL, direction TEXT NOT NULL, channel_id TEXT NOT NULL,
  channel_type TEXT, frequency REAL, power REAL, snr REAL, modulation TEXT,
  lock_status TEXT, corrected INTEGER, uncorrected INTEGER,
  t3_timeouts INTEGER, t4_timeouts INTEGER);
CREATE INDEX IF NOT EXISTS modem_channels_ts ON modem_channels(ts_ms);

CREATE TABLE IF NOT EXISTS modem_state (
  ts_ms INTEGER NOT NULL, docsis_version TEXT, status TEXT, uptime_s INTEGER,
  model TEXT, software TEXT);

CREATE TABLE IF NOT EXISTS modem_raw (
  ts_ms INTEGER NOT NULL, endpoint TEXT NOT NULL, http_status INTEGER,
  body TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS modem_raw_ts ON modem_raw(ts_ms);

-- The modem's own event log. ts_ms is the MODEM's timestamp for the event;
-- the whole log is re-read every poll, so (ts_ms, message) dedupes it.
CREATE TABLE IF NOT EXISTS modem_events (
  ts_ms INTEGER NOT NULL, priority TEXT, message TEXT NOT NULL,
  first_seen_ms INTEGER NOT NULL, UNIQUE(ts_ms, message));
CREATE INDEX IF NOT EXISTS modem_events_ts ON modem_events(ts_ms);

CREATE TABLE IF NOT EXISTS wifi (
  ts_ms INTEGER NOT NULL, interface TEXT, connected INTEGER,
  rssi_dbm REAL, noise_dbm REAL, channel TEXT, tx_rate_mbps REAL,
  phy_mode TEXT, mcs INTEGER, ssid_hash TEXT, bssid_hash TEXT);
CREATE INDEX IF NOT EXISTS wifi_ts ON wifi(ts_ms);

CREATE TABLE IF NOT EXISTS host (
  ts_ms INTEGER NOT NULL, load1 REAL, cpu_count INTEGER,
  mem_used_pct REAL, default_iface TEXT, iface_is_wifi INTEGER);

CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
  category TEXT NOT NULL, severity INTEGER, duration_s INTEGER,
  call_app TEXT, connection TEXT, note TEXT, created_ms INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS collector_health (
  ts_ms INTEGER NOT NULL, collector TEXT NOT NULL, ok INTEGER NOT NULL,
  message TEXT);
CREATE INDEX IF NOT EXISTS health_ts ON collector_health(ts_ms);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_ms():
    return int(time.time() * 1000)


def hash_id(value, salt):
    """Stable, non-reversible identifier for SSID/BSSID so a report can say
    'same network as before' without ever containing the network name."""
    if not value:
        return None
    return hashlib.sha256((salt + str(value)).encode()).hexdigest()[:12]


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        if not self.get_meta("salt"):
            self.set_meta("salt", os.urandom(16).hex())
        self.salt = self.get_meta("salt")

    # -- generic -------------------------------------------------------------
    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _rows(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def get_meta(self, key):
        r = self._rows("SELECT value FROM meta WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def set_meta(self, key, value):
        self._exec("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    # -- writes --------------------------------------------------------------
    def add_ping(self, target, seq, rtt_ms, lost, ts_ms=None):
        self._exec("INSERT INTO pings VALUES(?,?,?,?,?)",
                   (ts_ms or now_ms(), target, seq, rtt_ms, 1 if lost else 0))

    def add_channels(self, ts_ms, rows):
        with self._lock:
            self._db.executemany(
                "INSERT INTO modem_channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ts_ms, r["direction"], str(r["channel_id"]), r.get("channel_type"),
                  r.get("frequency"), r.get("power"), r.get("snr"), r.get("modulation"),
                  r.get("lock_status"), r.get("corrected"), r.get("uncorrected"),
                  r.get("t3_timeouts"), r.get("t4_timeouts")) for r in rows])
            self._db.commit()

    def add_modem_state(self, ts_ms, **s):
        self._exec("INSERT INTO modem_state VALUES(?,?,?,?,?,?)",
                   (ts_ms, s.get("docsis_version"), s.get("status"), s.get("uptime_s"),
                    s.get("model"), s.get("software")))

    def add_modem_raw(self, ts_ms, endpoint, http_status, body, error):
        self._exec("INSERT INTO modem_raw VALUES(?,?,?,?,?)",
                   (ts_ms, endpoint, http_status, body, error))

    def add_modem_events(self, seen_ms, events):
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO modem_events VALUES(?,?,?,?)",
                [(e["ts_ms"], e.get("priority"), e["message"], seen_ms) for e in events])
            self._db.commit()

    def add_wifi(self, ts_ms, **w):
        self._exec("INSERT INTO wifi VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ts_ms, w.get("interface"),
                    None if w.get("connected") is None else 1 if w.get("connected") else 0,
                    w.get("rssi_dbm"), w.get("noise_dbm"), w.get("channel"),
                    w.get("tx_rate_mbps"), w.get("phy_mode"), w.get("mcs"),
                    hash_id(w.get("ssid"), self.salt), hash_id(w.get("bssid"), self.salt)))

    def add_host(self, ts_ms, **h):
        self._exec("INSERT INTO host VALUES(?,?,?,?,?,?)",
                   (ts_ms, h.get("load1"), h.get("cpu_count"), h.get("mem_used_pct"),
                    h.get("default_iface"), 1 if h.get("iface_is_wifi") else 0))

    def add_health(self, collector, ok, message="", ts_ms=None):
        self._exec("INSERT INTO collector_health VALUES(?,?,?,?)",
                   (ts_ms or now_ms(), collector, 1 if ok else 0, message[:300]))

    def add_annotation(self, ts_ms, category, severity=None, duration_s=None,
                       call_app=None, connection=None, note=None):
        cur = self._exec(
            "INSERT INTO annotations(ts_ms,category,severity,duration_s,call_app,"
            "connection,note,created_ms) VALUES(?,?,?,?,?,?,?,?)",
            (ts_ms, category, severity, duration_s, call_app, connection, note, now_ms()))
        return cur.lastrowid

    # -- reads ---------------------------------------------------------------
    def pings(self, lo_ms, hi_ms, target=None):
        if target:
            return self._rows("SELECT * FROM pings WHERE ts_ms>=? AND ts_ms<? AND target=? "
                              "ORDER BY ts_ms", (lo_ms, hi_ms, target))
        return self._rows("SELECT * FROM pings WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                          (lo_ms, hi_ms))

    def ping_targets(self, since_ms):
        return [r["target"] for r in self._rows(
            "SELECT DISTINCT target FROM pings WHERE ts_ms>=? ORDER BY target", (since_ms,))]

    def channels(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM modem_channels WHERE ts_ms>=? AND ts_ms<? "
                          "ORDER BY ts_ms", (lo_ms, hi_ms))

    def modem_states(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM modem_state WHERE ts_ms>=? AND ts_ms<? "
                          "ORDER BY ts_ms", (lo_ms, hi_ms))

    def modem_events(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM modem_events WHERE ts_ms>=? AND ts_ms<? "
                          "ORDER BY ts_ms", (lo_ms, hi_ms))

    def has_modem_events(self):
        return bool(self._rows("SELECT 1 FROM modem_events LIMIT 1"))

    def wifi_rows(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM wifi WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                          (lo_ms, hi_ms))

    def host_rows(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM host WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                          (lo_ms, hi_ms))

    def health_rows(self, lo_ms, hi_ms):
        return self._rows("SELECT * FROM collector_health WHERE ts_ms>=? AND ts_ms<? "
                          "ORDER BY ts_ms", (lo_ms, hi_ms))

    def latest_health(self):
        return self._rows("SELECT collector, ok, message, MAX(ts_ms) AS ts_ms "
                          "FROM collector_health GROUP BY collector")

    def annotations(self, limit=50):
        return self._rows("SELECT * FROM annotations ORDER BY ts_ms DESC LIMIT ?", (limit,))

    def annotation(self, ann_id):
        r = self._rows("SELECT * FROM annotations WHERE id=?", (ann_id,))
        return r[0] if r else None

    def latest_channels(self):
        r = self._rows("SELECT MAX(ts_ms) AS m FROM modem_channels")
        if not r or r[0]["m"] is None:
            return []
        return self._rows("SELECT * FROM modem_channels WHERE ts_ms=? ORDER BY direction, "
                          "CAST(channel_id AS INTEGER)", (r[0]["m"],))

    def latest_one(self, table):
        r = self._rows(f"SELECT * FROM {table} ORDER BY ts_ms DESC LIMIT 1")
        return r[0] if r else None
