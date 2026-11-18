"""
Stack topology test: sample output from each kind of Catalyst, plus a pretend
stack served over telnet and drawn through the real web app.

    python tests/test_topology.py

No real switches needed.
"""

import os
import re
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
    general_voice = ["description \"Rm 102 Desk\"", "switchport mode general", "switchport general pvid 20",
                     "switchport general allowed vlan add 20 untagged", "switchport general allowed vlan add 40 tagged"]
    lines, notes = T.render_profile(T.parse_profile(general_voice, voice_id="40"), "cisco_ios")
    ok &= check("Dell general mode, PVID 20 + voice VLAN 40 tagged -> Cisco access port with a voice VLAN",
                lines == [" description Rm 102 Desk", " switchport mode access", " switchport access vlan 20",
                          " switchport voice vlan 40"] and "PVID 20" in notes[0], (lines, notes))
    lines, notes = T.render_profile(T.parse_profile(cfg["Gi1/0/5"]["lines"], voice_id="40"), "cisco_ios")
    ok &= check("...but a non-voice VLAN tagged (99) still copies as a trunk", " switchport mode trunk" in lines, lines)

    print("\nDell VLAN labels: the PVID, not 'trunk'")
    def label(lines, untagged=(), tagged=(), voice="40", in_config=True):
        prof = T.parse_profile(lines, voice) if lines is not None else None
        return T.dell_port_vlans(prof, in_config, set(untagged), set(tagged), voice)
    ok &= check("general mode desk port with the voice VLAN tagged -> its PVID",
                label(general_voice[1:], {"20"}, {"40"}) == ("20", "20", ["40"]))
    ok &= check("access port the phone put in the voice VLAN -> its access VLAN",
                label(["switchport access vlan 30", "voice vlan enable"], {"30"}, {"40"}) == ("30", "30", ["40"]))
    ok &= check("trunk mode with only the voice VLAN tagged -> its native VLAN",
                label(["switchport mode trunk", "switchport trunk native vlan 20",
                       "switchport trunk allowed vlan add 40"], {"20"}, {"40"}) == ("20", "20", ["40"]))
    ok &= check("an AP's PVID 60 with management 99 tagged -> 60",
                label(["switchport mode general", "switchport general pvid 60", "switchport general allowed vlan add 60 untagged",
                       "switchport general allowed vlan add 99 tagged"], {"60"}, {"99"}) == ("60", "60", ["99"]))
    ok &= check("an uplink with several VLANs tagged is still a trunk",
                label(["switchport mode trunk", "switchport trunk allowed vlan add 20,30-31,99"], {"1"},
                      {"20", "30", "31", "99"}) == ("trunk", "1", ["20", "30", "31", "99"]))
    ok &= check("no config read: the one untagged VLAN from 'show vlan'",
                label(None, {"30"}, {"40"}, in_config=False) == ("30", "30", ["40"]))
    ok &= check("nothing set on the port: VLAN 1", label([], (), ()) == ("1", "1", []))
    ddata, _ = T.build_stack(dell_demo_outputs(), "demo-dell", 6, now=NOW, vendor="dell_powerconnect")
    dports = [p for m in ddata["members"] for p in m["ports"] if not p.get("absent")]
    desks = [p for p in dports if p["desc"].startswith("Rm B-")]
    ok &= check("Dell demo: desk ports (some general mode) read as VLAN 20 with 40 tagged, only real trunks say trunk",
                desks and all(p["vlan"] == "20" and p["tagged"] == ["40"] for p in desks)
                and len([p for p in dports if p["vlan"] == "trunk"]) == 4
                and all(len(p["tagged"]) >= 2 for p in dports if p["vlan"] == "trunk"),
                [(p["name"], p["vlan"], p["tagged"]) for p in desks if p["vlan"] != "20"])

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


# ---------------------------------------------------------------------------
# Port health: PoE and slow-connection signs, and the cable test (TDR)
# ---------------------------------------------------------------------------
POWER_STACK = """Module   Available     Used     Remaining
          (Watts)     (Watts)    (Watts)
------   ---------   --------   ---------
1           435.0      410.8        24.2
2           435.0      122.4       312.6
Interface Admin  Oper       Power   Device              Class Max
                            (Watts)
--------- ------ ---------- ------- ------------------- ----- ----
Gi1/0/1   auto   on         5.9     IP Phone 8845       2     30.0
Gi1/0/2   auto   off        0.0     n/a                 n/a   30.0
Gi1/0/3   auto   faulty     0.0     n/a                 n/a   30.0
Gi1/0/4   auto   power-deny 0.0     Ieee PD             4     30.0
Gi2/0/7   auto   on         15.4    AIR-AP2802I-B-K9    4     30.0
Gi2/0/8   auto   errdisable 0.0     n/a                 n/a   30.0
"""
POWER_4500 = """Available:800(w)  Used:742(w)  Remaining:58(w)

Interface Admin  Oper            Power(Watts)     Device              Class
                           From PS    To Device
--------- ------ ---------- ---------- ---------- ------------------- -----
Gi2/1     auto   on         17.3       15.4       Ieee PD             3
Gi2/2     auto   off        0.0        0.0        n/a                 n/a
"""
POWER_SINGLE = """Available:370.0(w)  Used:21.4(w)  Remaining:348.6(w)

Interface Admin  Oper       Power   Device              Class Max
                            (Watts)
--------- ------ ---------- ------- ------------------- ----- ----
Gi1/0/1   auto   on         21.4    Ieee PD             4     30.0
"""
POWER_DELL = """
Unit  Power  Nominal Power  Consumed Power  Usage Threshold  Traps
----  -----  -------------  --------------  ---------------  -------
1     On     370 Watts      46 Watts (12%)  95               Disable

Port      Powered Device       State          Status      Priority   Class
--------- -------------------- -------------- ----------- ---------- ----------
gi1/0/1                        Auto           Searching   low        class0
gi1/0/2   IP-phone             Auto           On          low        class2
gi1/0/3                        Auto           Fault       low        class0
"""
TDR_STACK = """TDR test last run on: November 17 14:20:12

Interface Speed Local pair Pair length        Remote pair Pair status
--------- ----- ---------- ------------------ ----------- --------------------
Gi1/0/5   1000M Pair A     31   +/- 5  meters Pair B      Normal
                Pair B     31   +/- 5  meters Pair A      Normal
                Pair C     24   +/- 5  meters Pair D      Impedance Mismatch
                Pair D     31   +/- 5  meters Pair C      Normal
"""
TDR_4500 = """Interface   Speed  Pair  Cable Length  Distance to fault  Channel  Pair status
---------   -----  ----  ------------  -----------------  -------  -----------
Gi2/1       1Gbps  1-2   5 +/-1m       N/A                Pair B   Terminated
                   3-6   5 +/-1m       N/A                Pair A   Terminated
                   4-5   N/A           2 +/-1m            Pair D   Shorted
                   7-8   5 +/-1m       N/A                Pair C   Terminated
"""
TDR_UNPLUGGED = """Interface Speed Local pair Pair length        Remote pair Pair status
--------- ----- ---------- ------------------ ----------- --------------------
Gi1/0/7   auto  Pair A     41   +/- 5  meters N/A         Open
                Pair B     41   +/- 5  meters N/A         Open
                Pair C     41   +/- 5  meters N/A         Open
                Pair D     41   +/- 5  meters N/A         Open
"""
TDR_IN_PROGRESS = "TDR test is in progress on interface Gi1/0/5"


def health():
    ok = True
    print("\nPort health: power")
    ports, budgets = T.parse_power_inline(POWER_STACK)
    ok &= check("stack: power left per member", budgets[1]["remaining"] == 24.2 and budgets[2]["used"] == 122.4, budgets)
    ok &= check("stack: what each port powers", ports["Gi1/0/1"] == {"state": "on", "watts": 5.9, "device": "IP Phone 8845",
                                                                     "cls": "2"} and ports["Gi2/0/7"]["watts"] == 15.4, ports)
    ok &= check("stack: fault, denied and err-disabled power", (ports["Gi1/0/3"]["state"], ports["Gi1/0/4"]["state"],
                ports["Gi2/0/8"]["state"]) == ("faulty", "power-deny", "errdisable"), ports)
    ports, budgets = T.parse_power_inline(POWER_4500)
    ok &= check("4500: chassis budget, watts to the device", budgets == {0: {"available": 800, "used": 742, "remaining": 58}}
                and ports["Gi2/1"]["watts"] == 15.4 and ports["Gi2/1"]["cls"] == "3", (budgets, ports))
    ports, budgets = T.parse_power_inline(POWER_DELL, "dell_powerconnect")
    ok &= check("Dell: unit budget and port states", budgets[1] == {"available": 370, "used": 46, "remaining": 324}
                and ports["Gi1/0/2"]["device"] == "IP-phone" and ports["Gi1/0/2"]["cls"] == "2"
                and ports["Gi1/0/3"]["state"] == "fault", (budgets, ports))

    def titles(health):
        return [i["title"] for i in health[0]]
    fault = T.port_health("Gi1/0/3", False, "", "", "", None, {"state": "faulty", "watts": 0, "device": "", "cls": ""},
                          None, None)
    deny = T.port_health("Gi1/0/4", False, "", "", "", None, {"state": "power-deny", "watts": 0, "device": "", "cls": "4"},
                         {"available": 435, "used": 410.8, "remaining": 24.2}, None)
    on = T.port_health("Gi1/0/1", True, "a-1000", "a-full", "10/100/1000BaseTX", None,
                       {"state": "on", "watts": 5.9, "device": "IP Phone 8845", "cls": "2"}, None, None)
    ok &= check("PoE fault and denied are problems, with the budget left",
                titles(fault) == ["PoE fault"] and titles(deny) == ["PoE denied"] and "24 W left of 435 W" in deny[0][0]["text"],
                (fault, deny))
    ok &= check("a powered phone is a note, not a problem", on[0] == [] and on[1][0] == "Powering IP Phone 8845 · 5.9 W · class 2"
                and on[1][1] == "Linked at 1 Gb, full duplex", on)

    print("\nPort health: slow connections")
    uptime = timedelta(days=400)
    def errs(**kw):
        base = {"in_packets": 1_000_000, "out_packets": 2_000_000, "in_errors": 0, "crc": 0, "late": 0, "drops": 0,
                "cleared": "never"}
        base.update(kw)
        return base
    crc_bad = T.port_health("Gi1/0/5", True, "a-1000", "a-full", "", errs(crc=4000, in_errors=4000), None, None, uptime)
    crc_warn = T.port_health("Gi1/0/5", True, "a-1000", "a-full", "", errs(crc=300, in_errors=300), None, None, uptime)
    crc_noise = T.port_health("Gi1/0/5", True, "a-1000", "a-full", "", errs(crc=40, in_errors=40), None, None, uptime)
    ok &= check("receive errors: 0.4% bad, 0.03% a warning, a few dozen ignored",
                [i["level"] for i in crc_bad[0]] == ["bad"] and [i["level"] for i in crc_warn[0]] == ["warn"]
                and crc_noise[0] == [] and "since the switch started 13 months ago" in crc_bad[0][0]["text"],
                (crc_bad, crc_warn, crc_noise))
    late = T.port_health("Gi1/0/17", True, "a-1000", "a-full", "", errs(late=12), None, None, uptime)
    drops = T.port_health("Gi1/0/30", True, "a-100", "a-full", "", errs(drops=50_000), None, None, uptime)
    cleared = T.port_health("Gi1/0/5", True, "a-1000", "a-full", "", errs(crc=4000, cleared="2w3d"), None, None, uptime)
    ok &= check("late collisions, output drops, and counters cleared 2w3d ago",
                titles(late) == ["Late collisions"] and titles(drops) == ["Output drops"]
                and "in the 2 weeks since the counters were cleared" in cleared[0][0]["text"], (late, drops, cleared))
    half = T.port_health("Gi2/0/9", True, "a-100", "a-half", "10/100/1000BaseTX", None, None, None, None)
    ten = T.port_health("Gi2/0/22", True, "a-10", "a-full", "10/100/1000BaseTX", None, None, None, None)
    hundred = T.port_health("Gi1/0/11", True, "a-100", "a-full", "10/100/1000BaseTX", None, None, None, None)
    fast = T.port_health("Fa0/11", True, "a-100", "a-full", "10/100BaseTX", None, None, None, None)
    ok &= check("half duplex and 10 Mb are problems; 100 Mb on gigabit is only a note",
                titles(half) == ["Half duplex"] and titles(ten) == ["10 Mb link"] and hundred[0] == []
                and "100 Mb" in hundred[1][0] and fast[1] == ["Linked at 100 Mb, full duplex"], (half, ten, hundred, fast))
    errors = T.parse_ios_errors(demo_outputs()["interfaces"])
    ok &= check("counters read from 'show interfaces'", errors["Gi1/0/17"]["late"] == 233
                and errors["Gi1/0/5"]["crc"] > 1000 and errors["Gi1/0/5"]["cleared"] == "never", errors.get("Gi1/0/17"))

    print("\nPort health on the drawings")
    data, _ = T.build_stack(demo_outputs(), "demo-stack", 6, now=NOW)
    flagged = {p["name"]: [i["title"] for i in p["issues"]] for m in data["members"] for p in m["ports"] if p.get("issues")}
    ok &= check("Cisco demo: the seven made-up problems, nothing else", flagged == {
        "Gi1/0/5": ["Receive errors"], "Gi1/0/17": ["Late collisions"], "Gi1/0/30": ["Output drops"],
        "Gi1/0/40": ["PoE denied"], "Gi2/0/9": ["Half duplex"], "Gi2/0/22": ["10 Mb link"], "Gi2/0/31": ["PoE fault"]}
        and data["issues"] == {"ports": 7, "power": 2, "slow": 5}, flagged)
    ok &= check("low PoE budget warned about, per member",
                any("Switch 1 has only 24 W of PoE power left" in w for w in data["warnings"])
                and data["members"][0]["poe"]["remaining"] == 24.2, data["warnings"])
    lone = {k: v for k, v in demo_outputs().items() if k in ("prompt", "version", "status", "vlans")}
    lone["status"] = "\n".join(ln for ln in lone["status"].splitlines() if not re.match(r"Gi[23]/", ln))
    lone["power"] = POWER_SINGLE
    single, _ = T.build_stack(lone, "x", 6, now=NOW)
    ok &= check("a lone switch's 'Available:' line counts as switch 1", len(single["members"]) == 1
                and (single["members"][0]["poe"] or {}).get("available") == 370, single["members"][0].get("poe"))
    ddata, _ = T.build_stack(dell_demo_outputs(), "demo-dell", 6, now=NOW, vendor="dell_powerconnect")
    dflag = {p["name"]: [i["title"] for i in p["issues"]] for m in ddata["members"] for p in m["ports"] if p.get("issues")}
    ok &= check("Dell demo: half duplex and a PoE fault", dflag == {"gi1/0/14": ["Half duplex"], "gi2/0/7": ["PoE fault"]}
                and ddata["members"][0]["poe"]["used"] == 46, dflag)
    html = T.render_html(data)
    ok &= check("page has the Issues filter and the cable test", "Issues" in html and "Run cable test" in html
                and "/api/topology/cable-test" in html)

    print("\nCable test results")
    pairs = T.parse_tdr(TDR_STACK)
    verdict = T.tdr_verdict(pairs, True)
    ok &= check("stack: four pairs read, damaged pair C at 24 m",
                [(p["pair"], p["length"], p["status"]) for p in pairs] == [("A", 31, "normal"), ("B", 31, "normal"),
                ("C", 24, "impedance mismatch"), ("D", 31, "normal")]
                and verdict[0] == "problem" and verdict[1].startswith("Pair C is mismatched (impedance) at about 24 m"),
                (pairs, verdict))
    good = T.tdr_verdict(T.parse_tdr(TDR_STACK.replace("Impedance Mismatch", "Normal")), True)
    ok &= check("all pairs normal -> good, with the length", good == ("good", "Cable looks good: about 31 m long."), good)
    pairs = T.parse_tdr(TDR_4500)
    verdict = T.tdr_verdict(pairs, True)
    ok &= check("4500: pairs named 1-2 .. 7-8, short on 4-5 at 2 m",
                [p["pair"] for p in pairs] == ["1-2", "3-6", "4-5", "7-8"] and pairs[2]["length"] == 2
                and verdict[1].startswith("Pair 4-5 is shorted at about 2 m"), (pairs, verdict))
    verdict = T.tdr_verdict(T.parse_tdr(TDR_UNPLUGGED), False)
    ok &= check("all pairs open at the same length, no link -> nothing plugged in at the far end",
                verdict[0] == "problem" and "All pairs are open at about 41 m" in verdict[1]
                and "nothing is plugged in at the far end" in verdict[1], verdict)
    verdict = T.tdr_verdict(T.parse_tdr("Cable on port gi1/0/3 is open at 64 m"), False)
    ok &= check("Dell: one answer for the cable", verdict[1].startswith("The cable is open at about 64 m"), verdict)
    ok &= check("no result -> unknown", T.tdr_verdict(T.parse_tdr("TDR test last run on: never"), True)[0] == "unknown")
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

    print("\nCable test on one port")
    T.TDR_WAIT = {"cisco_ios": 0, "dell_powerconnect": 0}
    polls = {"n": 0}
    real_send = MockCLI.handle
    cli.outputs["test cable-diagnostics tdr interface Gi1/0/5"] = (
        "Link state may be affected during TDR test\nTDR test started on interface Gi1/0/5\n"
        "A TDR test can take a few seconds to run on an interface\nUse 'show cable-diagnostics tdr' to read the TDR results.")
    cli.outputs["show interfaces Gi1/0/5"] = (
        "GigabitEthernet1/0/5 is up, line protocol is up (connected)\n  Full-duplex, 1000Mb/s, media type is 10/100/1000BaseTX\n"
        '  Last clearing of "show interface" counters never\n'
        "  Input queue: 0/2000/0/0 (size/max/drops/flushes); Total output drops: 0\n"
        "     2000000 packets input, 960000000 bytes, 0 no buffer\n     9120 input errors, 9120 CRC, 0 frame\n"
        "     4000000 packets output, 1680000000 bytes, 0 underruns\n     0 late collision, 0 deferred")
    cli.outputs["show power inline Gi1/0/5"] = (
        "Interface Admin  Oper       Power   Device              Class Max\n"
        "--------- ------ ---------- ------- ------------------- ----- ----\n"
        "Gi1/0/5   auto   on         5.9     IP Phone 8845       2     30.0")
    cli.outputs["test cable-diagnostics tdr interface Te1/1/1"] = "% TDR test is not supported on this interface"

    def handle(self, line):              # the first look finds the test still running
        if line.strip() == "show cable-diagnostics tdr interface Gi1/0/5":
            self.commands.append(line.strip())
            polls["n"] += 1
            return (TDR_IN_PROGRESS if polls["n"] == 1 else TDR_STACK) + "\n"
        return real_send(self, line)
    MockCLI.handle = handle
    real_pause = T._pause
    T._pause = lambda s: None
    try:
        r = client.post("/api/topology/cable-test", json={"address": addr, "port": "Gi1/0/5", **login}, **base)
        res = r.json
        ok &= check("cable test runs and reads the result", r.status_code == 200 and res["verdict"] == "problem"
                    and res["pairs"][2]["status"] == "impedance mismatch" and polls["n"] == 2, res)
        ok &= check("errors and power read fresh for that port",
                    [i["title"] for i in res["issues"]] == ["Receive errors"] and "Try the cable test" not in res["issues"][0]["text"]
                    and any(n.startswith("Powering IP Phone 8845") for n in res["notes"]), res)
        sent = [c for c in cli.commands if c.endswith("Gi1/0/5")]
        ok &= check("test, then the result (asked again while running), then errors and power", sent == [
            "test cable-diagnostics tdr interface Gi1/0/5", "show cable-diagnostics tdr interface Gi1/0/5",
            "show cable-diagnostics tdr interface Gi1/0/5", "show interfaces Gi1/0/5", "show power inline Gi1/0/5"], sent)
        r = client.post("/api/topology/cable-test", json={"address": addr, "port": "Te1/1/1", **login}, **base)
        ok &= check("a port that can't be tested -> the switch's own words", r.status_code == 409
                    and "not supported" in r.json["problems"][0], r.json)
        before = len(cli.commands)
        r = client.post("/api/topology/cable-test", json={"address": addr, "port": "Gi1/0/5\nreload", **login}, **base)
        ok &= check("anything but a port name is refused before logging in", r.status_code == 400
                    and len(cli.commands) == before, r.json)
        r = client.post("/api/topology/cable-test", json={"address": "demo-stack", "port": "Gi1/0/5", **login}, **base)
        ok &= check("the made-up stack can't be tested", r.status_code == 400, r.json)
        r = client.post("/api/topology/cable-test", json={"address": addr, "port": "Gi1/0/5", "username": "netops"}, **base)
        ok &= check("no password -> asked for it", r.status_code == 400 and "password" in r.json["problems"][0], r.json)
        r = client.post("/api/topology/cable-test", data="{}", **base)
        ok &= check("non-JSON posts refused", r.status_code == 415)
    finally:
        MockCLI.handle = real_send
        T._pause = real_pause

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
    ok &= health()
    ok &= end_to_end()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
