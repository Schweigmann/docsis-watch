"""
Continuous 1s ICMP per target. Every reply is timestamped on arrival; gaps in
icmp_seq become explicit loss rows, so a 4-second dropout stays visible.
(Regex and parsing logic carried over from the first CLI spike — tested on macOS
and Linux ping output.)
"""
import re, subprocess, sys, time
from store import now_ms

RE_REPLY = re.compile(r"icmp_seq[= ](\d+).*?time[= ]([\d.]+)\s*ms", re.I)
RE_FAIL = re.compile(r"unreachable|timeout|100% packet loss|no route", re.I)


def default_gateway():
    for cmd, pat in ((["route", "-n", "get", "default"], r"gateway:\s*(\S+)"),
                     (["ip", "route"], r"^default via (\S+)"),
                     (["netstat", "-rn"], r"^(?:default|0\.0\.0\.0)\s+(\S+)")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        m = re.search(pat, out, re.M)
        if m:
            return m.group(1)
    return None


def run(store, target, stop):
    name = f"ping:{target}"
    while not stop.is_set():
        try:
            proc = subprocess.Popen(["ping", "-i", "1", target], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            store.add_health(name, False, "no 'ping' binary on PATH")
            return
        except Exception as e:
            store.add_health(name, False, f"could not start ping: {e}")
            stop.wait(10)
            continue
        store.add_health(name, True, "running")
        last_seq = None
        try:
            for line in proc.stdout:
                if stop.is_set():
                    break
                m = RE_REPLY.search(line)
                if m:
                    seq, rtt = int(m.group(1)), float(m.group(2))
                    if last_seq is not None and seq > last_seq + 1:
                        for missing in range(last_seq + 1, seq):
                            store.add_ping(target, missing, None, True)
                    last_seq = seq
                    store.add_ping(target, seq, rtt, False)
                elif RE_FAIL.search(line):
                    store.add_ping(target, None, None, True)
        except Exception as e:
            store.add_health(name, False, f"reader stopped: {e}")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
        if not stop.is_set():
            store.add_health(name, False, "ping exited; restarting in 5s")
            stop.wait(5)
