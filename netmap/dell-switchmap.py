#!/usr/bin/env python3
"""Build a port map of the Dell PowerConnect 2816 switch as a static HTML page.

The 2816 has no LLDP and no usable API, so this logs into its web UI, reads
  - the dynamic MAC address table   (which MAC was seen on which port),
  - each port's link state, speed, duplex and description,
  - each port's RMON received-bytes counter (rate = difference between runs),
then names each MAC from hibou's own view of the LAN (ARP table after a ping
sweep, reverse DNS via pi-hole, IEEE vendor list) and writes switchmap.html.

Usage:  dell-switchmap.py [--out /home/www/switchmap.html]
Config: password in ~/.config/netmap/dell-2816.pass (mode 600, one line).
State:  ~/.cache/netmap/dell-2816-counters.json (previous counters, for rates).
Runs from cron as the user who owns the password file.
"""

import argparse
import html
import http.cookiejar
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

SWITCH = "192.168.1.250"
SWITCH_NAME = "Dell PowerConnect 2816"
NPORTS = 16
LAN = ipaddress.ip_network("192.168.1.0/24")
IFACE = "eno1"
PASSFILE = os.path.expanduser("~/.config/netmap/dell-2816.pass")
STATEFILE = os.path.expanduser("~/.cache/netmap/dell-2816-counters.json")
OUI_FILES = ["/usr/share/ieee-data/oui.txt", "/var/lib/ieee-data/oui.txt"]
GROUP_MIN_MACS = 5  # ports with this many MACs list their devices behind a click

DUPLEX = {"1": "half", "2": "full"}


# ---------------------------------------------------------------- switch I/O
class Switch:
    def __init__(self, host, password):
        self.base = f"http://{host}"
        jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        data = urllib.parse.urlencode({
            "restoreUrl": "", "errorCollector": "",
            "userName$query": "admin", "password$query": password,
        }).encode()
        r = self.op.open(f"{self.base}/config/log_off_page.htm", data, timeout=15)
        if "accessnotallowed" in r.geturl() or "log_off_page" in r.geturl():
            raise SystemExit(f"login to {host} refused (check {PASSFILE})")

    def get(self, path):
        # The UI's query strings contain [] () ! & = which must reach the switch verbatim.
        url = self.base + urllib.parse.quote(path, safe="/?=&:[]()!<>'\"|@,.")
        with self.op.open(url, timeout=20) as r:
            return r.read().decode("latin1")

    def logout(self):
        try:
            self.get("/config/log_out.htm")
        except Exception:
            pass


def field(page, name):
    """Value of <INPUT NAME=name$query ... value="..."> in a switch page."""
    m = re.search(r'name=' + re.escape(name) + r'\$query\b[^>]*value="([^"]*)"', page, re.I)
    return m.group(1) if m else None


def read_switch(sw):
    page = sw.get("/config/address_dynamic.htm?Filter:((dot1qTpFdbStatus!=1)&&(dot1qTpFdbStatus!=5))"
                  "&&((ifOperStatus!=6)||(dot1qTpFdbPort>24))")
    macs = {}
    for idx, mac in re.findall(r'name=dot1qTpFdbAddress\$repeat\?(\d+)[^>]*value="([0-9a-fA-F]{12})"', page, re.I):
        m = re.search(r'name=dot1qTpFdbPort\$repeat\?' + idx + r'\b[^>]*value="g?(\d+)"', page, re.I)
        if m:
            macs.setdefault(int(m.group(1)), []).append(":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower())

    ports = {}
    for p in range(1, NPORTS + 1):
        pc = sw.get(f"/config/port_config.htm?[PortConfigTable]Query:ifIndex={p}")
        rs = sw.get(f"/config/rmon_stats.htm?[rmonStats]Query:etherStatsIndex={p}")
        rx = re.search(r'Received Bytes \(Octets\)</td>\s*<td[^>]*>\s*(\d+)', rs)
        speed = field(pc, "ifSpeed")
        ports[p] = {
            "up": field(pc, "ifOperStatus") == "1",
            "speed": int(speed) // 1_000_000 if speed and speed.isdigit() else None,
            "duplex": DUPLEX.get(field(pc, "swIfDuplexOperMode") or ""),
            "alias": field(pc, "ifAlias") or "",
            "rx": int(rx.group(1)) if rx else None,
            "macs": sorted(macs.get(p, [])),
        }
    return ports


# ------------------------------------------------------------ LAN knowledge
def ping_sweep():
    def ping(ip):
        subprocess.run(["ping", "-c1", "-W1", "-I", IFACE, str(ip)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with ThreadPoolExecutor(64) as ex:
        list(ex.map(ping, LAN.hosts()))


def arp_table():
    out = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True, text=True).stdout
    mac2ip = {}
    for line in out.splitlines():
        m = re.match(r"(\S+) dev \S+ lladdr ([0-9a-f:]{17})", line)
        if m and ipaddress.ip_address(m.group(1)) in LAN:
            mac2ip.setdefault(m.group(2), m.group(1))
    return mac2ip


def own_macs():
    """hibou's own interfaces never appear in its ARP table."""
    res = {}
    out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        dev, ip = parts[1], parts[3].split("/")[0]
        if ipaddress.ip_address(ip) in LAN:
            try:
                with open(f"/sys/class/net/{dev}/address") as f:
                    res[f.read().strip()] = ip
            except OSError:
                pass
    return res


def rdns(ips):
    def look(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0]
        except OSError:
            return ip, None
    socket.setdefaulttimeout(3)
    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(look, ips))


def load_oui():
    for path in OUI_FILES:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return {m.group(1): m.group(2).strip()
                        for m in (re.match(r"([0-9A-F]{6})\s+\(base 16\)\s+(.*)", l) for l in f) if m}
        except OSError:
            continue
    return {}


def vendor(mac, oui):
    if int(mac[:2], 16) & 0x02:
        return "private/randomized MAC"
    return oui.get(mac.replace(":", "")[:6].upper(), "")


# ------------------------------------------------------------------ output
def human_rate(bps):
    for unit in ("B/s", "kB/s", "MB/s"):
        if bps < 1000:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} GB/s"


def rates(ports):
    now = time.time()
    try:
        with open(STATEFILE) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    out = {}
    dt = now - prev.get("t", now)
    for p, info in ports.items():
        old, new = prev.get(str(p)), info["rx"]
        if new is None or old is None or dt < 30:
            continue
        diff = new - old
        if diff < 0:            # 32-bit counter wrapped (or switch rebooted)
            diff += 2 ** 32 if old > 2 ** 31 else new
        out[p] = diff / dt
    os.makedirs(os.path.dirname(STATEFILE), exist_ok=True)
    with open(STATEFILE, "w") as f:
        json.dump({"t": now, **{str(p): i["rx"] for p, i in ports.items()}}, f)
    return out, dt


def device_html(mac, mac2ip, names, oui):
    ip = mac2ip.get(mac)
    name = names.get(ip) if ip else None
    label = html.escape(name.split(".")[0]) if name else (html.escape(ip) if ip else "<em>unknown</em>")
    bits = []
    if ip:
        bits.append(f'<a href="http://{ip}/">{html.escape(ip)}</a>')
    bits.append(f"<code>{mac}</code>")
    v = vendor(mac, oui)
    if v:
        bits.append(html.escape(v))
    return f"<li><strong>{label}</strong> <span class=dim>{' · '.join(bits)}</span></li>"


def gateway_mac(mac2ip):
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True).stdout
    m = re.search(r"via (\S+)", out)
    return next((mac for mac, ip in mac2ip.items() if m and ip == m.group(1)), None)


def render(ports, rate, dt, mac2ip, names, oui):
    gw = gateway_mac(mac2ip)
    rows = []
    for p in range(1, NPORTS + 1):
        i = ports[p]
        if i["up"]:
            link = f'<span class="up">●</span> {i["speed"] or "?"} M {i["duplex"] or ""}'
        else:
            link = '<span class="down">○</span> no link'
        r = human_rate(rate[p]) if p in rate else "–"
        devs = [device_html(m, mac2ip, names, oui) for m in i["macs"]]
        if len(devs) >= GROUP_MIN_MACS:
            cell = (f"<details><summary>{len(devs)} devices, behind another switch or access point"
                    f"</summary><ul>{''.join(devs)}</ul></details>")
        elif devs:
            cell = f"<ul>{''.join(devs)}</ul>"
        else:
            cell = '<span class="dim">–</span>' if not i["up"] else '<span class="dim">link, no traffic seen</span>'
        alias = f'<div class="dim">{html.escape(i["alias"])}</div>' if i["alias"] else ""
        if gw in i["macs"]:
            alias += '<div class="tag">router side</div>'
        rows.append(f'<tr class="{"" if i["up"] else "off"}"><th>g{p}{alias}</th>'
                    f"<td>{link}</td><td class=num>{r}</td><td>{cell}</td></tr>")
    nup = sum(1 for i in ports.values() if i["up"])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    win = f"over the last {dt / 60:.0f} min" if rate else "available from the next run"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SWITCH_NAME} port map</title>
<link rel="icon" type="image/svg+xml" href="/icons/owl.svg">
<style>
 body {{ max-width: 60em; margin: 0 auto; padding: 0 1em 2em; font-family: Tahoma, Verdana, Arial, sans-serif; line-height: 1.45; }}
 h1 {{ font-size: 1.5em; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ text-align: left; vertical-align: top; padding: .45em .6em; border-bottom: 1px solid #ddd; }}
 thead th {{ border-bottom: 2px solid #999; font-size: .9em; }}
 tbody th {{ white-space: nowrap; }}
 td.num {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
 ul {{ margin: 0; padding-left: 1.1em; }}
 .dim {{ color: #666; font-size: .88em; font-weight: normal; }}
 .tag {{ display: inline-block; font-size: .75em; font-weight: normal; background: #e6f0ff; color: #2456a6; border-radius: .3em; padding: 0 .35em; }}
 .up {{ color: #1a8f3c; }} .down {{ color: #999; }}
 tr.off {{ color: #888; }}
 code {{ font-size: .95em; }}
 summary {{ cursor: pointer; }}
</style></head><body>
<p><a href="/">← Hibou</a></p>
<h1>{SWITCH_NAME} port map</h1>
<p>{nup} of {NPORTS} ports have a link. Updated {stamp} ·
<a href="http://{SWITCH}/">switch login</a> · traffic = bytes received by the switch on that port, {win}.</p>
<table>
<thead><tr><th>Port</th><th>Link</th><th class=num>Traffic in</th><th>Devices seen on the port</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
<p class="dim">Devices come from the switch's MAC address table; names and IPs from hibou's ARP table and DNS.
Port descriptions shown under the port number can be set in the switch UI (Ports → Port Configuration → Description).</p>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/home/www/switchmap.html")
    ap.add_argument("--no-sweep", action="store_true", help="skip the LAN ping sweep")
    a = ap.parse_args()

    with open(PASSFILE) as f:
        password = f.read().strip()
    sw = Switch(SWITCH, password)
    try:
        ports = read_switch(sw)
    finally:
        sw.logout()

    if not a.no_sweep:
        ping_sweep()
    mac2ip = {**arp_table(), **own_macs()}
    seen_ips = {mac2ip[m] for i in ports.values() for m in i["macs"] if m in mac2ip}
    names = rdns(seen_ips)
    rate, dt = rates(ports)

    page = render(ports, rate, dt, mac2ip, names, load_oui())
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)


if __name__ == "__main__":
    main()
