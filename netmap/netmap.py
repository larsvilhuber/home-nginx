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
NOTES = os.path.join(HERE, "notes.md")
README = os.path.join(HERE, "README.md")
HA_URL = "http://127.0.0.1:8123"
HA_TOKEN = os.path.expanduser("~/.config/netmap/ha.token")
PASSFILE = os.path.expanduser("~/.config/netmap/dell-2816.pass")
UNIFI_URL = "https://127.0.0.1:11443"   # UniFi OS Server (UOS) on hibou
UNIFI_PASS = os.path.expanduser("~/.config/netmap/unifi.pass")  # one line, login:password
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


def unifi_api():
    """{'clients': stat/sta, 'devices': stat/device} from the UniFi Network API, or None."""
    import ssl
    try:
        with open(UNIFI_PASS) as f:
            user, pw = f.read().strip().split(":", 1)
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        op.open(urllib.request.Request(f"{UNIFI_URL}/api/auth/login",
                                       json.dumps({"username": user, "password": pw}).encode(),
                                       {"Content-Type": "application/json"}), timeout=15)
        get = lambda path: json.load(op.open(f"{UNIFI_URL}/proxy/network/api/s/default/{path}", timeout=15))["data"]
        return {"clients": get("stat/sta"), "devices": get("stat/device")}
    except (OSError, ValueError) as e:
        print(f"UniFi API unavailable: {e}", file=sys.stderr)
        return None


def unifi_clients(states, api=None):
    """MAC -> {ap, ssid, band, ip} for wireless UniFi clients: from the UniFi API when it works,
    else from the UniFi Network integration's device trackers in Home Assistant."""
    if api:
        return {c["mac"].lower(): {"ap": c["ap_mac"].lower(), "ssid": c.get("essid") or "",
                                   "band": BAND.get(c.get("radio") or "", c.get("radio") or ""),
                                   "ip": c.get("ip")}
                for c in api["clients"] if not c.get("is_wired") and c.get("ap_mac") and c.get("mac")}
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

    def __init__(self, ports, mac2ip, eero, unifi, inventory, oui, api=None):
        self.ports, self.eero, self.unifi, self.oui = ports, eero, unifi, oui
        # UniFi switches (from the API): mac -> {name, model, uplink_port, ports: {idx: {...}}}
        self.usw, self.sw_port = {}, {}
        for d in (api or {}).get("devices", []):
            if d.get("type") == "usw":
                self.usw[d["mac"].lower()] = {
                    "name": d.get("name"), "model": d.get("model"),
                    "uplink_port": (d.get("uplink") or {}).get("port_idx"),
                    "ports": {pt["port_idx"]: {"up": pt.get("up"), "speed": pt.get("speed"), "name": pt.get("name"),
                                               "poe": pt.get("poe_power")}
                              for pt in d.get("port_table", []) if "port_idx" in pt}}
        for c in (api or {}).get("clients", []):           # wired client -> (switch mac, port)
            if c.get("is_wired") and c.get("sw_mac") and c.get("sw_port"):
                self.sw_port[c["mac"].lower()] = (c["sw_mac"].lower(), c["sw_port"])
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


class Node:
    """One thing on the map: router, switch, access point, eero, inventory box or end device."""

    def __init__(self, kind, label, sub="", mac=None, ip=None, edge="", detail="", children=None):
        self.kind, self.label, self.sub, self.mac, self.ip = kind, label, sub, mac, ip
        self.edge, self.detail, self.children = edge, detail, children or []
        self.y = 0.0

    def leaves(self):
        return 1 if not self.children else sum(c.leaves() for c in self.children)


def dev_node(net, mac, kind="dev", sub=None):
    ip = net.ip.get(mac)
    inv = net.inv_dev.get(mac, {})
    label = net.name(mac) or ip or mac
    if sub is None:
        sub = inv.get("model") or net.vendor(mac)
    return Node(kind, label, sub, mac=mac, ip=ip, detail=inv.get("detail", ""))


def sort_key(net):
    return lambda m: (net.name(m) is None, net.name(m) or "", m)


def port_nodes(net, p, placed, skip=()):
    """What is behind Dell port p, as a list of Nodes (wrapped in the port's inventory box if any)."""
    macs = [m for m in net.ports[p]["macs"] if m not in skip]
    out = []

    for ap in sorted({d["ap"] for d in net.unifi.values()} & set(macs)):
        n = dev_node(net, ap, "ap")
        for m in sorted((m for m, d in net.unifi.items() if d["ap"] == ap), key=sort_key(net)):
            d = net.unifi[m]
            n.children.append(dev_node(net, m, sub=" · ".join(x for x in (d["ssid"], d["band"]) if x)))
            placed.add(m)
        placed.add(ap)
        out.append(n)

    units = [u for u in net.eero_units_on(p) if u not in skip]
    for node in sorted({d["node"] for d in net.eero.values() if net.eero_node_port(d["node"]) == p}):
        head = units[0] if units else None
        n = dev_node(net, head, "eero") if head else Node("eero", f"eero {node}")
        n.sub = f"eero “{node}”"
        for m in sorted((m for m, d in net.eero.items() if d["node"] == node and m not in placed),
                        key=sort_key(net)):
            n.children.append(dev_node(net, m, sub=net.eero[m]["type"]))
            placed.add(m)
        placed.update(units)
        out.append(n)

    for m in macs:
        if "USW" in net.inv_dev.get(m, {}).get("model", "") and m not in placed:
            out.append(dev_node(net, m, "switch"))
            placed.add(m)

    for m in sorted(macs, key=sort_key(net)):
        if m not in placed:
            out.append(dev_node(net, m))
            placed.add(m)

    inv = net.inv_ports.get(str(p))
    if inv:
        return [Node("inv", inv.get("device", "?"), inv.get("detail", ""), detail=inv.get("note", ""),
                     children=out)]
    return out


def port_on(net, mac, switch):
    """'p<n>' if mac is known to be on that switch's port n (UniFi API, else inventory), else ''."""
    sw, port = net.sw_port.get(mac, (None, None))
    if sw == switch:
        return f"p{port}"
    inv = net.inv_dev.get(mac, {})
    return f"p{inv['port']}" if inv.get("uplink") == switch and inv.get("port") else ""


def port_on_uplink(net, switch):
    p = net.usw.get(switch, {}).get("uplink_port")
    return f"p{p}" if p else ""


def build(net, gw_ip):
    """The whole network as a tree rooted at the Internet, plus each Dell port's subtree."""
    placed = set()
    gw_mac = next((m for m, ip in net.ip.items() if ip == gw_ip), None)
    gw_port = net.port_of.get(gw_mac)
    dell_mac = next((m for m, d in net.inv_dev.items() if "PowerConnect" in d.get("model", "")), None)
    dell = Node("switch", SWITCH_NAME, SWITCH, ip=SWITCH, mac=dell_mac, edge=f"g{gw_port}" if gw_port else "")
    # The Dell port where the router is seen is the Dell's uplink. If the inventory says the Dell
    # is plugged into another switch there (the USW-Flex), everything seen on that port is behind it.
    # UniFi API (sw_port of the Dell's MAC) first, hand-kept inventory as fallback.
    upstream = net.sw_port[dell_mac][0] if dell_mac in net.sw_port else net.inv_dev.get(dell_mac, {}).get("uplink")
    if not (upstream and gw_port and net.port_of.get(upstream) == gw_port):
        upstream = None
    by_port = {}
    for p in range(1, NPORTS + 1):
        if net.ports[p]["macs"] and not (upstream and p == gw_port):
            by_port[p] = port_nodes(net, p, placed, skip={gw_mac} if gw_mac else ())
            for n in by_port[p]:
                n.edge = f"g{p}"
            dell.children.extend(by_port[p])
    router = dev_node(net, gw_mac, "router", sub="firewall") if gw_mac else Node("router", "router")
    if upstream:
        up = dev_node(net, upstream, "switch")
        placed.update({upstream, gw_mac})
        beside = []
        for m in sorted(net.ports[gw_port]["macs"], key=sort_key(net)):
            if m in placed:
                continue
            n = dev_node(net, m)
            n.edge = port_on(net, m, upstream)
            beside.append(n)
            placed.add(m)
        beside.sort(key=lambda n: (int(n.edge[1:]) if n.edge[1:].isdigit() else 99, n.label))
        flex_p = port_on(net, dell_mac, upstream)
        dell.edge = f"{flex_p} → g{gw_port}" if flex_p else f"g{gw_port}"
        up.edge = port_on_uplink(net, upstream)
        up.children = [dell] + beside
        router.children = [up]
        by_port[gw_port] = [Node("switch", up.label, "uplink to this switch; devices behind it:",
                                 mac=upstream, ip=up.ip, edge=f"g{gw_port}", children=beside)]
    else:
        router.children = [dell]
    root = Node("cloud", "Internet", children=[router])

    stray = []
    for src in (net.eero, net.unifi):
        for m in src:
            if m not in placed:
                stray.append(dev_node(net, m))
                placed.add(m)
    return root, by_port, stray, gw_port, len(placed)


# --- lists (tabs) ---
def nodes_html(nodes):
    """Nested list for the tabs: boxes for infrastructure, bullets for end devices."""
    out, devs = [], []

    def flush():
        if devs:
            out.append("<ul>" + "".join(devs) + "</ul>")
            devs.clear()

    for n in nodes:
        if n.kind == "dev":
            bits = [f"port {esc(n.edge[1:])}"] if n.edge.startswith("p") else []
            bits += [esc(n.sub)] if n.sub else []
            if n.ip:
                bits.append(f'<a href="http://{esc(n.ip)}/">{esc(n.ip)}</a>')
            if n.mac:
                bits.append(f"<code>{n.mac}</code>")
            devs.append(f"<li><strong>{esc(n.label)}</strong> <span class=dim>{' · '.join(bits)}</span></li>")
            continue
        flush()
        kids = nodes_html(n.children)
        if len(n.children) >= FOLD_MIN:
            kids = f"<details><summary>{len(n.children)} devices</summary>{kids}</details>"
        sub = " · ".join(esc(x) for x in (n.sub, n.ip) if x)
        detail = f'<div class="dim">{esc(n.detail)}</div>' if n.detail else ""
        out.append(f'<div class="node {n.kind}"><div class="nh"><strong>{esc(n.label)}</strong> '
                   f'<span class=dim>{sub}</span></div>{detail}{kids}</div>')
    flush()
    return "".join(out)


def dell_tab(net, by_port, rate, dt, gw_port):
    rows = []
    for p in range(1, NPORTS + 1):
        i = net.ports[p]
        link = (f'<span class="up">●</span> {i["speed"] or "?"} M {i["duplex"] or ""}' if i["up"]
                else '<span class="down">○</span> no link')
        r = human_rate(rate[p]) if p in rate else "–"
        tags = f'<div class="dim">{esc(i["alias"])}</div>' if i["alias"] else ""
        if p == gw_port:
            tags += '<div class="tag">router side</div>'
        what = nodes_html(by_port.get(p, [])) or (
            '<span class="dim">link, nothing seen</span>' if i["up"] else '<span class="dim">–</span>')
        rows.append(f'<tr class="{"" if i["up"] else "off"}"><th>g{p}{tags}</th>'
                    f"<td>{link}<div class=num>{r}</div></td><td>{what}</td></tr>")
    win = f"averaged over the last {dt / 3600:.0f} h" if rate and dt >= 3600 else (
        f"over the last {dt / 60:.0f} min" if rate else "available from the next run")
    return (f'<p class="dim"><a href="http://{SWITCH}/">Dell login</a> · traffic = bytes received on the port, {win}.</p>'
            '<table><thead><tr><th>Port</th><th>Link · traffic in</th><th>What is behind it</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>")


def find(node, mac):
    if node.mac == mac:
        return node
    for c in node.children:
        f = find(c, mac)
        if f:
            return f
    return None


def flex_tab(net, root):
    flex = next(iter(net.usw), None) or next((m for m, d in net.inv_dev.items() if "USW" in d.get("model", "")), None)
    node = find(root, flex) if flex else None
    if not node:
        return '<p class="dim">The UniFi switch was not seen.</p>'
    head = (f'<p><strong>{esc(node.label)}</strong> ({esc(node.sub)}, {esc(node.ip or "")}) · '
            '<a href="https://hibou:11443/">open in UniFi (UOS server)</a></p>')
    gw = root.children[0] if root.children and root.children[0].kind == "router" else None
    info = net.usw.get(flex)
    if not info:                     # no UniFi API: hand-kept port numbers only
        kids = list(node.children)
        if gw and gw.mac:
            kids.insert(0, Node("dev", gw.label, "firewall (uplink to the Internet)", mac=gw.mac, ip=gw.ip))
        return head + '<p class="dim">UniFi API unavailable; port numbers are hand-kept.</p>' + nodes_html(kids)
    rows = []
    for idx, pt in sorted(info["ports"].items()):
        link = (f'<span class="up">●</span> {pt["speed"] or "?"} M' if pt["up"]
                else '<span class="down">○</span> no link')
        if pt.get("poe"):
            link += f'<div class="num">PoE {float(pt["poe"]):.1f} W</div>'
        here = []
        if idx == info["uplink_port"] and gw:
            here.append(Node("dev", gw.label, "firewall · uplink to the Internet", mac=gw.mac, ip=gw.ip))
        for c in node.children:
            if c.edge.split(" ")[0] == f"p{idx}":
                if c.kind == "switch" and c.children:
                    here.append(Node("switch", c.label, f"its {c.edge.split()[-1]}; see the Dell tab for what is behind it",
                                     ip=c.ip, mac=c.mac))
                else:
                    here.append(Node(c.kind, c.label, c.sub, mac=c.mac, ip=c.ip, detail=c.detail, children=c.children))
        label = esc(pt["name"]) if pt.get("name") and pt["name"] != f"Port {idx}" else ""
        rows.append(f'<tr class="{"" if pt["up"] else "off"}"><th>{idx}<div class="dim">{label}</div></th>'
                    f'<td>{link}</td><td>{nodes_html(here) or "<span class=dim>–</span>"}</td></tr>')
    loose = [c for c in node.children if not c.edge]
    extra = (f'<p class="dim">Seen behind this switch, port unknown:</p>{nodes_html(loose)}' if loose else "")
    return (head + '<table><thead><tr><th>Port</th><th>Link</th><th>What is on it</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>' + extra)


# --- map (desktop) ---
COLORS = {"cloud": "#777", "router": "#444", "switch": "#2e7d32", "inv": "#b08f1e",
          "ap": "#2f7ed8", "eero": "#7a52c7", "dev": "#999"}
COL_W, BOX_W, ROW_H = 205, 170, 22


def svg_map(root):
    rows = [0]

    def place(n, depth):
        n.depth = depth
        if not n.children:
            n.y = rows[0]
            rows[0] += 1
        else:
            for c in n.children:
                place(c, depth + 1)
            n.y = (n.children[0].y + n.children[-1].y) / 2
    place(root, 0)

    maxdepth = [0]
    parts = []

    def trunc(s, k):
        s = str(s)
        return s if len(s) <= k else s[:k - 1] + "…"

    def draw(n):
        maxdepth[0] = max(maxdepth[0], n.depth)
        x, y = n.depth * COL_W + 4, n.y * ROW_H + 14
        for c in n.children:
            cx, cy = c.depth * COL_W + 4, c.y * ROW_H + 14
            mid = x + BOX_W + (cx - x - BOX_W) / 2
            parts.append(f'<path d="M{x + BOX_W},{y} H{mid} V{cy} H{cx}" class="ln"/>')
            if c.edge:
                parts.append(f'<text x="{cx - 4}" y="{cy - 3}" class="edge" text-anchor="end">{esc(c.edge)}</text>')
            draw(c)
        tip = " · ".join(esc(v) for v in (n.label, n.sub, n.ip, n.mac, n.detail) if v)
        col = COLORS.get(n.kind, "#999")
        link_open = f'<a href="http://{esc(n.ip)}/">' if n.ip else ""
        link_close = "</a>" if n.ip else ""
        if n.kind == "dev":
            label = trunc(n.label, 22)
            sub = trunc(n.sub, 26) if n.sub else ""
            parts.append(f'<g>{link_open}<title>{tip}</title><circle cx="{x + 4}" cy="{y}" r="3" fill="{col}"/>'
                         f'<text x="{x + 11}" y="{y + 4}" class="lbl">{esc(label)}'
                         f'<tspan class="sub" dx="5">{esc(sub)}</tspan></text>{link_close}</g>')
        else:
            parts.append(f'<g>{link_open}<title>{tip}</title>'
                         f'<rect x="{x}" y="{y - 9}" width="{BOX_W}" height="18" rx="4" fill="{col}"/>'
                         f'<text x="{x + 6}" y="{y + 4}" class="box">{esc(trunc(n.label, 26))}</text>'
                         f'{link_close}</g>')
    draw(root)
    width = (maxdepth[0] + 1) * COL_W + 200
    height = rows[0] * ROW_H + 10
    return (f'<svg class="map" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Network map">'
            '<style>.ln{fill:none;stroke:#bbb;stroke-width:1.2}.lbl{font:12px Tahoma,Verdana,sans-serif;fill:#222}'
            '.sub{font-size:10.5px;fill:#888}.box{font:bold 11.5px Tahoma,Verdana,sans-serif;fill:#fff}'
            '.edge{font:10px Tahoma,Verdana,sans-serif;fill:#2e7d32}a:hover text{text-decoration:underline}</style>'
            + "".join(parts) + "</svg>")


def md(text):
    """Small Markdown subset for notes.md: ## headings, - bullets (plus "  - " sub-bullets), paragraphs,
    **bold**, `code`, [text](url) and <url>."""
    def inline(t):
        t = esc(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"&lt;(https?://[^&\s]+)&gt;", r'<a href="\1">\1</a>', t)
        return t
    out, items, para = [], [], []

    def li(item):
        text, subs = item
        sub = "<ul>" + "".join(f"<li>{inline(x)}</li>" for x in subs) + "</ul>" if subs else ""
        return f"<li>{inline(text)}{sub}</li>"

    def flush():
        if items:
            out.append("<ul>" + "".join(li(i) for i in items) + "</ul>")
            items.clear()
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()
    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("- "):
            if para:
                flush()
            items.append((line[2:], []))
        elif line.startswith("  - ") and items:
            items[-1][1].append(line[4:])
        elif line.startswith("  ") and items:
            if items[-1][1]:
                items[-1][1][-1] += " " + line.strip()
            else:
                items[-1] = (items[-1][0] + " " + line.strip(), items[-1][1])
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "".join(out)


def md_tab(path):
    try:
        with open(path) as f:
            body = md(f.read())
    except OSError:
        return f'<p class="dim">{esc(path)} is missing.</p>'
    rel = os.path.relpath(path, os.path.dirname(HERE))
    return body + (f'<p class="dim">Source: <code>{esc(path)}</code> '
                   f'(<a href="https://github.com/larsvilhuber/home-nginx/blob/main/{esc(rel)}">{esc(rel)} on GitHub</a>); '
                   'the page picks up edits on the next run.</p>')


CSS = """
 body { max-width: 80em; margin: 0 auto; padding: 0 1em 2em; font-family: Tahoma, Verdana, Arial, sans-serif; line-height: 1.45; }
 h1 { font-size: 1.5em; } h2 { font-size: 1.15em; margin-top: 1.4em; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; vertical-align: top; padding: .45em .6em; border-bottom: 1px solid #ddd; }
 thead th { border-bottom: 2px solid #999; font-size: .9em; }
 tbody th { white-space: nowrap; }
 .num { font-size: .85em; color: #555; white-space: nowrap; font-variant-numeric: tabular-nums; }
 ul { margin: .1em 0 .2em; padding-left: 1.1em; }
 .dim { color: #666; font-size: .88em; font-weight: normal; }
 .tag { display: inline-block; font-size: .75em; font-weight: normal; background: #e6f0ff; color: #2456a6; border-radius: .3em; padding: 0 .35em; }
 .node { border-left: 3px solid #999; padding: .2em .5em; margin: .3em 0 .5em; }
 .node.ap { border-color: #2f7ed8; background: #f3f8fe; }
 .node.eero { border-color: #7a52c7; background: #f7f4fd; }
 .node.inv { border-color: #c9b458; background: #f8f7ef; }
 .node.switch { border-color: #2e7d32; background: #f2f8f2; }
 .up { color: #1a8f3c; } .down { color: #999; }
 tr.off { color: #888; }
 code { font-size: .95em; }
 summary { cursor: pointer; color: #2456a6; font-size: .9em; }
 .mapwrap { overflow-x: auto; border: 1px solid #ddd; border-radius: .4em; padding: .5em; margin: .5em 0 1.2em; }
 .tabs > input { position: absolute; opacity: 0; }
 .tabs > label { display: inline-block; padding: .45em 1em; border: 1px solid #ccc; border-bottom: none;
   border-radius: .4em .4em 0 0; cursor: pointer; background: #f4f4f4; margin-right: .2em; }
 .tabs > input:checked + label { background: #fff; font-weight: bold; position: relative; top: 1px; }
 .tabs > .panel { display: none; border-top: 1px solid #ccc; padding-top: .6em; }
 #t1:checked ~ .p1, #t2:checked ~ .p2, #t3:checked ~ .p3, #t4:checked ~ .p4 { display: block; }
 .doc h2 { font-size: 1.05em; margin: 1.1em 0 .3em; } .doc ul { padding-left: 1.3em; } .doc li { margin: .25em 0; }
 .doc { max-width: 52em; }
 @media (max-width: 60em) {
   .mapwrap { display: none; }
   th, td { padding: .4em .3em; }
 }
"""


def render(net, rate, dt, gw_ip):
    root, by_port, stray, gw_port, ncl = build(net, gw_ip)
    stray_html = (f"<h2>Not placed</h2><p class=dim>Known to eero or UniFi, but their node or access point "
                  f"was not seen on the Dell.</p>{nodes_html(stray)}") if stray else ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    srcs = ["Dell MAC table"] + (["eero (via Home Assistant)"] if net.eero else []) + \
           (["UniFi (via Home Assistant)"] if net.unifi else [])
    flex = next((m for m, d in net.inv_dev.items() if "USW" in d.get("model", "")), None)
    flex_label = esc(net.name(flex) or "USW-Flex") if flex else "USW-Flex"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network map</title>
<link rel="icon" type="image/svg+xml" href="/icons/owl.svg">
<style>{CSS}</style></head><body>
<p><a href="/">← Hibou</a></p>
<h1>Network map</h1>
<p>{ncl} devices · updated {stamp} · <a href="http://{SWITCH}/">Dell login</a> ·
<a href="https://hibou:11443/">UniFi (UOS server)</a></p>
<div class="mapwrap">{svg_map(root)}</div>
<div class="tabs">
<input type="radio" name="tab" id="t1" checked><label for="t1">Dell 2816</label>
<input type="radio" name="tab" id="t2"><label for="t2">{flex_label}</label>
<input type="radio" name="tab" id="t3"><label for="t3">Notes</label>
<input type="radio" name="tab" id="t4"><label for="t4">Implementation</label>
<div class="panel p1">{dell_tab(net, by_port, rate, dt, gw_port)}</div>
<div class="panel p2">{flex_tab(net, root)}</div>
<div class="panel p3 doc">{md_tab(NOTES)}</div>
<div class="panel p4 doc">{md_tab(README)}</div>
</div>
{stray_html}
<p class="dim">Sources: {", ".join(srcs)}; names from DHCP (pfSense) via DNS; vendors from the IEEE list;
hand-kept inventory in <code>/home/www/netmap/inventory.json</code>. Hover a map entry for details; click to open its web page.
Colours: <span style="color:#2e7d32">switch</span>, <span style="color:#2f7ed8">UniFi AP</span>,
<span style="color:#7a52c7">eero</span>, <span style="color:#b08f1e">inventory</span>.</p>
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
    api = unifi_api()
    try:
        with open(INVENTORY) as f:
            inventory = json.load(f)
    except (OSError, ValueError) as e:
        print(f"inventory {INVENTORY}: {e}", file=sys.stderr)
        inventory = {}
    net = Net(ports, {**arp_table(), **own_macs()}, eero_clients(states), unifi_clients(states, api),
              inventory, load_oui(), api)
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
