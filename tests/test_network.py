"""
Network map test: reading neighbors, deciding layers, and a full run through the web app.

    python tests/test_network.py

The full run starts a pretend switch for every switch of the made-up campus in
network_demo.py (Nexus cores over SSH, Catalysts, a Dell, an Aruba, one that refuses
the login), draws the map the way the page does and checks the result.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("PA_DATA_DIR", tempfile.mkdtemp(prefix="pa-net-test-"))

from mock_switch import MockCLI, SSHSwitch, TelnetSwitch  # noqa: E402
import network as N  # noqa: E402
import network_demo as ND  # noqa: E402
import app as webapp  # noqa: E402


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return bool(condition)


IOS_CDP = """-------------------------
Device ID: C4-DIST-1.lgcc.cuny.edu
Entry address(es):
  IP address: 10.4.0.1
Platform: cisco WS-C4507R+E,  Capabilities: Router Switch IGMP
Interface: GigabitEthernet1/0/49,  Port ID (outgoing port): GigabitEthernet2/14
Holdtime : 150 sec
-------------------------
Device ID: SEP001122334455
Entry address(es):
  IP address: 10.4.40.10
Platform: Cisco IP Phone 8845,  Capabilities: Host Phone Two-port Mac Relay
Interface: GigabitEthernet1/0/3,  Port ID (outgoing port): Port 1
-------------------------
Device ID: AP-C4-12
Entry address(es):
  IP address: 10.4.60.12
Platform: cisco AIR-AP2802I-B-K9,  Capabilities: Router Trans-Bridge
Interface: GigabitEthernet1/0/12,  Port ID (outgoing port): GigabitEthernet0
"""
NXOS_CDP = """----------------------------------------
Device ID:CORE-2(FOX1234ABCD)
System Name: CORE-2

Interface address(es):
    IPv4 Address: 10.0.0.2
Platform: N7K-C7010, Capabilities: Router Switch IGMP Filtering Supports-STP-Dispute
Interface: Ethernet1/1, Port ID (outgoing port): Ethernet1/1
Holdtime: 151 sec
"""
IOS_LLDP = """Capability codes:
    (R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device
------------------------------------------------
Local Intf: Gi1/0/8
Chassis id: 001a.2b00.00c8
Port id: GE0/0/47
Port Description: GE0/0/47
System Name: C4-IDF

System Description:
ArubaOS (MODEL: ArubaS3500-48P), Version 7.4.1.9

Time remaining: 98 seconds
System Capabilities: B,R
Enabled Capabilities: B
Management Addresses:
    IP: 10.3.1.14
------------------------------------------------
Local Intf: Gi1/0/9
Chassis id: 0011.2233.4455
Port id: 0011.2233.4455
Port Description: SW PORT
System Name: SEP0011223344

System Description:
Cisco IP Phone CP-8845

Time remaining: 160 seconds
System Capabilities: B,T
Enabled Capabilities: B,T
Management Addresses:
    IP: 10.3.40.9
"""
NXOS_LLDP = """Capability codes:
  (R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device
Chassis id: 001b.0c00.0001
Port id: Ethernet2/1
Local Port id: Eth2/1
Port Description: uplink
System Name: B-DIST
System Description: Cisco IOS Software, Catalyst 4500 L3 Switch
Time remaining: 101 seconds
System Capabilities: B, R
Enabled Capabilities: B, R
Management Address: 10.2.0.1
"""
DELL_LLDP = """System capability supported: Bridge, Router
System capability enabled: Bridge

  Port         Device ID          Port ID          System Name    Capabilities    TTL
---------  -----------------  ---------------  -------------  --------------  -----
gi1/0/49   00:24:c4:12:34:56  Gi2/0/14         C4-DIST-1      B, R            118
gi1/0/50   00:24:c4:12:34:57  gi1/0/48                        B               91
gi1/0/7    00:11:22:33:44:55  00:11:22:33:44:55 SEP001122     B, T            150
"""
ARUBA_LLDP = """Capability codes: (R)Router, (B)Bridge, (A)Access Point, (P)Phone, (O)Other
LLDP Neighbor Information
-------------------------
Local Intf  Chassis ID         Capability  Remote Intf    Expiry-Time (Secs)  System Name
----------  ----------         ----------  -----------    ------------------  -----------
GE0/0/47    00:24:c4:12:34:56  B:R         Gi1/0/8        98                  C-DIST-1
GE0/0/5     00:0b:86:aa:bb:cc  A           eth0           110                 AP-12
"""


def parsers():
    ok = True
    print("\nReading neighbors")
    cdp = N.parse_cdp(IOS_CDP)
    ok &= check("IOS CDP: name without the domain, ports, address, platform",
                (cdp[0]["name"], cdp[0]["local"], cdp[0]["port"], cdp[0]["ip"]) ==
                ("C4-DIST-1", "GigabitEthernet1/0/49", "GigabitEthernet2/14", "10.4.0.1")
                and cdp[0]["platform"] == "cisco WS-C4507R+E", cdp[0])
    ok &= check("phones and access points aren't network gear",
                [N.is_network_gear(nb) for nb in cdp] == [True, False, False])
    nx = N.parse_cdp(NXOS_CDP)
    ok &= check("NX-OS CDP: serial number dropped from the name",
                nx and nx[0]["name"] == "CORE-2" and nx[0]["ip"] == "10.0.0.2" and nx[0]["local"] == "Ethernet1/1", nx)
    lldp = N.parse_lldp_detail(IOS_LLDP)
    ok &= check("IOS LLDP: the Aruba, and the phone that isn't gear",
                [(nb["name"], nb["local"], nb["port"], nb["ip"]) for nb in lldp] ==
                [("C4-IDF", "Gi1/0/8", "GE0/0/47", "10.3.1.14"), ("SEP0011223344", "Gi1/0/9", "SW PORT", "10.3.40.9")]
                and [N.is_network_gear(nb) for nb in lldp] == [True, False], lldp)
    nxl = N.parse_lldp_detail(NXOS_LLDP)
    ok &= check("NX-OS LLDP", nxl and (nxl[0]["name"], nxl[0]["local"], nxl[0]["port"], nxl[0]["ip"]) ==
                ("B-DIST", "Eth2/1", "Ethernet2/1", "10.2.0.1") and N.is_network_gear(nxl[0]), nxl)
    dell = N.parse_lldp_table(DELL_LLDP, "dell_powerconnect")
    ok &= check("Dell LLDP table: with and without a system name; the phone left out",
                [(nb["local"], nb["port"], nb["name"]) for nb in dell if N.is_network_gear(nb)] ==
                [("gi1/0/49", "Gi2/0/14", "C4-DIST-1"), ("gi1/0/50", "gi1/0/48", "")], dell)
    aruba = N.parse_lldp_table(ARUBA_LLDP, "aruba_mas")
    ok &= check("Aruba LLDP table: the switch, not the access point",
                [(nb["local"], nb["port"], nb["name"]) for nb in aruba if N.is_network_gear(nb)] ==
                [("GE0/0/47", "Gi1/0/8", "C-DIST-1")], aruba)

    print("\nWho each switch is")
    ident = N.switch_identity({"prompt": "B1-IDF", "version": "B1-IDF uptime is 1 year\nModel Number    : C9300-48P\n"
                               "Base Ethernet MAC Address          : 00:1a:2b:00:00:07\n"}, "cisco_ios")
    ok &= check("Catalyst: model and base MAC", ident["model"] == "C9300-48P" and ident["macs"] == {"001a.2b00.0007"}, ident)
    ok &= check("Catalyst 4500: model from the processor line",
                N.switch_identity({"version": "cisco WS-C4507R+E (MPC8572) processor (revision 10)"}, "cisco_ios")["model"]
                == "WS-C4507R+E")
    ok &= check("Nexus", N.switch_identity({"version": '  cisco Nexus7000 C7010 (10 Slot) Chassis ("Supervisor")'},
                                           "cisco_nxos")["model"] == "Nexus7000 C7010")
    ok &= check("Dell and Aruba",
                N.switch_identity({"version": "System Description:  Dell PowerConnect 5548P\nSystem MAC Address:  "
                                   "00:1e:c9:aa:bb:01\n"}, "dell_powerconnect") ==
                {"hostname": "", "model": "PowerConnect 5548P", "macs": {"001e.c9aa.bb01"}}
                and N.switch_identity({"version": "ArubaOS (MODEL: ArubaS3500-48P), Version 7.4"}, "aruba_mas")["model"]
                == "ArubaS3500-48P")
    arp = N.parse_arp("Internet  10.2.1.12   3   001a.2b00.0008  ARPA   Vlan99\n"
                      "10.2.1.13      00:05:22  001a.2b00.0009  Vlan99\n")
    ok &= check("ARP tables (IOS and NX-OS)", arp == {"10.2.1.12": "001a.2b00.0008", "10.2.1.13": "001a.2b00.0009"}, arp)

    print("\nLayers")
    ok &= check("models", [N.layer_hint(m)[0] for m in ("Nexus7000 C7010", "WS-C4507R+E", "C9500-24Y4C", "C9300-48P",
                                                       "WS-C2960X-48FPD-L", "PowerConnect 5548P", "ArubaS3500-48P",
                                                       "WS-C6509-E", "SomethingElse")]
                == ["core", "distribution", "distribution", "access", "access", "access", "access", "core", None])
    ok &= check("a router or firewall goes with the core", N.layer_hint("ISR4451-X/K9", "Router IGMP")[0] == "core")
    nodes = {k: {"hint": None, "hint_why": ""} for k in "ABCDEFG"}
    adj = {"A": {"B", "C", "G"}, "B": {"A", "D", "E"}, "C": {"A", "F"}, "D": {"B"}, "E": {"B"}, "F": {"C"}, "G": {"A"}}
    N.assign_layers(nodes, adj)
    ok &= check("no models known: the busiest switch is core, the ones with switches below are distribution",
                {k: v["layer_auto"] for k, v in nodes.items()} ==
                {"A": "core", "B": "distribution", "C": "distribution", "D": "access", "E": "access", "F": "access",
                 "G": "access"}, {k: v["layer_auto"] for k, v in nodes.items()})
    return ok


def demo_map():
    ok = True
    print("\nThe made-up campus")
    data = N.build_map(ND.demo_results(), {}, "test")
    by = {n["name"]: n for n in data["nodes"]}
    layers = {name: n["layer"] for name, n in by.items()}
    ok &= check("cores, distribution, access, and the router with the core",
                [k for k, v in sorted(layers.items()) if v == "core"] == ["CORE-1", "CORE-2", "INET-RTR-1"]
                and [k for k, v in sorted(layers.items()) if v == "distribution"] == ["B-DIST", "C-DIST-1", "C-DIST-2", "E-DIST"]
                and len([v for v in layers.values() if v == "access"]) == 13, layers)
    ok &= check("phones and access points left out; the router isn't on the list",
                not any(re.match("SEP|AP-", n) for n in by) and not by["INET-RTR-1"]["listed"])
    links = {(by_id(data, l["a"]), by_id(data, l["b"])): l for l in data["links"]}
    ok &= check("each link once, though both ends report it", len(data["links"]) == 28
                and links[("CORE-1", "CORE-2")]["pairs"] == [{"a_port": "Eth1/1", "b_port": "Eth1/1", "found_by": "CDP"},
                                                              {"a_port": "Eth1/2", "b_port": "Eth1/2", "found_by": "CDP"}],
                len(data["links"]))
    aruba = links.get(("C-DIST-1", "C4-IDF"))
    ok &= check("the Aruba by LLDP, seen from both ends", aruba and aruba["pairs"] ==
                [{"a_port": "Gi1/0/8", "b_port": "GE0/0/47", "found_by": "LLDP"}], aruba)
    dell = links.get(("B-DIST", "B2-IDF"))
    ok &= check("the Dell nothing reports: found by MAC on B-DIST Gi3/2, not on the core's uplink",
                dell and dell["mac"] and dell["pairs"][0]["a_port"] == "Gi3/2"
                and ("CORE-1", "B2-IDF") not in links, dell)
    ok &= check("the switch that refused the login: named and placed by its neighbor's CDP",
                by["B4-IDF"]["listed"] and not by["B4-IDF"]["read"] and ("B-DIST", "B4-IDF") in links
                and any("couldn't be read" in w for w in data["warnings"]), by.get("B4-IDF"))
    ok &= check("daisy chain kept: B3-IDF-2 hangs off B3-IDF", ("B3-IDF", "B3-IDF-2") in links)
    moved = N.build_map(ND.demo_results(), {"10.0.1.11": "distribution"}, "test")
    m1 = next(n for n in moved["nodes"] if n["name"] == "M1-IDF")
    ok &= check("a layer you pick wins, and automatic is remembered",
                m1["layer"] == "distribution" and m1["layer_auto"] == "access", m1)
    html = N.render_html(data)
    ok &= check("page renders with the data", "Network map" in html and '"CORE-1"' in html and "<script" in html)
    return ok


def by_id(data, node_id):
    return next(n["name"] for n in data["nodes"] if n["id"] == node_id)


def full_run():
    ok = True
    print("\nDrawing the map through the web app")
    servers, clis, rows = [], {}, []
    for res in ND.demo_results() + []:
        s = res["switch"]
        if res["outputs"] is None:
            cli = MockCLI("B4-IDF", {})
            server = TelnetSwitch(cli, "netops", "another-password")
        else:
            out = res["outputs"]
            cmds = {}
            for key, commands in N.COMMANDS[s["type"]].items():
                if out.get(key):
                    cmds[commands[0]] = out[key]
            name = out["prompt"]
            if s["type"] == "cisco_nxos":
                cli = MockCLI(name, cmds)
                server = SSHSwitch(cli, "admin", "core-pass")
            elif s["type"] == "aruba_mas":
                cli = MockCLI(name, cmds, start_enabled=False, enable_secret="en-secret", prompt_style="aruba")
                server = TelnetSwitch(cli, "netops", "sw-pass", user_prompt="User: ")
            elif s["type"] == "dell_powerconnect":
                cli = MockCLI(name, cmds, start_enabled=False, enable_secret="en-secret")
                server = TelnetSwitch(cli, "netops", "sw-pass", user_prompt="User Name:")
            else:
                cli = MockCLI(name, cmds, start_enabled=False, enable_secret="en-secret")
                server = TelnetSwitch(cli, "netops", "sw-pass")
        servers.append(server)
        address = f"127.0.0.1:{server.port}"
        clis[address] = cli
        rows.append({**s, "address": address, "protocol": "ssh" if s["type"] == "cisco_nxos" else "telnet"})
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": rows}, **base)
    client.post("/api/settings", json={"default_protocol": "telnet", "parallel": 8, "timeout": 10}, **base)
    login = {"username": "netops", "password": "sw-pass", "secret": "en-secret", "ssh_different": True,
             "ssh_username": "admin", "ssh_password": "core-pass"}

    ok &= check("form page loads, the main page has the button, the sample draws",
                client.get("/network", **base).status_code == 200
                and b'href="/network"' in client.get("/", **base).data
                and client.get("/network/demo", **base).status_code == 200)
    r = client.post("/api/network/run", json={"scope": "", "username": "netops"}, **base)
    ok &= check("no password -> asked for it", r.status_code == 400, r.json)
    r = client.post("/api/network/run", json={"scope": "", **login}, **base)
    ok &= check("run starts", r.status_code == 200, r.json)
    started = time.time()
    while client.get("/api/network/job", **base).json["state"] == "running" and time.time() - started < 300:
        time.sleep(0.3)
    job = client.get("/api/network/job", **base).json
    log = [ln["msg"] for ln in job["lines"]]
    ok &= check("map drawn", job["state"] == "done" and bool(job["report_file"]), log[-3:])
    ok &= check("the switch that refused the login is reported", sum("Couldn't read" in m for m in log) == 1, log)
    pages = client.get("/api/network/pages", **base).json["pages"]
    ok &= check("map listed", pages and pages[0]["file"] == job["report_file"], pages)
    page = client.get(f"/network/page/{job['report_file']}", **base)
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page.data.decode(), re.S)
                      .group(1))
    by = {n["name"]: n for n in data["nodes"]}
    ok &= check("every switch it could read is placed by its name; layers as on the sample",
                all(by[n]["listed"] for n in ("CORE-1", "CORE-2", "B-DIST", "C-DIST-1", "C4-IDF", "B2-IDF", "E3-IDF"))
                and by["CORE-1"]["layer"] == "core" and by["C-DIST-2"]["layer"] == "distribution"
                and by["C4-IDF"]["layer"] == "access" and data["summary"]["read"] == 18, data["summary"])
    names = {n["id"]: n["name"] for n in data["nodes"]}
    pairs = {(names[l["a"]], names[l["b"]]) if names[l["a"]] < names[l["b"]] else (names[l["b"]], names[l["a"]]): l
             for l in data["links"]}
    ok &= check("links between the switches on the list, the Aruba by LLDP, the Dell by MAC",
                ("CORE-1", "CORE-2") in pairs and ("C-DIST-1", "C4-IDF") in pairs
                and pairs[("B-DIST", "B2-IDF")]["mac"], sorted(pairs))
    ok &= check("no passwords in the page", b"sw-pass" not in page.data and b"core-pass" not in page.data)

    core1 = by["CORE-1"]["id"]
    r = client.post("/api/network/layer", json={"id": core1, "layer": "distribution"}, **base)
    ok &= check("pick a layer: saved", r.status_code == 200 and r.json["layers"] == {core1: "distribution"}
                and client.get("/api/network/layers", **base).json["layers"] == {core1: "distribution"}, r.json)
    r = client.post("/api/network/layer", json={"id": core1, "layer": ""}, **base)
    ok &= check("back to automatic", r.json["layers"] == {})
    r = client.post("/api/network/layer", json={"id": core1, "layer": "galaxy"}, **base)
    ok &= check("nonsense layers refused", r.status_code == 400)
    ok &= check("other files can't be opened as maps",
                client.get("/network/page/..%2Flayers.json", **base).status_code == 404
                and client.get("/network/page/layers.json", **base).status_code == 404)
    ok &= check("Draw this stack opens Stack topology with the switch picked",
                b"URLSearchParams(location.search).get(\"address\")" in client.get("/topology", **base).data)
    return ok


def main():
    ok = parsers()
    ok &= demo_map()
    ok &= full_run()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
