## Layout

- **Internet → belier** (pfSense firewall, `192.168.1.1`) → **usw-flex-1** (UniFi USW-Flex-2.5G-5, `192.168.1.245`).
- Flex ports: 1 belier (uplink), 2 hibou, 3 cerf, 4 reolink-hub (100 Mb/s), 5 the **Dell PowerConnect 2816** (`192.168.1.250`), on the Dell's g16.
- The Dell is the backbone for everything else: g3 eero-sous-sol ("Basement"), g7 eero-salon ("Living room"), g6 the roof TRENDnet switch, plus wired devices on g1, g2, g10 and g14.
- Names come only from DHCP on pfSense (via DNS). They are never set in eero or UniFi.

## VLANs

- **VLAN 1:** the main LAN, `192.168.1.0/24`, untagged on every port.
- **VLAN 10 "IoT":** the **IoT-wifi** network, `192.168.10.0/24`, gateway `192.168.10.1` on pfSense. The UniFi APs send it tagged.
- On the Dell, VLAN 10 is **tagged on g6** (roof switch, both UniFi APs) and **g16** (uplink to the Flex and pfSense). It was set on 2026-11-28.
- If an IoT-wifi access point or VLAN 10 device moves to another Dell port, that port needs VLAN 10 tagged too. Otherwise its devices connect to the wifi but can't reach anything.
- History: switching the Dell to managed mode on 2026-11-27 left it with VLAN 1 only. That silently cut off all IoT-wifi devices until VLAN 10 was added.

## Dell PowerConnect 2816

- Managed mode, address from DHCP (reserved at `192.168.1.250` on pfSense). Web UI at <http://192.168.1.250/>, user `admin`.
- **"Restore Saved Configuration" page after every login:** don't click Apply there, because that reloads the saved configuration and reboots the switch. To get past it, run `document.cookie="checked=true;path=/"` in the browser console, then open `/home.htm`.
- **"Request Entity Too Large" errors:** the web server has a tiny header limit. Use a private window, or `127.0.0.1` instead of `localhost` when tunnelling.
- **Telnet (port 23):** this is a factory debug shell, not a management interface, and it can't be turned off from the web UI.
- **SNMP:** v2c read-only is enabled.
- **Cable test:** System → Diagnostics → Integrated Cable Test. Running it drops that port for a few seconds.
- **Not confirmed:** whether changes survive a reboot without an explicit save. After a restart, check that VLAN Membership still shows VLAN 10 with g6 and g16 marked T.

## Roof (Dell g6)

- **TRENDnet TPE-S50:** 6 ports, all 10/100 (4 PoE+, 60 W budget).
  - Every port is 10/100, so the whole roof shares **100 Mb/s** to the house: cameras plus both UniFi APs.
  - A cable test on 2026-11-27 reported "open" at ~14 m. That's the wire pairs a 10/100 port leaves unused, not a fault.
- **UniFi APs:** unifi-bateau (`.240`) and Unifi-Maison (`.241`, DNS name `unifi-outside`), both UAP-AC-Mesh. Rated 867 Mb/s at 5 GHz and 300 Mb/s at 2.4 GHz.
  - unifi-bateau sits at the end of a 100 m PoE run.
  - MACs: unifi-bateau `f4:92:bf:29:24:30`, Unifi-Maison `f4:e2:c6:13:c0:0c`.
  - Each AP shows the other as its "uplink", because the TRENDnet passes LLDP through. Both are wired and wireless meshing is off, so traffic isn't affected.
  - Because of that loop, neither AP can be traced back to the Flex, so the APs **don't appear in UniFi's topology view**. They're still listed and working under UniFi Devices. Adding the Dell as a third-party device (MAC `5c:26:0a:9b:e2:08`) probably won't fix it.
- **Upgrade path:** a gigabit UniFi PoE switch (e.g. USW-Lite-8-PoE) in place of the TRENDnet. It would fix both the 100 Mb/s limit and the uplink label, and the roof would appear in UniFi's topology.

## eero

- Two nodes: eero-salon (`.242`, "Living room", Dell g7) and eero-sous-sol (`.243`, "Basement", Dell g3), wifi network `belanger-base`.
- **No local API:** clients and which node each one is on come from the eero integration in Home Assistant, logged in with a separate eero admin account.

## Other

- **BirdNET Pi** (`192.168.1.23`): on wifi (Unifi-Maison). Its ethernet port is a private point-to-point link: the Pi is `192.168.2.1` and the wifi microphone `192.168.2.14`. Because of that, `192.168.2.1` can appear to answer on the LAN through the Pi's wifi.
- **hibou** has a second interface: USB wifi `hibou-wifi` (`192.168.1.24`), on eero Basement.

## Planned (when the UniFi PoE switch replaces the roof TRENDnet)

- **Roof switch:** a UniFi PoE switch in place of the TRENDnet on Dell g6. That brings gigabit to the roof, makes both APs appear in UniFi's topology, and gives per-port VLANs.
- **Move the Reolink devices to IoT (VLAN 10)**, all together so the hub sees every camera on one network:
  - reolink-camera: change its wifi network to IoT-wifi in the Reolink app.
  - reolink-hub (now on Flex port 4, not its final location): set its port's Native VLAN to IoT in UniFi.
  - reolink-exterieur (now on Dell g2, not its final location): its Dell port needs VLAN 10 untagged with PVID 10, or set the VLAN on a UniFi port if it moves there.
  - reolink-entree (behind the roof switch): set its port to IoT on the new UniFi switch.
  - Then: re-add the cameras in the hub over the LAN (its own wifi is no longer needed for them), add DHCP reservations for `192.168.10.x` on pfSense, and update Home Assistant's Reolink integration and go2rtc with the new addresses.
- **Indoor IoT coverage** (robovac etc. now reach the outside APs): a small indoor UniFi AP on a Dell port with VLAN 10 tagged. Don't reuse the IoT-wifi name on the eeros: they can't tag VLAN 10, and devices would roam between two different networks.

## How this map is built

See the **Implementation** tab (`netmap/README.md` in the [home-nginx repository](https://github.com/larsvilhuber/home-nginx)).
