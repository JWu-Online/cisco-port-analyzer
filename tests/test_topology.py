"""
Stack topology test: sample output from each kind of Catalyst, plus a pretend
stack served over telnet and drawn through the real web app.

    python tests/test_topology.py

No real switches needed.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("PA_DATA_DIR", tempfile.mkdtemp(prefix="pa-topo-test-"))

from mock_switch import MockCLI, TelnetSwitch  # noqa: E402
import topology as T  # noqa: E402
from topology_demo import demo_outputs, dell_demo_outputs  # noqa: E402
import samples as S  # noqa: E402
import app as webapp  # noqa: E402

NOW = datetime(2026, 11, 16, 9, 0)
SIX_MONTHS = timedelta(days=6 * T.DAYS_PER_MONTH)


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return condition


# Catalyst 3750-X, classic IOS: "Master" role, no Up Time column, member 2 rebooted 22 weeks ago
C3750 = {
    "switch": """Switch/Stack Mac Address : 0019.e7b1.2300
                                           H/W   Current
Switch#  Role   Mac Address     Priority Version  State
----------------------------------------------------------
*1       Master 0019.e7b1.2300     15     0       Ready
 2       Member 001b.d4c3.9a80     1      0       Ready
""",
    "version": """Cisco IOS Software, C3750E Software (C3750E-UNIVERSALK9-M), Version 15.0(2)SE11
LAB-3750 uptime is 2 years, 10 weeks, 4 days, 1 hour, 22 minutes

Switch Ports Model              SW Version            SW Image
------ ----- -----              ----------            ----------
*    1 54    WS-C3750X-48P      15.0(2)SE11           C3750E-UNIVERSALK9-M
     2 30    WS-C3750X-24P      15.0(2)SE11           C3750E-UNIVERSALK9-M


Switch 02
---------
Switch Uptime                   : 22 weeks, 1 day, 3 hours, 12 minutes
Model number                    : WS-C3750X-24P-S
""",
    "link": """
Port      Name               Down Time
Gi1/0/1   Rm 2 desk 6w3d     00:00:00
Gi1/0/2                      2y10w
Gi1/0/3   printer            6w3d
Gi2/0/1                      22w1d
""",
    "status": """
Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   Rm 2 desk 6w3d     connected    20         a-full a-1000 10/100/1000BaseTX
Gi1/0/2                      notconnect   30           auto   auto 10/100/1000BaseTX
Gi1/0/3   printer connected  notconnect   50           auto   auto 10/100/1000BaseTX
Gi1/0/4   AP trunk           connected    trunk      a-full a-1000 10/100/1000BaseTX
Gi1/0/5                      err-disabled 20           auto   auto 10/100/1000BaseTX
Gi1/1/1                      notconnect   1            auto   auto Not Present
Te1/1/1   UPLINK             connected    trunk        full    10G SFP-10GBase-SR
Gi2/0/1                      disabled     30           auto   auto 10/100/1000BaseTX
""",
    "interfaces": """GigabitEthernet1/0/1 is up, line protocol is up (connected)
  Description: Rm 2 desk 6w3d
  Last input never, output 00:00:01, output hang never
GigabitEthernet1/0/2 is down, line protocol is down (notconnect)
  Last input never, output never, output hang never
GigabitEthernet1/0/3 is down, line protocol is down (notconnect)
  Description: printer connected
  Last input never, output 6w3d, output hang never
GigabitEthernet1/0/4 is up, line protocol is up (connected)
  Last input 00:00:00, output 00:00:00, output hang never
GigabitEthernet1/0/5 is down, line protocol is down (err-disabled)
  Last input 1d02h, output 1d02h, output hang never
GigabitEthernet2/0/1 is administratively down, line protocol is down (disabled)
  Last input never, output never, output hang never
""",
}


def parsers():
    ok = True
    print("\nReading each command")
    ok &= check("show switch: 'Master' is the active switch",
                T.parse_show_switch(C3750["switch"])[1]["role"] == "Active")
    v = T.parse_version(C3750["version"])
    ok &= check("show version: hostname, models, member uptime",
                v["hostname"] == "LAB-3750" and v["members"][1]["model"] == "WS-C3750X-48P"
                and v["members"][2]["uptime"] == timedelta(weeks=22, days=1, hours=3, minutes=12), v)
    link = T.parse_link(C3750["link"])
    ok &= check("show interfaces link: a time in the description isn't the down time",
                link == {"Gi1/0/1": "00:00:00", "Gi1/0/2": "2y10w", "Gi1/0/3": "6w3d", "Gi2/0/1": "22w1d"}, link)
    st = T.parse_status(C3750["status"])
    ok &= check("show interfaces status: 'connected' in a description isn't the status",
                st["Gi1/0/3"]["status"] == "notconnect" and st["Gi1/0/3"]["vlan"] == "50"
                and st["Gi1/0/4"]["vlan"] == "trunk", st["Gi1/0/3"])
    names = T.parse_vlan_names("""
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/1, Gi1/0/2, Gi1/0/3
                                                Gi1/0/4
20   STAFF-DATA                       active    Gi1/0/5
85   VLAN0085                         active
99   Net Mgmt                         active
1002 fddi-default                     act/unsup
""")
    ok &= check("show vlan brief: names read, wrapped port lines skipped, unnamed VLAN0085 left out",
                names.get("20") == "STAFF-DATA" and names.get("99") == "Net Mgmt" and "85" not in names, names)
    cfg = T.parse_interface_configs("""Building configuration...
!
hostname LAB
username netops privilege 15 secret 9 $9$FAKEHASH
!
interface GigabitEthernet1/0/1
 description Rm 2 password-reset kiosk
 switchport access vlan 20
 ip ospf message-digest-key 1 md5 7 0822455D0A16
!
interface GigabitEthernet1/0/2
!
line vty 0 4
 password 7 FAKE
end
""")
    ok &= check("running-config: only interface sections kept",
                set(cfg) == {"Gi1/0/1", "Gi1/0/2"} and cfg["Gi1/0/1"]["ifname"] == "GigabitEthernet1/0/1"
                and cfg["Gi1/0/2"]["lines"] == [], cfg)
    ok &= check("running-config: a port line with a key is left out, a description isn't",
                cfg["Gi1/0/1"]["removed"] == 1 and "0822455D0A16" not in str(cfg)
                and " description Rm 2 password-reset kiosk" in cfg["Gi1/0/1"]["lines"], cfg["Gi1/0/1"])
    ok &= check("down times keep how precise they are",
                T.parse_cisco_duration("26w3d") == (timedelta(weeks=26, days=3), timedelta(days=1))
                and T.parse_cisco_duration("never") == (None, None))

    print("\nDeciding Active / Inactive / Unsure")
    data, _ = T.build_stack(C3750, "lab", 6, now=NOW)
    ports = {p["name"]: p for m in data["members"] for p in m["ports"] if not p.get("absent")}
    ok &= check("only front-panel ports 1-48 (no uplink module, no Te1/1/1)",
                set(ports) == {"Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4", "Gi1/0/5", "Gi2/0/1"}, sorted(ports))
    ok &= check("member sizes come from the model (48 and 24)",
                [m["port_count"] for m in data["members"]] == [48, 24])
    ok &= check("link up now -> Active", ports["Gi1/0/1"]["activity"] == "active")
    ok &= check("down since boot on a 2-year-old switch -> Inactive", ports["Gi1/0/2"]["activity"] == "inactive",
                ports["Gi1/0/2"])
    ok &= check("went down 6 weeks ago -> Active", ports["Gi1/0/3"]["activity"] == "active", ports["Gi1/0/3"])
    ok &= check("err-disabled yesterday -> Active, flagged", ports["Gi1/0/5"]["activity"] == "active"
                and ports["Gi1/0/5"]["err"])
    ok &= check("member up only 22 weeks -> Unsure, not Inactive", ports["Gi2/0/1"]["activity"] == "unsure"
                and ports["Gi2/0/1"]["admin_down"], ports["Gi2/0/1"])

    # 2960-X: Gi1/0/1-52 and uplinks Te1/0/1-2 share port numbers 1 and 2
    status = "\n".join(
        ["Port      Name               Status       Vlan       Duplex  Speed Type"]
        + [f"Gi1/0/{i:<6}                   connected    10         a-full a-1000 10/100/1000BaseTX" for i in range(1, 53)]
        + ["Te1/0/1                      connected    trunk        full    10G SFP-10GBase-SR"])
    d2, _ = T.build_stack({"status": status, "version": "S uptime is 1 year, 2 weeks"}, "x", 6, now=NOW)
    names = [p["name"] for p in d2["members"][0]["ports"]]
    ok &= check("2960-X: access ports win over Te1/0/1-2, stops at 48",
                names[0] == "Gi1/0/1" and names[-1] == "Gi1/0/48" and len(names) == 48, names[:2])

    print("\nHistory across reboots")
    h = T.empty_history("x")
    T.update_coverage(h, 1, NOW - timedelta(days=60), timedelta(days=365))
    ok &= check("a reboot with no run in between leaves a blind spot",
                T.update_coverage(h, 1, NOW, timedelta(days=7)) == NOW - timedelta(days=7))
    h = T.empty_history("y")
    for day in range(200):
        when = NOW - timedelta(days=200 - day)
        since = T.update_coverage(h, 1, when, timedelta(days=5 + day) if day < 100 else timedelta(hours=12 + (day - 100) * 24))
    ok &= check("daily runs carry the evidence across a reboot", NOW - since > SIX_MONTHS, since)
    act, _, _ = T.evaluate_port(NOW, SIX_MONTHS, 6, False, "3w2d", timedelta(days=23.2), None, "",
                                datetime(2026, 9, 1), NOW - timedelta(days=23))
    ok &= check("seen up by an earlier run -> Active even after a reboot", act == "active")

    print("\nStack cables")
    links = T._stack_links(T.parse_two_port_table("1 2 2\n2 1 1"), T.parse_two_port_table("1 OK OK\n2 OK OK"))
    ok &= check("2-member stack: two cables between the same pair", len(links) == 2, links)
    d, _ = T.build_stack(demo_outputs(), "demo", 6)
    ok &= check("broken ring is the first warning", d["warnings"][0].startswith("Stack ring is broken"))
    ok &= check("demo: Switch 3 restarted 3 weeks ago, so its idle ports are Unsure",
                d["members"][2]["counts"]["unsure"] > 0 and d["members"][0]["counts"]["unsure"] == 0)

    print("\nThe page")
    evil = dict(demo_outputs())
    evil["interfaces"] = evil["interfaces"].replace("Description: Rm E-310 Desk 2",
                                                    "Description: </script><img src=x onerror=alert(1)>", 1)
    html = T.render_html(T.build_stack(evil, "x", 6)[0])
    ok &= check("a port description can't break out of the page's data", "</script><img" not in html)
    ok &= check("print sheet: button, black-and-white print styles, port checklist",
                'id="print"' in html and "@media print" in html and 'id="print-list"' in html)
    ok &= check("print sheet keeps the black boxes even with 'Background graphics' off",
                "print-color-adjust: exact" in html)
    ok &= check("move list: button, dock, script/CSV/print outputs",
                all(f'id="{i}"' in html for i in ("move-btn", "dock", "mv-start", "mv-copy", "mv-csv", "mv-print", "move-print")))
    d, _ = T.build_stack(demo_outputs(), "x", 6)
    ok &= check("VLAN names go to the page, only for VLANs on these ports",
                d["vlan_names"].get("20") == "STAFF-DATA" and "1002" not in d["vlan_names"]
                and "85" not in d["vlan_names"], d["vlan_names"])
    return ok


def dell_and_translation():
    ok = True
    print("\nDell PowerConnect 5500")
    names, untagged, tagged = T.parse_dell_vlans(S.DELL_VLAN)
    ok &= check("show vlan: names and untagged ports (repo sample)",
                names == {"131": "Staff"} and untagged.get("Gi1/0/2") == {"131"} and untagged.get("Gi1/0/30") == {"1"}
                and not tagged, (names, untagged.get("Gi1/0/2"), tagged))
    shifted = "\n".join(ln if not ln.strip()[:1].isdigit() else "  " + ln for ln in S.DELL_VLAN.splitlines())
    ok &= check("show vlan: still read when the columns don't line up",
                T.parse_dell_vlans(shifted)[1].get("Gi1/0/2") == {"131"})
    totals = T.dell_packet_totals(S.DELL_COUNTERS)
    ok &= check("show interfaces counters: packets only, not bytes",
                totals["Gi1/0/1"] == 120931 + 2310 + 5512 + 100231 + 9123 + 12312 and totals["Gi1/0/2"] == 0, totals)
    system = T.parse_dell_system(S.DELL_SYSTEM)
    ok &= check("show system: name, uptime, model", system["hostname"] == "B218-5548P"
                and abs(system["uptime"].days - 412) <= 1 and system["model"] == "PowerConnect 5548P", system)
    cfg = T.parse_interface_configs("""config-file-header
voice vlan id 40
username admin password encrypted FAKEHASH privilege 15
interface vlan 20
name STAFF
exit
interface gigabitethernet1/0/1
description "Rm 101 Desk"
switchport access vlan 20
voice vlan enable
exit
interface range gigabitethernet1/0/2-4
spanning-tree portfast
exit
interface gigabitethernet1/0/5
switchport mode general
switchport general pvid 60
switchport general allowed vlan add 60 untagged
switchport general allowed vlan add 99 tagged
power inline never
exit
snmp-server community FAKECOMM ro
""")
    ok &= check("Dell running-config: 'exit' blocks, ranges, nothing outside the ports",
                set(cfg) == {"Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4", "Gi1/0/5"}
                and cfg["Gi1/0/3"]["lines"] == ["spanning-tree portfast"] and "FAKE" not in str(cfg), cfg)
    prof = T.parse_profile(cfg["Gi1/0/1"]["lines"], voice_id="40")
    lines, notes = T.render_profile(prof, "cisco_ios")
    ok &= check("Dell -> Cisco: description unquoted, voice VLAN from the switch-wide id",
                lines == [" description Rm 101 Desk", " switchport mode access", " switchport access vlan 20",
                          " switchport voice vlan 40"] and not notes, (lines, notes))
    lines, notes = T.render_profile(T.parse_profile(cfg["Gi1/0/5"]["lines"]), "cisco_ios")
    ok &= check("Dell general mode -> Cisco trunk, with notes for it and the PoE line",
                " switchport mode trunk" in lines and " switchport trunk native vlan 60" in lines
                and " switchport trunk allowed vlan 60,99" in lines
                and any("general" in n for n in notes) and any("power inline never" in n for n in notes), (lines, notes))
    cisco = [" description " + "x" * 70, " switchport access vlan 30", " switchport mode access",
             " switchport voice vlan 40", " spanning-tree portfast edge", " power inline never"]
    lines, notes = T.render_profile(T.parse_profile(cisco), "dell_powerconnect")
    ok &= check("Cisco -> Dell: no indent, 64-character description, voice VLAN and PoE noted",
                lines[0] == "description " + "x" * 64 and "switchport access vlan 30" in lines
                and "spanning-tree portfast" in lines and len(notes) == 3, (lines, notes))
    copies = T.port_copies("cisco_ios", cisco, T.parse_profile(cisco))
    ok &= check("same brand: lines copied exactly", copies["cisco_ios"]["lines"] == cisco)

    print("\nDell: deciding from packet counts, across drawings")
    status = """Port     Type         Duplex  Speed Neg      ctrl State       Pressure Mode
-------- ------------ ------  ----- -------- ---- ----------- -------- -------
gi1/0/1  1G-Copper    Full    1000  Enabled  Off  Up          Disabled On
gi1/0/2  1G-Copper      --      --     --     --  Down           --     --
gi1/0/3  1G-Copper      --      --     --     --  Down           --     --
"""
    def counters(a, b):
        return ("    Port      InUcastPkts  InMcastPkts  InBcastPkts    InOctets\n"
                f"  gi1/0/1      500 0 0 9999\n  gi1/0/2      {a} 0 0 9999\n  gi1/0/3      {b} 0 0 0\n")
    def draw(when, uptime_days, a, b, history):
        out = {"system": f"System Up Time (days,hour:min:sec):       {uptime_days},00:00:00\nSystem Name: LAB",
               "status": status, "counters": counters(a, b), "config": "interface gigabitethernet1/0/9\nexit"}
        data, history = T.build_stack(out, "lab", 6, history, now=when, vendor="dell_powerconnect")
        return {p["name"]: p for p in data["members"][0]["ports"] if not p.get("absent")}, history
    t0 = datetime(2026, 1, 5, 9, 0)
    first, hist = draw(t0, 400, 1234, 0, None)
    ok &= check("first drawing: up -> Active, old traffic -> Unsure, never any -> Inactive",
                (first["gi1/0/1"]["activity"], first["gi1/0/2"]["activity"], first["gi1/0/3"]["activity"])
                == ("active", "unsure", "inactive"), {k: (v["activity"], v["why"]) for k, v in first.items()})
    later, hist2 = draw(t0 + timedelta(days=200), 600, 1234, 0, json_copy(hist))
    ok &= check("200 days later, count never moved -> Inactive",
                later["gi1/0/2"]["activity"] == "inactive" and "since 2026-01-05" in later["gi1/0/2"]["why"],
                later["gi1/0/2"])
    moved, _ = draw(t0 + timedelta(days=100), 500, 1300, 0, json_copy(hist))
    ok &= check("100 days later, count went up -> Active", moved["gi1/0/2"]["activity"] == "active", moved["gi1/0/2"])
    moved, _ = draw(t0 + timedelta(days=200), 600, 1300, 0, json_copy(hist))
    ok &= check("200 days later, count went up -> Unsure (could have been 199 days ago)",
                moved["gi1/0/2"]["activity"] == "unsure" and "went up" in moved["gi1/0/2"]["why"], moved["gi1/0/2"])
    soon, _ = draw(t0 + timedelta(days=30), 430, 1234, 0, json_copy(hist))
    ok &= check("30 days later, count never moved -> still Unsure", soon["gi1/0/2"]["activity"] == "unsure")
    rebooted, _ = draw(t0 + timedelta(days=200), 10, 0, 0, json_copy(hist))
    ok &= check("after a reboot, no traffic yet -> Unsure, not Inactive",
                rebooted["gi1/0/2"]["activity"] == "unsure", rebooted["gi1/0/2"])
    ok &= check("ports with no config lines aren't 'basic' (a Dell only lists ports that have settings)",
                not first["gi1/0/2"]["basic"])
    return ok


def chassis():
    ok = True
    print("\nChassis (Catalyst 4507) and fixed switches with two-number port names")
    status = ["Port      Name               Status       Vlan       Duplex  Speed Type",
              "Te1/1     UPLINK-CORE        connected    trunk        full    10G 10GBase-SR",
              "Te1/2                        notconnect   1            full    10G 10GBase-SR",
              "Gi1/3                        notconnect   1            full   1000 No Gbic"]
    intf = []
    for slot in (2, 3):
        for port in range(1, 49):
            up = (port % 3) != 0
            status.append(f"Gi{slot}/{port:<7}{'B127 Desk ' + str(port) if up else '':<19}"
                          f"{'connected' if up else 'notconnect':<13}{'20' if slot == 2 else '30':<11}"
                          f"{'a-full' if up else 'auto':>6} {'a-1000' if up else 'auto':>6} 10/100/1000-TX")
            intf += [f"GigabitEthernet{slot}/{port} is {'up' if up else 'down'}, line protocol is "
                     f"{'up (connected)' if up else 'down (notconnect)'}",
                     f"  Last input never, output {'00:00:01' if up else 'never'}, output hang never"]
    outputs = {
        "switch": "", "neighbors": "", "stack_ports": "", "link": "",
        "version": "Cisco IOS Software, IOS-XE Software, Catalyst 4500 L3 Switch Software\n"
                   "B127_4507 uptime is 2 years, 14 weeks, 3 days, 2 hours, 11 minutes\n"
                   "cisco WS-C4507R+E (MPC8572) processor (revision 10) with 2097152K/20480K bytes of memory.",
        "modules": """Chassis Type : WS-C4507R+E

Power consumed by backplane : 40 Watts

Mod Ports Card Type                              Model              Serial No.
---+-----+--------------------------------------+------------------+-----------
 1     6  Sup 7-E 10GE (SFP+), 1000BaseX (SFP)   WS-X45-SUP7-E      CAT1111111
 2    48  10/100/1000BaseT Premium POE E Series  WS-X4748-RJ45V+E   CAT2222222
 3    48  10/100/1000BaseT Premium POE E Series  WS-X4748-RJ45V+E   CAT3333333

 M MAC addresses                    Hw  Fw           Sw               Status
--+--------------------------------+---+------------+----------------+---------
 1 0021.d8fd.3a00 to 0021.d8fd.3a05 1.2 15.0(1r)SG10 03.06.08.E        Ok
""",
        "status": "\n".join(status), "interfaces": "\n".join(intf),
        "config": "interface GigabitEthernet2/1\n description B127 Desk 1\n switchport access vlan 20\n!\n",
    }
    data, _ = T.build_stack(outputs, "10.0.4.45", 6, now=NOW)
    ok &= check("4507: drawn by slot, not empty", data["unit"] == "Slot" and [m["number"] for m in data["members"]] == [1, 2, 3],
                [(m["number"], m["port_count"]) for m in data["members"]])
    m2 = data["members"][1]
    ok &= check("4507: line cards get 48 ports and their model; the supervisor its few",
                m2["port_count"] == 48 and m2["model"] == "WS-X4748-RJ45V+E" and data["members"][0]["port_count"] == 8
                and data["model"] == "WS-C4507R+E", (m2["port_count"], m2["model"], data["members"][0]["port_count"]))
    ports = {p["name"]: p for m in data["members"] for p in m["ports"] if not p.get("absent")}
    ok &= check("4507: ports read and decided", ports["Gi2/1"]["activity"] == "active" and ports["Gi2/3"]["activity"] == "inactive"
                and ports["Gi3/2"]["vlan"] == "30" and ports["Gi2/1"]["config"] == [" description B127 Desk 1", " switchport access vlan 20"],
                (ports["Gi2/3"]["activity"], ports["Gi2/3"]["why"]))
    ok &= check("4507: no 'single switch' warning", not any("single switch" in w for w in data["warnings"]), data["warnings"])
    html = T.render_html(data)
    ok &= check("4507 page says slots", "3 slots" in html and "WS-C4507R+E" in html)

    one = "\n".join(["Port      Name               Status       Vlan       Duplex  Speed Type"] +
                     [f"Gi1/{i:<8}                   connected    10         a-full a-1000 10/100/1000BaseT" for i in range(1, 49)] +
                     ["Te1/49                       notconnect   1            full    10G Not Present"])
    d, _ = T.build_stack({"status": one, "version": "LAB-4948 uptime is 1 year, 2 weeks"}, "x", 6, now=NOW)
    ok &= check("4948 (one slot, Gi1/1-48): a single switch with 48 ports",
                d["unit"] == "Switch" and len(d["members"]) == 1 and d["members"][0]["port_count"] == 48)
    try:
        T.build_stack({"status": "Port      Name   Status       Vlan\n" + "\n".join(
            f"Te1/1/{i}           connected    trunk  full 10G SFP-10GBase-SR" for i in range(1, 5))}, "x", 6, now=NOW)
        ok &= check("unknown port names -> a clear error, not an empty page", False)
    except ValueError as exc:
        ok &= check("unknown port names -> a clear error, not an empty page", "couldn't place" in str(exc), str(exc))
    return ok


def json_copy(x):
    import json
    return json.loads(json.dumps(x))


def end_to_end():
    ok = True
    print("\nDrawing a pretend stack through the web app")
    demo, cmds = demo_outputs(), {}
    for key, cmd in T.COMMANDS.items():
        if isinstance(cmd, tuple):
            # This pretend switch is too old for '| section', so the tool falls back to the full
            # config. Its passwords and SNMP community must not end up on the page.
            cmds[cmd[-1]] = ("Building configuration...\n!\nhostname DEMO-IDF-E3\n"
                             "username netops privilege 15 secret 9 $9$FAKE-SECRET-HASH\n!\n" + demo[key] +
                             "\nsnmp-server community FAKE-COMMUNITY RO\nend\n")
        elif key in demo:          # commands the demo has no output for get "% Invalid input", like a real switch
            cmds[cmd] = demo[key]
    cli = MockCLI("DEMO-IDF-E3", cmds, start_enabled=False, enable_secret="en-secret")
    stack = TelnetSwitch(cli, "netops", "sw-pass")
    ddemo, dcmds = dell_demo_outputs(), {}
    for key, cmd in T.DELL_COMMANDS.items():
        dcmds[cmd[0] if isinstance(cmd, tuple) else cmd] = ddemo[key]
    dell_cli = MockCLI("DEMO-B2-DELL", dcmds, start_enabled=False, enable_secret="en-secret")
    dell = TelnetSwitch(dell_cli, "netops", "sw-pass", user_prompt="User Name:")
    aruba = TelnetSwitch(MockCLI("S3500-M205", {}, prompt_style="aruba"), "netops", "sw-pass", user_prompt="User: ")
    addr = f"127.0.0.1:{stack.port}"
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": [
        {"address": addr, "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E3 IDF"},
        {"address": f"127.0.0.1:{dell.port}", "type": "5548P", "protocol": "telnet", "building": "B Building"},
        {"address": f"127.0.0.1:{aruba.port}", "type": "S3500", "protocol": "telnet"}]}, **base)
    client.post("/api/settings", json={"default_protocol": "telnet", "parallel": 4, "timeout": 10}, **base)
    login = {"username": "netops", "password": "sw-pass", "secret": "en-secret"}

    ok &= check("form page loads", client.get("/topology", **base).status_code == 200)
    ok &= check("main page links to it", b'href="/topology"' in client.get("/", **base).data)
    r = client.post("/api/topology", json={"address": addr, "months": 6, **login}, **base)
    ok &= check("stack read and drawn", r.status_code == 200 and r.json["ok"], r.json)
    page = client.get(r.json["url"], **base)
    ok &= check("saved page opens, with VLAN names", page.status_code == 200 and b"DEMO-IDF-E3" in page.data
                and b"E Building, E3 IDF" in page.data and b"STUDENT-LAB" in page.data)
    sent = [c for cmd in T.COMMANDS.values() for c in (cmd if isinstance(cmd, tuple) else [cmd])]
    ok &= check("every command was sent (and the old-IOS fallback used)", all(c in cli.commands for c in sent), cli.commands)
    ok &= check("port configs on the page, the rest of the config isn't",
                b"spanning-tree bpduguard enable" in page.data and b"GigabitEthernet1/0/5" in page.data
                and b"FAKE-SECRET-HASH" not in page.data and b"FAKE-COMMUNITY" not in page.data
                and b"hostname DEMO" not in page.data)
    r2 = client.post("/api/topology", json={"address": addr, "months": 6, **login}, **base)
    ok &= check("second run remembers the first", b"run 2," in client.get(r2.json["url"], **base).data)
    pages = client.get("/api/topology/pages", **base).json["pages"]
    ok &= check("earlier drawings are listed", len(pages) >= 1 and pages[0]["name"] == "DEMO-IDF-E3", pages)

    print("\nA Dell PowerConnect stack")
    r = client.post("/api/topology", json={"address": f"127.0.0.1:{dell.port}", "months": 6, **login}, **base)
    ok &= check("Dell stack read and drawn", r.status_code == 200 and r.json["ok"], r.json)
    dpage = client.get(r.json["url"], **base).data if r.status_code == 200 else b""
    ok &= check("Dell page: name, members, VLAN names, Dell wording",
                b"DEMO-B2-DELL" in dpage and b"PowerConnect 5524P" in dpage and b"STAFF-DATA" in dpage
                and b"doesn't record when" in dpage)
    dsent = [c[0] if isinstance(c, tuple) else c for c in T.DELL_COMMANDS.values()]
    ok &= check("every Dell command was sent", all(c in dell_cli.commands for c in dsent), dell_cli.commands)
    ok &= check("Dell page holds no secrets from its config",
                b"FAKE-COMMUNITY" not in dpage and b"FAKE0123456789" not in dpage)
    ok &= check("Dell demo page loads", b"DEMO-B2-DELL" in client.get("/topology/demo?type=dell", **base).data)

    print("\nRefusals and safety")
    r = client.post("/api/topology", json={"address": f"127.0.0.1:{aruba.port}", **login}, **base)
    ok &= check("an Aruba in the switch list is refused", r.status_code == 400 and "Aruba" in r.json["problems"][0],
                r.json)
    r = client.post("/api/topology", json={"address": addr, "username": "netops"}, **base)
    ok &= check("no password -> asked for it", r.status_code == 400 and "password" in r.json["problems"][0])
    r = client.post("/api/topology", json={"address": addr, "username": "netops", "password": "wrong"}, **base)
    ok &= check("wrong password -> friendly error", r.status_code == 502 and "login failed" in r.json["problems"][0],
                r.json)
    ok &= check("pages outside the folder can't be opened",
                client.get("/topology/page/..%2Fhistory%2Fx.json", **base).status_code == 404
                and client.get("/topology/page/nothing.html", **base).status_code == 404)
    ok &= check("non-JSON posts refused", client.post("/api/topology", data={"address": addr}, **base).status_code == 415)
    ok &= check("demo page loads", client.get("/topology/demo", **base).status_code == 200)
    debug = list((Path(webapp.DATA_DIR) / "debug").glob("*-topology-*.txt"))
    ok &= check("what each switch printed is kept in data/debug, for troubleshooting",
                len(debug) >= 2 and "===== status =====" in debug[0].read_text(encoding="utf-8"), debug)
    saved = "".join(p.read_text(encoding="utf-8", errors="ignore")
                    for p in Path(webapp.DATA_DIR).rglob("*") if p.is_file())
    ok &= check("no password written to disk",
                not any(pw in saved for pw in ("sw-pass", "en-secret", "FAKE-SECRET-HASH", "FAKE-COMMUNITY")))
    stack.stop()
    dell.stop()
    aruba.stop()
    return ok


def main():
    ok = parsers()
    ok &= dell_and_translation()
    ok &= chassis()
    ok &= end_to_end()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
