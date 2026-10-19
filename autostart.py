"""
Start docsis-watch automatically at login, and restart it if it dies.

    python3 autostart.py install     # start now and at every login
    python3 autostart.py status
    python3 autostart.py uninstall   # stop it and remove the login item

macOS: a per-user LaunchAgent (no sudo). The app runs under `caffeinate -s`,
which keeps the Mac awake while it is on mains power but lets it sleep on
battery. Closing the lid still sleeps the Mac; nothing is measured then.
Linux: a systemd --user unit.

Without this, a reboot silently stops collection and the next incident has no
data (that happened on 2026-10-15: the monitor died with a reboot and the
2026-10-16 call drop could not be analysed).
"""
import os, platform, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = "local.docsis-watch"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
UNIT = os.path.expanduser("~/.config/systemd/user/docsis-watch.service")
LOG = os.path.join(HERE, "data", "docsis-watch.log")


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plist(extra_args):
    args = ["/usr/bin/caffeinate", "-s", sys.executable, os.path.join(HERE, "docsis_watch.py")] + extra_args
    items = "\n".join(f"    <string>{_xml(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key><string>{_xml(HERE)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{_xml(LOG)}</string>
  <key>StandardErrorPath</key><string>{_xml(LOG)}</string>
</dict>
</plist>
"""


def unit(extra_args):
    cmd = " ".join([sys.executable, os.path.join(HERE, "docsis_watch.py")] + extra_args)
    return f"""[Unit]
Description=docsis-watch line monitor

[Service]
WorkingDirectory={HERE}
ExecStart={cmd}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def install(extra_args):
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    if platform.system() == "Darwin":
        sh("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w") as f:
            f.write(plist(extra_args))
        r = sh("launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST)
        if r.returncode:
            sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
        print(f"Installed {PLIST}\nRunning now and at every login. Log: {LOG}")
    elif platform.system() == "Linux":
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        with open(UNIT, "w") as f:
            f.write(unit(extra_args))
        sh("systemctl", "--user", "daemon-reload")
        r = sh("systemctl", "--user", "enable", "--now", "docsis-watch.service")
        if r.returncode:
            sys.exit(f"systemctl failed: {r.stderr.strip()}")
        print(f"Installed {UNIT}\nRunning now and at every login.")
    else:
        sys.exit(f"unsupported platform: {platform.system()}")
    print("Dashboard: http://127.0.0.1:8787")


def uninstall():
    if platform.system() == "Darwin":
        sh("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        if os.path.exists(PLIST):
            os.remove(PLIST)
    elif platform.system() == "Linux":
        sh("systemctl", "--user", "disable", "--now", "docsis-watch.service")
        if os.path.exists(UNIT):
            os.remove(UNIT)
    print("Removed. Collected data stays in data/.")


def status():
    if platform.system() == "Darwin":
        r = sh("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
        if r.returncode:
            print("not installed")
            return
        state = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith(("state =", "pid =", "last exit code"))]
        print("\n".join(state) or "installed")
    elif platform.system() == "Linux":
        print(sh("systemctl", "--user", "status", "docsis-watch.service").stdout)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall", "status"):
        sys.exit(__doc__)
    {"install": lambda: install(sys.argv[2:]), "uninstall": uninstall, "status": status}[sys.argv[1]]()
