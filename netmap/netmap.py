#!/usr/bin/env python3
"""Build a whole-network map of the home LAN as a static HTML page (netmap.html).

The Dell PowerConnect 2816 is the backbone: every other switch and access point
hangs off one of its ports. Sources, each optional except the Dell:
  - Dell 2816 web UI: MAC address table, port link/speed/description, RMON
    received-bytes counters (traffic = difference between runs).
  - Home Assistant REST API (token of non-admin user "claude"):
      * eero integration device trackers: which eero node a client is on,
      * UniFi Network integration device trackers: which UniFi AP, SSID and band.
  - hibou itself: ARP table after a ping sweep, reverse DNS (names come ONLY
    from DHCP on pfSense, via DNS), IEEE vendor list.
  - inventory.json (hand-kept): what is on a port / model of a device.

Usage:  netmap.py [--out /home/www/netmap.html] [--no-sweep]
Config: ~/.config/netmap/dell-2816.pass, ~/.config/netmap/ha.token (mode 600).
State:  ~/.cache/netmap/dell-2816-counters.json (previous counters, for rates).
Runs daily from cron as the user who owns those files.
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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

SWITCH = "192.168.1.250"
SWITCH_NAME = "Dell PowerConnect 2816"
NPORTS = 16
LAN = ipaddress.ip_network("192.168.1.0/24")
IFACE = "eno1"
HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(HERE, "inventory.json")
HA_URL = "http://127.0.0.1:8123"
HA_TOKEN = os.path.expanduser("~/.config/netmap/ha.token")
PASSFILE = os.path.expanduser("~/.config/netmap/dell-2816.pass")
STATEFILE = os.path.expanduser("~/.cache/netmap/dell-2816-counters.json")
OUI_FILES = ["/usr/share/ieee-data/oui.txt", "/var/lib/ieee-data/oui.txt"]
FOLD_MIN = 6  # a group with this many devices is folded behind a click

DUPLEX = {"1": "half", "2": "full"}
BAND = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}


# ---------------------------------------------------------------- Dell switch
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
            "macs": sorted(set(macs.get(p, []))),
        }
    return ports


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


# ------------------------------------------------------------ hibou's view
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


def gateway_ip():
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True).stdout
    m = re.search(r"via (\S+)", out)
    return m.group(1) if m else None


def rdns(ips):
    def look(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0].split(".")[0]
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


# ------------------------------------------------------------ Home Assistant
def ha_states():
    """All entity states, or [] if HA (or the token) is unavailable."""
    try:
        with open(HA_TOKEN) as f:
            token = f.read().strip()
        req = urllib.request.Request(f"{HA_URL}/api/states", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        print(f"Home Assistant unavailable: {e}", file=sys.stderr)
        return []


def eero_clients(states):
    """MAC -> {node, type, ip} from the eero integration (schmittx/home-assistant-eero)."""
    out = {}
    for st in states:
        a = st.get("attributes", {})
        if st["entity_id"].startswith("device_tracker.") and a.get("network_name") and a.get("mac") \
                and st.get("state") == "home":
            out[a["mac"].lower()] = {"node": a.get("connected_to") or "?",
                                     "type": a.get("connection_type") or "", "ip": a.get("ip")}
    return out


def unifi_clients(states):
    """MAC -> {ap, ssid, band, ip} for wireless clients from the UniFi Network integration."""
    out = {}
    for st in states:
        a = st.get("attributes", {})
        if st["entity_id"].startswith("device_tracker.") and a.get("ap_mac") and a.get("mac") \
                and st.get("state") == "home":
            out[a["mac"].lower()] = {"ap": a["ap_mac"].lower(), "ssid": a.get("essid") or "",
                                     "band": BAND.get(a.get("radio") or "", a.get("radio") or ""),
                                     "ip": a.get("ip")}
    return out


# ------------------------------------------------------------------ model
class Net:
    """Everything known about each MAC, plus the placement rules."""

    def __init__(self, ports, mac2ip, eero, unifi, inventory, oui):
        self.ports, self.eero, self.unifi, self.oui = ports, eero, unifi, oui
        self.inv_ports = inventory.get("ports", {})
        self.inv_dev = inventory.get("devices", {})
        self.ip = dict(mac2ip)
        arp_owner = {ip: mac for mac, ip in mac2ip.items()}
        claimed = {}
        for src in (eero, unifi):
            for mac, d in src.items():
                if d.get("ip"):
                    claimed.setdefault(d["ip"], set()).add(mac)
        for src in (eero, unifi):                   # IPs hibou cannot ARP (e.g. IoT VLAN), unless stale:
            for mac, d in src.items():              # ARP shows another MAC on it, or several MACs claim it
                ip = d.get("ip")
                if ip and arp_owner.get(ip, mac) == mac and len(claimed[ip]) == 1:
                    self.ip.setdefault(mac, ip)
        self.port_of = {m: p for p, i in ports.items() for m in i["macs"]}
        self.names = {}

    def resolve_names(self):
        self.names = rdns({ip for ip in self.ip.values() if ip})

    def vendor(self, mac):
        if int(mac[:2], 16) & 0x02:
            return "private/randomized MAC"
        return self.oui.get(mac.replace(":", "")[:6].upper(), "")

    def name(self, mac):
        ip = self.ip.get(mac)
        return self.names.get(ip) if ip else None

    def ap_port(self, ap):
        return self.port_of.get(ap)

    def eero_node_port(self, node):
        """Port where most of a node's clients were seen (the node itself is wired there)."""
        c = Counter(self.port_of[m] for m, d in self.eero.items() if d["node"] == node and m in self.port_of)
        return c.most_common(1)[0][0] if c else None

    def eero_units_on(self, port):
        units = [m for m in self.ports[port]["macs"] if self.vendor(m).lower().startswith("eero")]
        return sorted(units, key=lambda m: (self.name(m) is None, m))   # the named unit first


# ------------------------------------------------------------------ output
def human_rate(bps):
    for unit in ("B/s", "kB/s", "MB/s"):
        if bps < 1000:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} GB/s"


def esc(s):
    return html.escape(str(s))


def dev_li(net, mac, extra=""):
    ip, name = net.ip.get(mac), net.name(mac)
    label = esc(name) if name else (esc(ip) if ip else "<em>unknown</em>")
    bits = []
    if extra:
        bits.append(esc(extra))
    if ip:
        bits.append(f'<a href="http://{esc(ip)}/">{esc(ip)}</a>')
    bits.append(f"<code>{mac}</code>")
    inv = net.inv_dev.get(mac, {})
    v = inv.get("model") or net.vendor(mac)
    if v:
        bits.append(esc(v))
    return f"<li><strong>{label}</strong> <span class=dim>{' · '.join(bits)}</span></li>"


def fold(title, items, cls="grp"):
    body = f"<ul>{''.join(items)}</ul>" if items else ""
    if len(items) >= FOLD_MIN:
        return f'<details class="{cls}"><summary>{title} <span class=dim>({len(items)})</span></summary>{body}</details>'
    return f'<div class="{cls}"><div class="h">{title}</div>{body}</div>'


def node_box(net, kind, head_macs, title, clients_html):
    head = " ".join(f"<strong>{esc(net.name(m) or net.ip.get(m) or m)}</strong>" for m in head_macs[:1])
    sub = []
    for m in head_macs:
        inv = net.inv_dev.get(m, {})
        bits = [esc(x) for x in (net.ip.get(m), inv.get("model") or net.vendor(m)) if x]
        sub.append(f"<code>{m}</code> " + " · ".join(bits))
    detail = next((net.inv_dev[m]["detail"] for m in head_macs if net.inv_dev.get(m, {}).get("detail")), "")
    return (f'<div class="node {kind}"><div class="nh">{title} {head}</div>'
            f'<div class="dim">{"<br>".join(sub)}</div>'
            + (f'<div class="dim">{esc(detail)}</div>' if detail else "")
            + clients_html + "</div>")


def port_cell(net, p, placed):
    """Everything reachable through Dell port p, as nested boxes."""
    i = net.ports[p]
    left = [m for m in i["macs"]]
    parts = []

    inv = net.inv_ports.get(str(p))
    if inv:
        box = f'<div class="inv"><strong>{esc(inv.get("device", ""))}</strong>'
        for k in ("detail", "note"):
            if inv.get(k):
                box += f'<div class="{k}">{esc(inv[k])}</div>'
        parts.append(box + "</div>")

    # UniFi access points on this port, with their wireless clients (from UniFi, wherever the Dell saw them)
    aps = sorted({d["ap"] for d in net.unifi.values()} & set(i["macs"]))
    for ap in aps:
        by_ssid = {}
        for m, d in sorted(net.unifi.items(), key=lambda kv: (net.name(kv[0]) or "~", kv[0])):
            if d["ap"] == ap:
                by_ssid.setdefault(d["ssid"], []).append(dev_li(net, m, d["band"]))
                placed.add(m)
        clients = "".join(fold(f"wifi “{esc(s)}”", v) for s, v in sorted(by_ssid.items()))
        parts.append(node_box(net, "unifi", [ap], "UniFi AP", clients or '<div class="dim">no wireless clients</div>'))
        placed.add(ap)

    # eero nodes whose clients mostly appear on this port
    nodes = sorted({d["node"] for d in net.eero.values() if net.eero_node_port(d["node"]) == p})
    units = net.eero_units_on(p)
    for node in nodes:
        groups = {}
        for m, d in sorted(net.eero.items(), key=lambda kv: (net.name(kv[0]) or "~", kv[0])):
            if d["node"] == node and m not in placed:
                groups.setdefault(d["type"] or "?", []).append(dev_li(net, m))
                placed.add(m)
        clients = "".join(fold(esc(t), v) for t, v in sorted(groups.items()))
        parts.append(node_box(net, "eero", units, f"eero “{esc(node)}”", clients))
        placed.update(units)

    # UniFi switches (from the inventory) on this port: a box of their own
    for m in i["macs"]:
        if "USW" in net.inv_dev.get(m, {}).get("model", "") and m not in placed:
            parts.append(node_box(net, "unifi", [m], "UniFi switch", ""))
            placed.add(m)

    # whatever else the Dell saw on this port
    rest = [dev_li(net, m) for m in sorted(left, key=lambda m: (net.name(m) or "~", m)) if m not in placed]
    placed.update(left)
    if rest:
        title = "other devices on this port (directly, or behind the boxes above)" if parts else "devices on this port"
        parts.append(fold(title, rest))
    if not parts:
        parts.append('<span class="dim">–</span>' if not i["up"] else '<span class="dim">link, no traffic seen</span>')
    return "".join(parts)


CSS = """
 body { max-width: 64em; margin: 0 auto; padding: 0 1em 2em; font-family: Tahoma, Verdana, Arial, sans-serif; line-height: 1.45; }
 h1 { font-size: 1.5em; } h2 { font-size: 1.15em; margin-top: 1.6em; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; vertical-align: top; padding: .45em .6em; border-bottom: 1px solid #ddd; }
 thead th { border-bottom: 2px solid #999; font-size: .9em; }
 tbody th { white-space: nowrap; }
 td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
 ul { margin: .1em 0 .2em; padding-left: 1.1em; }
 .dim { color: #666; font-size: .88em; font-weight: normal; }
 .tag { display: inline-block; font-size: .75em; font-weight: normal; background: #e6f0ff; color: #2456a6; border-radius: .3em; padding: 0 .35em; }
 .inv { background: #f6f6f0; border-left: 3px solid #c9b458; padding: .2em .5em; margin-bottom: .4em; }
 .inv .detail { font-size: .88em; } .inv .note { font-size: .88em; color: #8a5a00; }
 .node { border-left: 3px solid #999; padding: .2em .5em; margin: .3em 0 .5em; }
 .node.unifi { border-color: #2f7ed8; background: #f3f8fe; }
 .node.eero { border-color: #7a52c7; background: #f7f4fd; }
 .nh { font-size: .95em; }
 .grp .h, .grp summary { font-size: .85em; color: #2456a6; margin-top: .25em; }
 .up { color: #1a8f3c; } .down { color: #999; }
 tr.off { color: #888; }
 code { font-size: .95em; }
 summary { cursor: pointer; }
 .path { background: #f4f4f4; padding: .4em .7em; border-radius: .3em; }
"""


def render(net, rate, dt, gw_ip):
    placed = set()
    gw_mac = next((m for m, ip in net.ip.items() if ip == gw_ip), None)
    gw_port = net.port_of.get(gw_mac)
    rows = []
    for p in range(1, NPORTS + 1):
        i = net.ports[p]
        link = (f'<span class="up">●</span> {i["speed"] or "?"} M {i["duplex"] or ""}' if i["up"]
                else '<span class="down">○</span> no link')
        r = human_rate(rate[p]) if p in rate else "–"
        tags = f'<div class="dim">{esc(i["alias"])}</div>' if i["alias"] else ""
        if p == gw_port:
            tags += '<div class="tag">router side</div>'
        rows.append(f'<tr class="{"" if i["up"] else "off"}"><th>g{p}{tags}</th>'
                    f"<td>{link}</td><td class=num>{r}</td><td>{port_cell(net, p, placed)}</td></tr>")

    # clients known to eero/UniFi whose node or AP was not seen on any Dell port
    stray = [dev_li(net, m, f"{d.get('node') or d.get('ap')}") for src in (net.eero, net.unifi)
             for m, d in src.items() if m not in placed]
    stray_html = (f"<h2>Not placed</h2><p class=dim>Known to eero or UniFi, but their node or access point "
                  f"was not seen on the Dell.</p><ul>{''.join(stray)}</ul>") if stray else ""

    gw_name = esc(net.name(gw_mac) or gw_ip or "router") if gw_mac else "router"
    path = (f'<p class="path">Internet → <strong>{gw_name}</strong> (firewall, {esc(gw_ip)}) → '
            f'<strong>{SWITCH_NAME}</strong> port {"g" + str(gw_port) if gw_port else "?"} → '
            "everything below</p>")
    nup = sum(1 for i in net.ports.values() if i["up"])
    ncl = len(placed | set(net.eero) | set(net.unifi))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    win = f"averaged over the last {dt / 3600:.0f} h" if rate and dt >= 3600 else (
        f"over the last {dt / 60:.0f} min" if rate else "available from the next run")
    srcs = ["Dell MAC table"] + (["eero (via Home Assistant)"] if net.eero else []) + \
           (["UniFi (via Home Assistant)"] if net.unifi else [])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network map</title>
<link rel="icon" type="image/svg+xml" href="/icons/owl.svg">
<style>{CSS}</style></head><body>
<p><a href="/">← Hibou</a></p>
<h1>Network map</h1>
{path}
<p>{ncl} devices · {nup} of {NPORTS} Dell ports with a link · updated {stamp} ·
<a href="http://{SWITCH}/">Dell login</a> · <a href="https://hibou:11443/">UniFi</a></p>
<table>
<thead><tr><th>Dell port</th><th>Link</th><th class=num>Traffic in</th><th>What is behind it</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
{stray_html}
<p class="dim">Sources: {", ".join(srcs)}; names from DHCP (pfSense) via DNS; vendors from the IEEE list.
Traffic = bytes the Dell received on that port, {win}.
Boxes: <span style="color:#2f7ed8">UniFi access point</span>, <span style="color:#7a52c7">eero node</span>,
<span style="color:#8a5a00">hand-kept inventory</span> (<code>/home/www/netmap/inventory.json</code>).
Wired clients of the USW-Flex and of the eeros are not broken down by port: neither reports that through Home Assistant.</p>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/home/www/netmap.html")
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
    states = ha_states()
    try:
        with open(INVENTORY) as f:
            inventory = json.load(f)
    except (OSError, ValueError) as e:
        print(f"inventory {INVENTORY}: {e}", file=sys.stderr)
        inventory = {}
    net = Net(ports, {**arp_table(), **own_macs()}, eero_clients(states), unifi_clients(states),
              inventory, load_oui())
    net.resolve_names()
    rate, dt = rates(ports)

    page = render(net, rate, dt, gateway_ip())
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)


if __name__ == "__main__":
    main()
