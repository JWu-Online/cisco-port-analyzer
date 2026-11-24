"""
Port documentation test: reading MAC tables, and a full run through the web app.

    python tests/test_portdocs.py

Starts a pretend switch of each type (a Cisco stack and a Dell stack from the Stack
topology samples, a Nexus over SSH, an Aruba), plus one that rejects the login, then
makes the CSVs the way the page does and checks what's in them. No real switches needed.
"""

import csv
import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("PA_DATA_DIR", tempfile.mkdtemp(prefix="pa-docs-test-"))

from mock_switch import MockCLI, SSHSwitch, TelnetSwitch  # noqa: E402
import samples as S  # noqa: E402
import analyzer as A  # noqa: E402
import portdocs as P  # noqa: E402
import switches as S_  # noqa: E402
import topology as T  # noqa: E402
from topology_demo import demo_outputs, dell_demo_outputs  # noqa: E402
import app as webapp  # noqa: E402


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return condition


IOS_MACS = """          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
 All    0100.0ccc.cccc    STATIC      CPU
  20    0011.2233.4401    DYNAMIC     Gi1/0/1
  40    a4b1.c2d3.e401    DYNAMIC     Gi1/0/1
  30    0011.2233.4405    DYNAMIC     Gi1/0/5
  60    0011.2233.4428    DYNAMIC     Gi1/0/28
  99    00aa.bbcc.ddee    DYNAMIC     Po1
Total Mac Addresses for this criterion: 6
"""
CAT4500_MACS = """Unicast Entries
 vlan   mac address     type        protocols               port
-------+---------------+--------+---------------------+--------------------
  20    0011.2233.4488   dynamic ip,ipx,assigned,other GigabitEthernet2/14
  20    0011.2233.4499   dynamic ip,ipx,assigned,other Port-channel1
"""
NXOS_MACS = """Legend:
        * - primary entry, G - Gateway MAC, (R) - Routed MAC, O - Overlay MAC
   VLAN     MAC Address      Type      age     Secure NTFY Ports
---------+-----------------+--------+---------+------+----+------------------
*    1     0050.5689.0001   dynamic  0         F      F    Eth1/4
*   15     0050.5689.0011   dynamic  0         F      F    Eth1/1
G    -     002a.6a12.3456   static   -         F      F    sup-eth1(R)
"""
DELL_MACS = """Aging time is 300 sec

  Vlan        Mac Address         Port       Type
-------- --------------------- ---------- ----------
   20      00:11:22:33:44:08     gi1/0/8    dynamic
   40      a4:b1:c2:d3:e4:08     gi1/0/8    dynamic
   1       00:11:22:33:44:56     ch1        dynamic
"""
ARUBA_MACS = """MAC Address Table
-----------------
Destination Address  Address Type  VLAN  Destination Port
-------------------  ------------  ----  ----------------
00:0b:86:00:00:01    Learned       15    GE0/0/0
00:0b:86:00:00:02    Learned       1     pc0
"""


def mac_tables():
    ok = True
    print("\nReading MAC address tables")
    ios = P.parse_mac_table(IOS_MACS)
    ok &= check("Catalyst: per port with its VLAN; CPU and port-channel left out",
                ios[("gi", "1/0/1")] == [("0011.2233.4401", "20"), ("a4b1.c2d3.e401", "40")]
                and ("po", "1") not in ios and len(ios) == 3, ios)
    ok &= check("Catalyst 4500: long port names", P.parse_mac_table(CAT4500_MACS) == {("gi", "2/14"): [("0011.2233.4488", "20")]})
    nx = P.parse_mac_table(NXOS_MACS)
    ok &= check("Nexus: '*' column skipped, gateway entry left out",
                nx == {("et", "1/4"): [("0050.5689.0001", "1")], ("et", "1/1"): [("0050.5689.0011", "15")]}, nx)
    dell = P.parse_mac_table(DELL_MACS)
    ok &= check("Dell: colons turned into 0011.2233.4408, ch1 left out",
                dell == {("gi", "1/0/8"): [("0011.2233.4408", "20"), ("a4b1.c2d3.e408", "40")]}, dell)
    ok &= check("Aruba", P.parse_mac_table(ARUBA_MACS) == {("ge", "0/0/0"): [("000b.8600.0001", "15")]})
    ok &= check("just the MAC addresses, and how many",
                P.mac_columns("20", [("a4b1.c2d3.e401", "40"), ("0011.2233.4401", "20")])
                == ("0011.2233.4401, a4b1.c2d3.e401", 2))
    ok &= check("the same MAC on two VLANs counts once",
                P.mac_columns("20", [("0011.2233.4401", "20"), ("0011.2233.4401", "40")]) == ("0011.2233.4401", 1))
    ok &= check("no MACs -> blank and 0", P.mac_columns("20", []) == ("", 0))
    ok &= check("trunks just say so, with no count", P.mac_columns("trunk", [("0011.2233.4401", "20")]) == ("Trunk port", "")
                and P.mac_columns("routed", []) == ("Routed port", ""))
    ok &= check("Status in the switch's words; up/down switches get the same words",
                [P.status_word(p) for p in ({"status": "connected"}, {"status": "notconnect"}, {"status": "err-disabled"},
                                            {"status": "disabled"}, {"status": "sfpAbsent"}, {"status": "up"},
                                            {"status": "down"}, {"status": "up", "state": "connected"},
                                            {"status": "down", "state": "err-disabled"})]
                == ["connected", "notconnect", "err-disabled", "disabled", "sfpAbsent", "connected", "notconnect",
                    "connected", "err-disabled"])
    ok &= check("Nexus 'show interface' reasons -> the same words",
                [A.nxos_state(u, r) for u, r in (("up", ""), ("down", "Link not connected"), ("down", "Administratively down"),
                                                 ("down", "errDisabled"), ("down", "Error disabled"), ("down", "XCVR not inserted"),
                                                 ("down", "SFP not inserted"), ("down", "Inactive"))]
                == ["connected", "notconnect", "disabled", "err-disabled", "err-disabled", "xcvrAbsent", "sfpAbsent",
                    "notconnect"])
    ok &= check("SNMP location from the config, 'show snmp', a Dell's 'show system', or bare",
                [S_.parse_location(t, b) for t, b in (("snmp-server location E Building 3rd floor\n", False),
                                                      ('snmp-server location "B2 IDF"', False),
                                                      ("sys contact: noc\nsys location: Main MDF", False),
                                                      ("System Location:          B Building", False),
                                                      ('syslocation "M2 closet"', False), ("Library 1st fl", True),
                                                      ("%SNMP agent not enabled", True), ("", False),
                                                      ("System Location:", False))]
                == ["E Building 3rd floor", "B2 IDF", "Main MDF", "B Building", "M2 closet", "Library 1st fl", "", "", ""])
    ok &= check("network-policy profiles: their voice VLAN number, from the show command or the config",
                P.parse_network_policies("Network Policy Profile 10\n   voice vlan 40 cos 5\n   voice-signaling vlan 41\n"
                                         "  Interface:\n   GigabitEthernet1/0/5\nNetwork Policy Profile 20\n"
                                         "   voice vlan dot1p cos 5\n") == {"10": "40"}
                and P.parse_network_policies("network-policy profile 3\n voice vlan 45 cos 5\n voice-signaling vlan 45\n"
                                             "network-policy profile 7\n voice vlan 46\n") == {"3": "45", "7": "46"}
                and P.parse_network_policies("% Invalid input detected at '^' marker.") == {})
    ok &= check("a port's 'network-policy 10' is read from its config",
                T.parse_profile(["switchport access vlan 30", "network-policy 10"])["network_policy"] == "10")
    text = P.csv_text([{"description": "=HYPERLINK(\"x\")", "vlan": "20", "tagged": "40 VOICE-PHONES"}])
    ok &= check("text Excel would run as a formula is made harmless", "'=HYPERLINK" in text, text)
    return ok


def pretend_switches():
    """One pretend switch of each type, plus one with another password. -> (clis, addresses, switch list)"""
    demo, cmds = demo_outputs(), {}
    for key, cmd in T.COMMANDS.items():
        if isinstance(cmd, tuple):
            cmds[cmd[0]] = demo[key]
        elif key in demo:
            cmds[cmd] = demo[key]
    cmds["show mac address-table dynamic"] = IOS_MACS
    cmds["show running-config | include snmp-server location"] = "snmp-server location E Building 3rd floor E-310\n"
    # Gi1/0/5 gets its voice VLAN from a network-policy profile instead of 'switchport voice vlan'
    cmds[T.COMMANDS["config"][0]] = demo["config"].replace(
        "interface GigabitEthernet1/0/5\n description Lab E-310 PC05\n",
        "interface GigabitEthernet1/0/5\n description Lab E-310 PC05\n network-policy 10\n")
    cmds["show network-policy profile"] = ("Network Policy Profile 10\n   voice vlan 40 cos 5\n  Interface:\n"
                                           "   GigabitEthernet1/0/5\nNetwork Policy Profile 20\n   voice vlan dot1p cos 5\n"
                                           "  Interface:\n   none\n")
    ios_cli = MockCLI("DEMO-IDF-E3", cmds, start_enabled=False, enable_secret="en-secret")
    ddemo, dcmds = dell_demo_outputs(), {}
    for key, cmd in T.DELL_COMMANDS.items():
        dcmds[cmd[0] if isinstance(cmd, tuple) else cmd] = ddemo[key]
    dcmds["show mac address-table dynamic"] = DELL_MACS
    dell_cli = MockCLI("DEMO-B2-DELL", dcmds, start_enabled=False, enable_secret="en-secret")
    nxos_cli = MockCLI("LGCC-N7K", {**S.OUTPUTS["cisco_nxos"], "show mac address-table dynamic": NXOS_MACS,
                                    "show running-config snmp": "snmp-server contact noc\n"
                                                                "snmp-server location M Building Main MDF\n",
                                    "show vlan brief": "VLAN Name                             Status    Ports\n"
                                                       "---- -------------------------------- --------- -------\n"
                                                       "1    default                          active    Eth1/2\n"
                                                       "15   LAB                              active    \n"})
    aruba_cli = MockCLI("S3500-M205", {**S.OUTPUTS["aruba_mas"], "show mac-address-table": ARUBA_MACS},
                        start_enabled=False, enable_secret="en-secret", prompt_style="aruba")
    servers = {
        "ios": TelnetSwitch(ios_cli, "netops", "sw-pass"),
        "dell": TelnetSwitch(dell_cli, "netops", "sw-pass", user_prompt="User Name:"),
        "nxos": SSHSwitch(nxos_cli, "admin", "core-pass"),
        "aruba": TelnetSwitch(aruba_cli, "netops", "sw-pass", user_prompt="User: "),
        "bad": TelnetSwitch(MockCLI("B-IDF3-SW3", {}), "netops", "another-password"),
    }
    addr = {k: f"127.0.0.1:{s.port}" for k, s in servers.items()}
    clis = {"ios": ios_cli, "dell": dell_cli, "nxos": nxos_cli, "aruba": aruba_cli, "_servers": servers}
    return clis, addr, [
        {"address": addr["ios"], "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E3 IDF",
         "name": "E3-STACK"},
        {"address": addr["dell"], "type": "5548P", "protocol": "telnet", "building": "B Building", "location": "B2 IDF"},
        {"address": addr["nxos"], "type": "N7k", "protocol": "ssh", "building": "M Building", "location": ""},
        {"address": addr["aruba"], "type": "S3500", "protocol": "telnet", "building": "M Building", "location": "M2 IDF"},
        {"address": addr["bad"], "type": "cisco_ios", "protocol": "telnet", "building": "B Building", "location": "B3 IDF"},
    ]


def full_run():
    ok = True
    print("\nMaking CSVs through the web app")
    clis, addr, switch_list = pretend_switches()
    ios_cli, nxos_cli, aruba_cli = clis["ios"], clis["nxos"], clis["aruba"]
    demo, ddemo = demo_outputs(), dell_demo_outputs()
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": switch_list}, **base)
    client.post("/api/settings", json={"default_protocol": "telnet", "parallel": 5, "timeout": 10}, **base)
    login = {"username": "netops", "password": "sw-pass", "secret": "en-secret", "ssh_different": True,
             "ssh_username": "admin", "ssh_password": "core-pass"}

    ok &= check("page loads, and the main page has a button for it",
                client.get("/port-docs", **base).status_code == 200
                and b'href="/port-docs"' in client.get("/", **base).data)
    r = client.post("/api/docs/run", json={"scope": "", "months": 6, "username": "netops"}, **base)
    ok &= check("no password -> asked for it", r.status_code == 400 and "password" in r.json["problems"][0].lower(), r.json)
    r = client.post("/api/docs/run", data="{}", **base)
    ok &= check("non-JSON posts refused", r.status_code == 415)

    r = client.post("/api/docs/run", json={"scope": "", "months": 6, **login}, **base)
    ok &= check("run starts", r.status_code == 200, r.json)
    started = time.time()
    while client.get("/api/docs/job", **base).json["state"] == "running" and time.time() - started < 240:
        time.sleep(0.3)
    job = client.get("/api/docs/job", **base).json
    log = [ln["msg"] for ln in job["lines"]]
    ok &= check("run finished", job["state"] == "done" and bool(job["report_file"]), log)
    ok &= check("the switch with the wrong password is reported, the rest aren't",
                sum("Couldn't document" in m for m in log) == 1
                and any("Couldn't document" in m and addr["bad"] in m for m in log), log)

    runs = client.get("/api/docs/runs", **base).json["runs"]
    run = runs[0]
    ok &= check("run listed with its files and the failed switch",
                run["run"] == job["report_file"] and run["switches"] == 4 and len(run["files"]) == 4
                and run["failed"][0]["address"] == addr["bad"], run)

    r = client.get(f"/port-docs/download/{run['run']}/all", **base)
    ok &= check("one CSV, every switch: downloads with a BOM for Excel",
                r.status_code == 200 and r.data.startswith(b"\xef\xbb\xbf")
                and "attachment" in r.headers["Content-Disposition"], r.status_code)
    rows = list(csv.DictReader(io.StringIO(r.data.decode("utf-8-sig"))))
    ok &= check("the columns", list(rows[0]) == [c[0] for c in P.COLUMNS], list(rows[0]))
    by = {(row["Switch"], row["Interface"]): row for row in rows}
    stack, _ = T.build_stack(demo, addr["ios"], 6)
    dstack, _ = T.build_stack(ddemo, addr["dell"], 6, vendor="dell_powerconnect")
    expected = sum(1 for d in (stack, dstack) for m in d["members"] for p in m["ports"] if not p.get("absent"))
    ok &= check("a row for every front port of both stacks, plus every Nexus and Aruba port",
                len(rows) == expected + 4 + 4, (len(rows), expected))

    gi5 = by.get(("DEMO-IDF-E3", "Gi1/0/5"), {})
    ok &= check("Cisco port with a network-policy profile: its voice VLAN, just the number",
                gi5.get("VLAN") == "30" and gi5.get("Tagged VLANs") == "40", gi5)
    ok &= check("ports without one stay as they were",
                sum(1 for k, r in by.items() if k[0] == "DEMO-IDF-E3" and r["Tagged VLANs"] == "40") == 1)
    gi1 = by.get(("DEMO-IDF-E3", "Gi1/0/1"), {})
    ok &= check("Cisco desk port: switch, member, port, description, VLAN name, voice VLAN, MACs",
                gi1.get("Address") == addr["ios"] and gi1.get("Building") == "E Building"
                and gi1.get("Member/slot") == "1" and gi1.get("Port") == "1"
                and gi1.get("Description") == "Rm E-310 Desk 2" and gi1.get("VLAN") == "20"
                and gi1.get("VLAN name") == "STAFF-DATA" and gi1.get("Tagged VLANs") == "40 VOICE-PHONES"
                and gi1.get("MAC addresses") == "0011.2233.4401, a4b1.c2d3.e401" and gi1.get("MAC count") == "2"
                and gi1.get("Result") == "Active" and gi1.get("Status") == "connected" and gi1.get("Speed") == "1000", gi1)
    statuses = {row["Status"] for row in rows}
    ok &= check("Status column in the switches' own words", statuses ==
                {"connected", "notconnect", "disabled", "err-disabled", "xcvrAbsent"}, statuses)
    nx_status = {row["Interface"]: row["Status"] for row in rows if row["Switch"] == "LGCC-N7K"}
    ok &= check("Nexus: up, no link, shut down, no transceiver", nx_status ==
                {"Eth1/1": "connected", "Eth1/2": "notconnect", "Eth1/3": "disabled", "Eth1/4": "xcvrAbsent"}, nx_status)
    locs = {row["Switch"]: (row["Location"], row["SNMP location"]) for row in rows}
    ok &= check("SNMP location column from each switch; Location from the list, or the switch's when the list's is "
                "empty", locs == {"DEMO-IDF-E3": ("E3 IDF", "E Building 3rd floor E-310"),
                                  "DEMO-B2-DELL": ("B2 IDF", "B Building"),
                                  "LGCC-N7K": ("M Building Main MDF", "M Building Main MDF"),
                                  "S3500-M205": ("M2 IDF", "")}, locs)
    saved = {s["address"]: (s["name"], s["location"]) for s in client.get("/api/state", **base).json["switches"]}
    ok &= check("empty names and locations on the list filled in from the switches; typed ones left alone",
                saved == {addr["ios"]: ("E3-STACK", "E3 IDF"), addr["dell"]: ("DEMO-B2-DELL", "B2 IDF"),
                          addr["nxos"]: ("LGCC-N7K", "M Building Main MDF"), addr["aruba"]: ("S3500-M205", "M2 IDF"),
                          addr["bad"]: ("", "B3 IDF")}, saved)
    ok &= check("the log says what it filled in",
                any("Filled in 3 names and 1 location on your switch list" in m for m in log), log)
    trunk = by.get(("DEMO-IDF-E3", "Gi1/0/28"), {})
    ok &= check("Cisco trunk: VLAN says Trunk, MAC column says Trunk port, its VLANs listed",
                trunk.get("VLAN") == "Trunk" and trunk.get("MAC addresses") == "Trunk port" and trunk.get("MAC count") == ""
                and bool(trunk.get("Tagged VLANs")), trunk)
    problem = by.get(("DEMO-IDF-E3", "Gi1/0/5"), {})
    ok &= check("Cisco issues and power carried over", problem.get("Issues") == "Receive errors", problem)
    phone = next((row for row in rows if row["Switch"] == "DEMO-IDF-E3" and row["Power"].startswith("IP Phone")), {})
    ok &= check("what a port powers", phone.get("Power", "").startswith("IP Phone 8845 · 5.9 W"), phone)
    dell8 = by.get(("DEMO-B2-DELL", "gi1/0/8"), {})
    ok &= check("Dell desk port: PVID 20, voice VLAN tagged, both MACs",
                dell8.get("VLAN") == "20" and dell8.get("Tagged VLANs") == "40 VOICE-PHONES"
                and dell8.get("MAC addresses") == "0011.2233.4408, a4b1.c2d3.e408" and dell8.get("MAC count") == "2", dell8)
    nx1 = by.get(("LGCC-N7K", "Eth1/1"), {})
    nx4 = by.get(("LGCC-N7K", "Eth1/4"), {})
    ok &= check("Nexus: trunk port, and an access port's MAC and VLAN name",
                nx1.get("VLAN") == "Trunk" and nx1.get("MAC addresses") == "Trunk port"
                and nx4.get("MAC addresses") == "0050.5689.0001" and nx4.get("MAC count") == "1" and nx4.get("Port") == "4"
                and nx4.get("Member/slot") == "1" and nx4.get("Building") == "M Building", (nx1, nx4))
    ar0 = by.get(("S3500-M205", "GE0/0/0"), {})
    ok &= check("Aruba: VLAN name from 'show vlan', MAC, result",
                ar0.get("VLAN") == "15" and ar0.get("VLAN name") == "LAB" and ar0.get("MAC addresses") == "000b.8600.0001"
                and ar0.get("Result") == "Active" and ar0.get("Port") == "0", ar0)
    ok &= check("no passwords or config lines in the CSV", b"sw-pass" not in r.data and b"FAKE" not in r.data)

    r = client.get(f"/port-docs/download/{run['run']}/zip", **base)
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist() if r.status_code == 200 else []
    ok &= check("one CSV per switch in a .zip, named after the switch",
                sorted(names) == ["DEMO-B2-DELL.csv", "DEMO-IDF-E3.csv", "LGCC-N7K.csv", "S3500-M205.csv"], names)
    one = zipfile.ZipFile(io.BytesIO(r.data)).read("LGCC-N7K.csv").decode("utf-8-sig")
    ok &= check("each switch's file has only its own ports", len(one.strip().splitlines()) == 1 + 4, one)
    ok &= check("other runs and paths refused",
                client.get("/port-docs/download/../../etc/all", **base).status_code == 404
                and client.get(f"/port-docs/download/{run['run']}/run.json", **base).status_code == 404
                and client.get("/port-docs/download/20990101-000000/all", **base).status_code == 404)
    ok &= check("MAC table read with one login per switch",
                ios_cli.commands.count("show mac address-table dynamic") == 1
                and "show mac-address-table" in aruba_cli.commands and "show vlan brief" in nxos_cli.commands)
    hist = T.load_history(webapp.TOPOLOGY_DIR, addr["ios"])
    ok &= check("the stacks' history is shared with the Stack topology page", hist["runs"] == 1, hist.get("runs"))
    return ok


def main():
    ok = mac_tables()
    ok &= full_run()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
