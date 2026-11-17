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
from topology_demo import demo_outputs  # noqa: E402
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
    d, _ = T.build_stack(demo_outputs(), "x", 6)
    ok &= check("VLAN names go to the page, only for VLANs on these ports",
                d["vlan_names"].get("20") == "STAFF-DATA" and "1002" not in d["vlan_names"]
                and "85" not in d["vlan_names"], d["vlan_names"])
    return ok


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
        else:
            cmds[cmd] = demo[key]
    cli = MockCLI("DEMO-IDF-E3", cmds, start_enabled=False, enable_secret="en-secret")
    stack = TelnetSwitch(cli, "netops", "sw-pass")
    dell = TelnetSwitch(MockCLI("B218-5548P", {}), "netops", "sw-pass", user_prompt="User Name:")
    addr = f"127.0.0.1:{stack.port}"
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": [
        {"address": addr, "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E3 IDF"},
        {"address": f"127.0.0.1:{dell.port}", "type": "5548P", "protocol": "telnet"}]}, **base)
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

    print("\nRefusals and safety")
    r = client.post("/api/topology", json={"address": f"127.0.0.1:{dell.port}", **login}, **base)
    ok &= check("a Dell in the switch list is refused", r.status_code == 400)
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
    saved = "".join(p.read_text(encoding="utf-8", errors="ignore")
                    for p in Path(webapp.DATA_DIR).rglob("*") if p.is_file())
    ok &= check("no password written to disk",
                not any(pw in saved for pw in ("sw-pass", "en-secret", "FAKE-SECRET-HASH", "FAKE-COMMUNITY")))
    stack.stop()
    dell.stop()
    return ok


def main():
    ok = parsers()
    ok &= end_to_end()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
