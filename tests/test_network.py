"""
Network map test: reading neighbors, deciding layers, and a full run through the web app.

    python tests/test_network.py

The full run starts a pretend switch for every switch of the made-up campus in
network_demo.py (Nexus cores over SSH, Catalysts, a Dell, an Aruba, one that refuses
the login), draws the map the way the page does and checks the result: only switches
on the list are drawn.
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
    ok &= check("Nexus (and 6500-class chassis) are the core; everything else isn't",
                [N.layer_hint(m)[0] for m in ("Nexus7000 C7010", "N7K-C7010", "N9K-C93180YC-EX", "WS-C6509-E",
                                              "WS-C4507R+E", "C9500-24Y4C", "C9300-48P", "PowerConnect 5548P", "")]
                == ["core", "core", "core", "core", None, None, None, None, None])
    ok &= check("a Nexus on the list is core even if its model wasn't read", N.layer_hint("", "cisco_nxos")[0] == "core")

    print("\nAddress ranges")
    ok &= check("10.0.0.0/16, 10.0.x.x, 10.0.*, 10.0 all mean the same; several with commas; empty is none",
                [N.parse_ranges(t) for t in ("10.0.0.0/16", "10.0.x.x", "10.0.*", "10.0.")] == [[N.ipaddress.ip_network("10.0.0.0/16")]] * 4
                and [str(n) for n in N.parse_ranges("10.0.0.0/16, 172.16.5.x")] == ["10.0.0.0/16", "172.16.5.0/24"]
                and N.parse_ranges("  ") == [] and N.parse_ranges("10.0.3.7") == [N.ipaddress.ip_network("10.0.3.7/32")])
    bad = []
    for text in ("10.x.0.1", "switches", "10.0.0.0/33", "fe80::/10"):
        try:
            N.parse_ranges(text)
        except ValueError as exc:
            bad.append(str(exc))
    ok &= check("anything else is refused with an example", len(bad) == 4 and all("10.0.x.x" in b for b in bad), bad)
    nets = N.parse_ranges("10.0.0.0/16")
    ok &= check("in the range or not", N.in_ranges("10.0.200.3", nets) and not N.in_ranges("10.1.0.3", nets)
                and not N.in_ranges("", nets) and not N.in_ranges("10.0.0.1", []))
    nodes = {k: {"hint": None, "hint_why": ""} for k in "ABCDEFG"}
    adj = {"A": {"B", "C", "G"}, "B": {"A", "D", "E"}, "C": {"A", "F"}, "D": {"B"}, "E": {"B"}, "F": {"C"}, "G": {"A"}}
    N.assign_layers(nodes, adj)
    ok &= check("no Nexus: the busiest switch in the middle is core, the rest access",
                {k: v["layer_auto"] for k, v in nodes.items()} ==
                {"A": "core", "B": "access", "C": "access", "D": "access", "E": "access", "F": "access",
                 "G": "access"}, {k: v["layer_auto"] for k, v in nodes.items()})
    return ok


def demo_map():
    ok = True
    print("\nThe made-up campus")
    nets = N.parse_ranges(N.DEFAULT_RANGE)
    data = N.build_map(ND.demo_results(), {}, "test", also_draw=nets)
    by = {n["name"]: n for n in data["nodes"]}
    layers = {name: n["layer"] for name, n in by.items()}
    ok &= check("the two Nexus are the core, every closet (the 4507 too) is access, no distribution row",
                sorted(k for k, v in layers.items() if v == "core") == ["CORE-1", "CORE-2"]
                and layers["B127-4507"] == "access" and len([v for v in layers.values() if v == "access"]) == 22
                and data["summary"]["layers"]["distribution"] == 0, layers)
    ours = {s[4] for s in ND.SWITCHES}
    ok &= check("drawn: the list plus the two switches in 10.0.x.x; no router, wireless controller, phone, AP or "
                "the lab switch on 192.168", set(by) == ours | {"C5-IDF-2", "E9-IDF"}, sorted(set(by) ^ ours))
    extra = by["C5-IDF-2"]
    ok &= check("a switch that isn't on the list: dashed, not logged into, who saw it, building of the closet "
                "it hangs off", not extra["on_list"] and not extra["listed"] and not extra["read"]
                and extra["seen_by"] == ["C5-IDF"] and extra["building"] == "C Building" and extra["building_guess"]
                and extra["model"] == "WS-C2960X-48FPD-L" and extra["layer"] == "access"
                and "through 1 other switch" in extra["layer_why"], extra)
    ok &= check("one plugged straight into the core: no building guessed (the core's isn't its)",
                by["E9-IDF"]["building"] == "" and sorted(by["E9-IDF"]["seen_by"]) == ["CORE-1", "CORE-2"]
                and by["E9-IDF"]["links"] == 2, by["E9-IDF"])
    ok &= check("warnings: the ones drawn dashed, and the switch outside the range named but not drawn",
                any("Drawn dashed: 2" in w and "C5-IDF-2, E9-IDF" in w for w in data["warnings"])
                and any("LAB-SW-TEMP" in w and "or in 10.0.0.0/16" in w for w in data["warnings"])
                and not any("INET-RTR" in w or "WLC" in w or "SEP00" in w for w in data["warnings"]), data["warnings"])
    ok &= check("totals: 22 on the list, 21 read, 2 more found", data["summary"]["listed"] == 22
                and data["summary"]["read"] == 21 and data["summary"]["extra"] == 2, data["summary"])
    links = {(by_id(data, l["a"]), by_id(data, l["b"])): l for l in data["links"]}
    pairs = {tuple(sorted(k)): v for k, v in links.items()}
    ok &= check("each link once, though both ends report it", len(data["links"]) == 38
                and pairs[("CORE-1", "CORE-2")]["pairs"] == [{"a_port": "Eth1/1", "b_port": "Eth1/1", "found_by": "CDP"},
                                                              {"a_port": "Eth1/2", "b_port": "Eth1/2", "found_by": "CDP"}]
                and pairs[("C5-IDF", "C5-IDF-2")]["pairs"] == [{"a_port": "Gi1/0/48", "b_port": "Gi1/0/49",
                                                                 "found_by": "CDP"}], len(data["links"]))
    aruba = pairs.get(("C4-IDF", "CORE-1"))
    ok &= check("the Aruba by LLDP, seen from both ends", aruba and len(aruba["pairs"]) == 1
                and aruba["pairs"][0]["found_by"] == "LLDP"
                and {aruba["pairs"][0]["a_port"], aruba["pairs"][0]["b_port"]} == {"Eth3/14", "GE0/0/47"}, aruba)
    dell = pairs.get(("B2-IDF", "CORE-1"))
    ok &= check("the Dell nothing reports: found by MAC on CORE-1 Eth2/2, not on CORE-2's link to CORE-1",
                dell and dell["mac"] and "Eth2/2" in (dell["pairs"][0]["a_port"], dell["pairs"][0]["b_port"])
                and ("B2-IDF", "CORE-2") not in pairs, dell)
    ok &= check("the switch that refused the login: named and placed by its neighbor's CDP, not drawn twice",
                not by["B4-IDF"]["read"] and by["B4-IDF"]["listed"] and ("B4-IDF", "CORE-1") in pairs
                and len([n for n in data["nodes"] if n["address"] == "10.0.2.15"]) == 1
                and any("couldn't be read" in w for w in data["warnings"]), by.get("B4-IDF"))
    ok &= check("daisy chain kept: B3-IDF-2 hangs off B3-IDF", ("B3-IDF", "B3-IDF-2") in pairs
                and "through 1 other switch" in by["B3-IDF-2"]["layer_why"])

    only = N.build_map(ND.demo_results(), {}, "test")
    ok &= check("with no range: only the list is drawn, and the other switches are named instead",
                {n["name"] for n in only["nodes"]} == ours and len(only["links"]) == 35
                and any("C5-IDF-2, E9-IDF, LAB-SW-TEMP" in w and "or in" not in w for w in only["warnings"]),
                only["warnings"])
    part = [r for r in ND.demo_results() if r["switch"]["building"] == "B Building"]
    one = N.build_map(part, {}, "B Building", also_draw=nets, whole_list=ND.demo_switch_list())
    cores = [n for n in one["nodes"] if n["name"].startswith("CORE")]
    ok &= check("one building: the cores it's plugged into are drawn as yours (on the list, not read this time)",
                len(cores) == 2 and all(n["on_list"] and not n["listed"] and n["layer"] == "core"
                                        and n["building"] == "M Building" for n in cores)
                and one["summary"]["listed"] == 6 and one["summary"]["extra"] == 0
                and not any("Drawn dashed" in w for w in one["warnings"]), [(n["name"], n["on_list"]) for n in cores])

    renamed = ND.demo_results()
    for r in renamed:
        if r["switch"]["address"] == "10.0.2.15":           # the one that refuses the login, on another address
            r["switch"].update(address="10.9.9.15", name="B4-IDF")
    b4 = N.build_map(renamed, {}, "test", also_draw=nets)
    b4_node = next(n for n in b4["nodes"] if n["address"] == "10.9.9.15")
    ok &= check("a switch that can't be read is still found by its name on the list (its address there differs)",
                b4_node["name"] == "B4-IDF" and b4_node["links"] == 1
                and not any(n["address"] == "10.0.2.15" for n in b4["nodes"]), b4_node)
    nameless = ND.demo_results()
    for r in nameless:
        r["switch"]["name"] = ""
    ok &= check("with no names on the list, it still gets its name from its neighbor's CDP",
                next(n for n in N.build_map(nameless, {}, "test")["nodes"] if n["address"] == "10.0.2.15")["name"]
                == "B4-IDF")
    lst = ND.demo_switch_list()
    part = [r for r in ND.demo_results() if r["switch"]["building"] == "C Building"]
    for row in lst:
        if row["name"] == "CORE-1":
            row["address"] = "10.0.0.100"                # listed under another address than CDP reports
    c_only = N.build_map(part, {}, "C Building", also_draw=[], whole_list=lst)
    core1 = next((n for n in c_only["nodes"] if n["name"] == "CORE-1"), None)
    ok &= check("mapping one building: a core on the list under another address is matched by its name",
                core1 and core1["on_list"] and core1["address"] == "10.0.0.100" and core1["layer"] == "core", core1)

    sg = data["suggestions"]
    ok &= check("ideas for the switch list: the two switches it found, with a type from their model",
                [(a["name"], a["address"], a["type"], a["building"]) for a in sg["add"]] ==
                [("C5-IDF-2", "10.0.3.16", "cisco_ios", "C Building"), ("E9-IDF", "10.0.5.19", "cisco_ios", "")]
                and sg["fix"] == [] and sg["gone"] == [], sg)
    sg = b4["suggestions"]
    ok &= check("a switch it couldn't log into that its neighbors see elsewhere: change its address",
                sg["fix"] == [{"name": "B4-IDF", "old": "10.9.9.15", "new": "10.0.2.15", "seen_by": ["CORE-1"],
                               "problem": "login failed (check the username and password)"}], sg["fix"])
    lonely = ND.demo_results()
    lonely.append({"switch": {"address": "10.0.9.9", "type": "cisco_ios", "protocol": "telnet", "building": "B Building",
                              "location": "B9 IDF", "name": "B9-IDF"}, "outputs": None, "problem": "timed out"})
    ok &= check("a switch on the list nothing answers for and nobody sees: maybe gone",
                N.build_map(lonely, {}, "test")["suggestions"]["gone"] ==
                [{"name": "B9-IDF", "address": "10.0.9.9", "problem": "timed out"}])
    ok &= check("its type from a model or a description",
                [N.guess_type(m) for m in ("WS-C2960X-48FPD-L", "C9300-48P", "N7K-C7010", "Cisco Nexus Operating System "
                                           "(NX-OS) Software", "PowerConnect 5548P", "ArubaOS (MODEL: ArubaS3500-48P)",
                                           "Cisco IOS Software, C3850 Software", "Aruba 2930F", "ISR4451-X/K9", "")]
                == ["cisco_ios", "cisco_ios", "cisco_nxos", "cisco_nxos", "dell_powerconnect", "aruba_mas", "cisco_ios",
                    "", "", ""])

    moved = N.build_map(ND.demo_results(), {"10.0.2.27": "distribution"}, "test", also_draw=nets)
    sw4507 = next(n for n in moved["nodes"] if n["name"] == "B127-4507")
    ok &= check("a layer you pick wins, and automatic is remembered",
                sw4507["layer"] == "distribution" and sw4507["layer_auto"] == "access", sw4507)
    html = N.render_html(data)
    ok &= check("page renders with the data and the filters", '"CORE-1"' in html and 'id="collapse"' in html
                and 'id="dist"' in html and "show-layer" in html and "LAB-SW-TEMP" in html
                and '"INET-RTR-1"' not in html and "2 more found in 10.0.0.0/16" in html
                and '<label id="others-label">' in html)
    ok &= check("no 'Not on your list' filter when there's nothing to hide",
                '<label id="others-label" hidden>' in N.render_html(only))
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
    r = client.post("/api/network/run", json={"scope": "", "also_draw": "10.0.x.x, our switches", **login}, **base)
    ok &= check("a range that isn't one -> said so", r.status_code == 400
                and r.json["problems"] == ["'our' isn't an address range. Use something like 10.0.0.0/16 or 10.0.x.x."],
                r.json)
    r = client.post("/api/network/run", json={"scope": "", "also_draw": "10.0.3.0/24, 10.0.5.x", **login}, **base)
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
                all(n in by for n in ("CORE-1", "CORE-2", "B127-4507", "C4-IDF", "B2-IDF", "E3-IDF"))
                and by["CORE-1"]["layer"] == "core" and by["B127-4507"]["layer"] == "access"
                and by["C4-IDF"]["layer"] == "access" and data["summary"]["read"] == 21
                and data["summary"]["listed"] == 22, data["summary"])
    names = {n["name"] for n in data["nodes"]}
    ok &= check("the switches in the ranges you gave are drawn too; nothing else off the list",
                {"C5-IDF-2", "E9-IDF"} <= names and not names & {"INET-RTR-1", "WLC-1", "LAB-SW-TEMP"}
                and data["summary"]["extra"] == 2 and data["ranges"] == "10.0.3.0/24, 10.0.5.0/24", data["summary"])
    names = {n["id"]: n["name"] for n in data["nodes"]}
    pairs = {(names[l["a"]], names[l["b"]]) if names[l["a"]] < names[l["b"]] else (names[l["b"]], names[l["a"]]): l
             for l in data["links"]}
    ok &= check("links between the switches on the list, the Aruba by LLDP, the Dell by MAC",
                ("CORE-1", "CORE-2") in pairs and ("C4-IDF", "CORE-1") in pairs
                and pairs[("B2-IDF", "CORE-1")]["mac"] and ("C5-IDF", "C5-IDF-2") in pairs
                and len(pairs) == 38 and ("B4-IDF", "CORE-1") in pairs, sorted(pairs))
    # (the pretend switches answer on 127.0.0.1, so CDP's 10.0.2.15 for the switch that refused the login doesn't
    # match its address on the list; its name on the list does)
    ok &= check("no passwords in the page", b"sw-pass" not in page.data and b"core-pass" not in page.data)

    print("\nUpdating the switch list from the map")
    sg = client.get("/api/network/suggestions", **base).json["suggestions"]
    b4_row = next(r for r in rows if r["name"] == "B4-IDF")
    ok &= check("found switches offered, and the one whose neighbors see it elsewhere",
                [(a["name"], a["status"]) for a in sg["add"]] == [("C5-IDF-2", "new"), ("E9-IDF", "new")]
                and [(f["name"], f["old"], f["new"], f["status"]) for f in sg["fix"]]
                == [("B4-IDF", b4_row["address"], "10.0.2.15", "ready")] and sg["page"] == job["report_file"], sg)
    before = client.get("/api/state", **base).json["switches"]
    r = client.post("/api/network/suggestions/apply", json={"add": [{"address": "10.0.5.19", "type": "", "building": "E"},
                                                                     {"address": "10.0.3.16"}, {"address": "10.9.9.9"}],
                                                             "fix": [{"old": "1.2.3.4", "new": "10.0.2.15"}]}, **base)
    ok &= check("added what was ticked; a type is still needed; things it didn't suggest ignored",
                r.json["added"] == ["C5-IDF-2"] and r.json["fixed"] == []
                and r.json["skipped"] == ["E9-IDF (10.0.5.19): pick its type."], r.json)
    r = client.post("/api/network/suggestions/apply", json={"add": [{"address": "10.0.5.19", "type": "cisco_ios",
                                                                      "building": "  E   Building "},
                                                                     {"address": "10.0.3.16", "type": "cisco_ios"}],
                                                             "fix": [{"old": b4_row["address"], "new": "10.0.2.15"}]},
                    **base)
    after = {s["address"]: s for s in client.get("/api/state", **base).json["switches"]}
    ok &= check("then the other one, and the moved switch's address; the one already added is not added twice",
                r.json["added"] == ["E9-IDF"] and r.json["fixed"] == [f"B4-IDF: {b4_row['address']} -> 10.0.2.15"]
                and r.json["skipped"] == ["C5-IDF-2 (10.0.3.16): that address is already on your list."]
                and len(after) == len(before) + 2, r.json)
    ok &= check("new rows: its name, type, the building it's in, the same connection as switches of its type",
                after["10.0.3.16"] == {"address": "10.0.3.16", "type": "cisco_ios", "protocol": "telnet",
                                       "building": "C Building", "location": "", "name": "C5-IDF-2"}
                and after["10.0.5.19"]["building"] == "E Building"
                and "10.0.2.15" in after and b4_row["address"] not in after
                and after["10.0.2.15"]["name"] == "B4-IDF" and after["10.0.2.15"]["location"] == b4_row["location"],
                (after.get("10.0.3.16"), after.get("10.0.5.19")))
    sg = r.json["suggestions"]
    ok &= check("and the ideas now say so", [a["status"] for a in sg["add"]] == ["on_list", "on_list"]
                and sg["fix"][0]["status"] == "done", sg)
    found = N.load_suggestions(webapp.NETWORK_DIR)
    found["add"].append({"address": "10.0.7.7", "name": "CORE-2", "model": "N7K-C7010", "type": "cisco_nxos",
                         "building": "", "seen_by": ["CORE-1"]})
    (webapp.NETWORK_DIR / "suggestions.json").write_text(json.dumps(found), encoding="utf-8")
    ok &= check("a name already on the list (at another address) isn't offered as new",
                client.get("/api/network/suggestions", **base).json["suggestions"]["add"][-1]["status"] == "name_on_list")
    r = client.post("/api/network/suggestions/apply", json={"add": [{"address": "10.0.7.7"}]}, **base)
    ok &= check("and isn't added", r.json["added"] == [] and "already on your list" in r.json["skipped"][0], r.json)

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
