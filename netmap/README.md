## What this is

- `netmap.py` builds **https://hibou/netmap.html**, a static map of the home LAN: a desktop SVG map plus one tab per managed switch, the network notes and these implementation notes.
- **Repository:** [larsvilhuber/home-nginx](https://github.com/larsvilhuber/home-nginx), folder `netmap/`. It's checked out at `/home/www` on hibou, which nginx serves (Caddy terminates TLS in front of it).
- **Files in `netmap/`:**
  - `netmap.py` — the generator.
  - `inventory.json` — hand-kept facts.
  - `notes.md` — the network notes tab.
  - `README.md` — this tab.
- **Generated, not in git:** `/home/www/netmap.html`. The old `/switchmap.html` is a committed redirect to it.

## Running it

- **Cron** (user `vilhuber`, daily): `15 5 * * * /home/www/netmap/netmap.py >/dev/null 2>> $HOME/.cache/netmap/netmap.err`
- **By hand:** `/home/www/netmap/netmap.py`. Add `--no-sweep` to skip the LAN ping sweep, or `--out FILE` to write elsewhere.
- A run takes about 20 s with the sweep. Errors go to stderr. Optional sources that fail are skipped, and the page still builds.
- **State:** `~/.cache/netmap/dell-2816-counters.json` holds the previous Dell RX counters. The traffic column is the difference between two runs.

## Credentials (`~/.config/netmap/`, mode 600, never in git)

- `dell-2816.pass` — the Dell web UI password for user `admin`, one line.
- `ha.token` — a Home Assistant long-lived token for the non-admin HA user `claude`.
  - HA logs every LAN address in automatically (`trusted_networks`), so getting a token needs a password login done as an API flow: `/auth/login_flow` with handler `["homeassistant", null]`, then `/auth/token`, then the websocket message `auth/long_lived_access_token`.
- `unifi.pass` — a UniFi OS login as `login:password`, one line (split at the first colon). Currently it's the same local account Home Assistant's UniFi integration uses (`hauser`).

## Data sources and how they are read

- **Dell PowerConnect 2816** (`192.168.1.250`): web UI scraping. It has no usable API and no LLDP, and its SNMP isn't used here.
  - **Login:** POST `/config/log_off_page.htm` with `userName$query` and `password$query`; this sets a session cookie.
  - **MAC table:** `/config/address_dynamic.htm?Filter:…`, read from the `dot1qTpFdbAddress$repeat?N` and `dot1qTpFdbPort$repeat?N` inputs.
  - **Port status:** `/config/port_config.htm?[PortConfigTable]Query:ifIndex=N` (fields `ifOperStatus`, `ifSpeed`, `swIfDuplexOperMode`, `ifAlias`).
  - **Counters:** `/config/rmon_stats.htm?[rmonStats]Query:etherStatsIndex=N`; the "Received Bytes (Octets)" cell.
  - **Logout:** `/config/log_out.htm`. Query strings contain `[ ]`, so with curl use `-g`.
- **Home Assistant** (`http://127.0.0.1:8123/api/states`, Bearer token):
  - **eero integration** (schmittx/home-assistant-eero, a separate eero admin account): device trackers with `network_name`, `connected_to` (the node: "Basement" or "Living room") and `connection_type`.
  - **UniFi Network integration:** client device trackers with `ap_mac`, `essid` and `radio` (ng = 2.4 GHz, na = 5 GHz). They're disabled by default in HA; they were enabled by hand on 2026-11-27. They carry **no switch-port data for wired clients**.
- **hibou itself:**
  - a parallel ping sweep of `192.168.1.0/24` on `eno1`, then `ip neigh` for MAC→IP;
  - its own interface MACs;
  - the default route, to find the router;
  - reverse DNS through pi-hole and pfSense for names (the only name source);
  - the IEEE OUI list for vendors.
- **UniFi Network API** (UOS server on hibou):
  - **Login:** POST `https://127.0.0.1:11443/api/auth/login` with `{username, password}`; this sets a cookie.
  - **Clients:** `/proxy/network/api/s/default/stat/sta`. Wired clients have `sw_mac` and `sw_port`; wireless ones have `ap_mac`, `essid` and `radio`.
  - **Devices:** `/stat/device`, for each UniFi switch's `port_table` (up, speed, PoE) and its `uplink.port_idx`.
  - When the API works it's the source for UniFi wireless clients. If it fails, the script falls back to the HA trackers.

## How devices are placed

- **The Dell is the backbone:** each Dell port's MAC list is split into nested nodes, in this order:
  1. UniFi APs seen on the port, each with its wireless clients (from UniFi, wherever the Dell saw them);
  2. eero nodes whose clients mostly appear on the port, with their clients;
  3. UniFi switches named in the inventory;
  4. everything else.
- **Inventory boxes:** if `inventory.json` has a box for the port (e.g. the TRENDnet on g6), the port's nodes go inside it.
- **Uplink:** the Dell port where the router's MAC appears is its uplink. If the UniFi API puts the Dell's MAC on a UniFi switch port, then everything on that Dell port is drawn behind that switch, at the port the API reports, and the switch sits directly under the router. Failing the API, the inventory's `uplink`/`port` entries are used.
- **IP addresses:** IPs reported by eero or UniFi are ignored when ARP shows another MAC on that address, or when several clients claim it, because HA keeps stale IPs.
- **Names:** only from DNS, which pfSense feeds from DHCP (the owner's rule). Never fall back to eero or UniFi names.
- **Not placed:** clients known to eero or UniFi whose node was never seen on the Dell are listed separately.

## Hand-kept files

- `inventory.json`:
  - `ports.<n>` — a box drawn around Dell port n (`device`, `detail`, `note`);
  - `devices.<mac>` — `model`, `detail`, and `uplink` (MAC of the switch it plugs into), plus optional `port`.
  - Names don't go here.
- `notes.md` — the network notes. A small Markdown subset: `##` headings, `-` bullets with indented `-` sub-bullets, double-asterisk bold, backtick code, and links written as [text](url) or <url>.

## Changing the Dell's VLANs from a script (done 2026-11-28 for VLAN 10)

- **Create a VLAN:** POST `/config/create_new_vlan.htm` with:
  - `dot1qVlanIndex$add`, `dot1qVlanStaticName$add`;
  - egress, forbidden and untagged port lists set to `00`;
  - `dot1qVlanStaticRowStatus$add=4`, plus the `…VT`/`$current`/`$end…` marker fields the form carries.
- **Set membership:** POST `/config/vlan_member.htm` with:
  - `dot1qVlanIndex$queryStatic`, `dot1qVlanStaticName$query`, `dot1qVlanStaticRowStatus$query=1`;
  - `dot1qVlanStaticEgressPorts$query` = a hex port bitmap that must be **exactly 252 hex characters** (ports 1–999, then the 8 LAGs from index 1000). Leave the untagged and forbidden lists empty when there are none. Tagged-only on g6+g16 is `0401` followed by 248 zeros.
  - A wrong length comes back as "Wrong length" on `/config/error_collector.htm`.
- **Port VLAN settings** (`vlan_iterface.htm`) are only PVID, accepted frame types and ingress filtering. Leave them at PVID 1 / admit all.

## Known gaps

- The eeros' own ethernet ports are opaque (insteon on g7 shows as "wired" without detail).
- The Dell's MAC table only lists VLAN 1 MAC addresses. IoT-VLAN devices are placed through the UniFi data.
