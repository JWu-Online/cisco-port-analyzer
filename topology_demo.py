"""
topology_demo.py - made-up command output for a pretend 3-member Catalyst 9300
stack, so the Stack topology page can be seen (and tested) without a real switch.

Nothing here comes from a real network. Hostnames, MACs and descriptions are invented.

The demo shows the interesting cases on purpose:
  * Switch 1 and 2 have been up ~1.5 years -> ports can be confirmed inactive.
  * Switch 3 rebooted ~3 weeks ago -> idle ports there are "Unsure".
  * One stack cable is down -> the ring is broken (half ring).
"""
import random

UPTIME_LONG = "1 year, 23 weeks, 4 days, 6 hours, 12 minutes"
UPTIME_LONG_TOKEN = "1y23w"            # how 'show interfaces link' prints that uptime
UPTIME_SHORT = "3 weeks, 2 days, 5 hours, 40 minutes"
UPTIME_SHORT_TOKEN = "3w2d"

MEMBERS = [
    # number, role, model, ports, uptime words, uptime token
    (1, "Active", "C9300-48P", 48, UPTIME_LONG, UPTIME_LONG_TOKEN),
    (2, "Standby", "C9300-48P", 48, UPTIME_LONG, UPTIME_LONG_TOKEN),
    (3, "Member", "C9300-24P", 24, UPTIME_SHORT, UPTIME_SHORT_TOKEN),
]

# access VLANs and how often each shows up
VLAN_WEIGHTS = [("20", 30), ("30", 22), ("50", 6), ("60", 8), ("99", 3), ("21", 6), ("40", 4), ("70", 2), ("85", 1)]


def _pick_vlan(rng):
    total = sum(w for _, w in VLAN_WEIGHTS)
    r = rng.uniform(0, total)
    for vlan, w in VLAN_WEIGHTS:
        r -= w
        if r <= 0:
            return vlan
    return VLAN_WEIGHTS[0][0]


def _desc(rng, vlan, member, port):
    room = f"E-{300 + member * 10 + port // 6}"
    return {
        "20": f"Rm {room} Desk {port % 6 + 1}",
        "21": f"Rm {room} Faculty",
        "30": f"Lab {room} PC{port:02d}",
        "40": f"Rm {room} Phone",
        "50": f"Printer {room}",
        "60": f"AP-E{member}-{port:02d}",
        "70": f"Camera {room}",
        "85": f"Kiosk {room}",
        "99": f"Mgmt {room}",
    }.get(vlan, "") if rng.random() > 0.12 else ""


def _ports():
    """Yield one dict per front-panel port with consistent fake state."""
    rng = random.Random(7)
    for num, _, _, count, _, boot_token in MEMBERS:
        for port in range(1, count + 1):
            vlan = _pick_vlan(rng)
            if vlan == "60" and rng.random() < 0.5:
                vlan = "trunk"
            roll = rng.random()
            short_uptime = boot_token == UPTIME_SHORT_TOKEN
            if roll < 0.55:
                kind = "up"
            elif roll < 0.68:
                kind = "recent"                       # went down a few weeks ago
            elif roll < 0.76 and not short_uptime:
                kind = "old"                          # last used ~10 months ago
            elif roll < 0.79:
                kind = "shut"
            elif roll < 0.80:
                kind = "errdis"
            else:
                kind = "never"                        # down since boot
            yield num, port, vlan, kind, _desc(rng, vlan if vlan != "trunk" else "60", num, port), boot_token


# Made-up problems, so the health checks have something to show (Cisco demo)
HEALTH = {(1, 5): "crc", (1, 17): "late", (1, 30): "drops", (2, 9): "half", (2, 22): "ten",
          (1, 11): "hundred", (2, 31): "poe-fault", (1, 40): "poe-deny"}


def _status_line(name, desc, status, vlan, duplex, speed, ptype):
    return f"{name:<13}{desc[:18]:<19}{status:<13}{vlan:<11}{duplex:>6} {speed:>6} {ptype}"


def demo_outputs():
    status = ["", "Port         Name               Status       Vlan       Duplex  Speed Type"]
    link = ["", "Port           Name               Down Time      Up Time"]
    intf = []

    recent_tokens = ["1w2d", "2w5d", "4w1d", "6w3d", "9w0d", "13w4d", "17w2d"]
    old_tokens = ["38w2d", "44w6d", "52w1d"]

    run = []          # 'show running-config | section ^interface'
    power = ["Module   Available     Used     Remaining", "          (Watts)     (Watts)    (Watts)",
             "------   ---------   --------   ---------",
             "1           435.0      410.8        24.2", "2           435.0      122.4       312.6",
             "3           435.0       38.9       396.1",
             "Interface Admin  Oper       Power   Device              Class Max", "                            (Watts)",
             "--------- ------ ---------- ------- ------------------- ----- ----"]

    for num, port, vlan, kind, desc, boot_token in _ports():
        problem = HEALTH.get((num, port))
        if problem == "poe-deny":
            vlan, desc = "60", "AP E-3 hallway"
        if problem in ("crc", "late", "drops", "half", "ten", "hundred"):
            kind = "up"
        elif problem:
            kind = "recent"
        short = f"Gi{num}/0/{port}"
        long_ = f"GigabitEthernet{num}/0/{port}"
        if kind == "up":
            st, down, li, lo = "connected", "00:00:00", "never", "00:00:01"
        elif kind == "recent":
            tok = recent_tokens[(num * 7 + port) % len(recent_tokens)]
            if boot_token == "3w2d":
                tok = ["1w2d", "2w0d", "5d04h"][port % 3]
            st, down, li, lo = "notconnect", tok, "never", tok
        elif kind == "old":
            tok = old_tokens[port % len(old_tokens)]
            st, down, li, lo = "notconnect", tok, "never", tok
        elif kind == "shut":
            st, down, li, lo = "disabled", boot_token, "never", "never"
        elif kind == "errdis":
            st, down, li, lo = "err-disabled", "2d03h", "never", "2d03h"
        else:
            st, down, li, lo = "notconnect", boot_token, "never", "never"

        up = st == "connected"
        duplex = ("a-half" if problem == "half" else "a-full") if up else "auto"
        speed = {"half": "a-100", "ten": "a-10", "hundred": "a-100"}.get(problem, "a-1000") if up else "auto"
        status.append(_status_line(short, desc, st, vlan, duplex, speed, "10/100/1000BaseTX"))
        # 'show power inline': phones and access points draw power; two ports have power problems
        if problem == "poe-fault":
            power.append(f"{short:<10}auto   faulty     0.0     n/a                 n/a   30.0")
        elif problem == "poe-deny":
            power.append(f"{short:<10}auto   power-deny 0.0     Ieee PD             4     30.0")
        elif up and vlan in ("20", "21"):
            power.append(f"{short:<10}auto   on         5.9     IP Phone 8845       2     30.0")
        elif up and vlan in ("60", "trunk"):
            power.append(f"{short:<10}auto   on         15.4    AIR-AP2802I-B-K9    4     30.0")
        else:
            power.append(f"{short:<10}auto   off        0.0     n/a                 n/a   30.0")
        packets_in = (port * 7919 + num * 104729) * 13 if up or kind in ("recent", "old") else 0
        packets_out = packets_in * 2
        crc = packets_in // 250 if problem == "crc" else (3 if packets_in else 0)
        late = 233 if problem == "late" else 0
        drops = packets_out // 40 if problem == "drops" else 0
        link.append(f"{short:<15}{desc[:18]:<19}{down:<15}{'12w3d' if up else ''}")
        head = {
            "connected": "is up, line protocol is up (connected)",
            "notconnect": "is down, line protocol is down (notconnect)",
            "disabled": "is administratively down, line protocol is down (disabled)",
            "err-disabled": "is down, line protocol is down (err-disabled)",
        }[st]
        run.append(f"interface {long_}")
        if desc:
            run.append(f" description {desc}")
        if vlan == "trunk":
            run += [" switchport trunk native vlan 60", " switchport trunk allowed vlan 60,99", " switchport mode trunk"]
        else:
            run += [f" switchport access vlan {vlan}", " switchport mode access"]
            if vlan in ("20", "21"):
                run.append(" switchport voice vlan 40")
            run += [" spanning-tree portfast", " spanning-tree bpduguard enable"]
        if kind == "shut":
            run.append(" shutdown")
        run.append("!")
        intf += [
            f"{long_} {head} ",
            f"  Hardware is Gigabit Ethernet, address is 0c11.aa00.{num:02d}{port:02d} (bia 0c11.aa00.{num:02d}{port:02d})",
            *([f"  Description: {desc}"] if desc else []),
            "  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec, ",
            f"  Last input {li}, output {lo}, output hang never",
            '  Last clearing of "show interface" counters never',
            f"  Input queue: 0/2000/0/0 (size/max/drops/flushes); Total output drops: {drops}",
            f"     {packets_in} packets input, {packets_in * 480} bytes, 0 no buffer",
            f"     {crc} input errors, {crc} CRC, 0 frame, 0 overrun, 0 ignored",
            f"     {packets_out} packets output, {packets_out * 420} bytes, 0 underruns",
            "     0 output errors, 0 collisions, 1 interface resets",
            f"     {late} late collision, 0 deferred",
        ]

    # Ports the tool must ignore: uplink module, app-hosting port, SVIs.
    for num, *_ in MEMBERS:
        for p in range(1, 5):
            status.append(_status_line(f"Gi{num}/1/{p}", "", "notconnect", "1", "auto", "auto", "unknown"))
        status.append(_status_line(f"Te{num}/1/1", "UPLINK-CORE", "connected", "trunk", "full", "10G", "SFP-10GBase-SR"))
        status.append(_status_line(f"Ap{num}/0/1", "", "connected", "1", "a-full", "a-1000", "App-hosting port"))
        intf += [
            f"TenGigabitEthernet{num}/1/1 is up, line protocol is up (connected) ",
            "  Description: UPLINK-CORE",
            "  Last input 00:00:00, output 00:00:00, output hang never",
            f"AppGigabitEthernet{num}/0/1 is up, line protocol is up (connected) ",
            "  Last input never, output never, output hang never",
        ]
    intf += ["Vlan99 is up, line protocol is up ", "  Last input 00:00:00, output 00:00:00, output hang never"]

    version = "\n".join([
        "Cisco IOS XE Software, Version 17.09.04a",
        "Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.4a",
        "",
        f"DEMO-IDF-E3 uptime is {UPTIME_LONG}",
        f"Uptime for this control processor is {UPTIME_LONG}",
        "",
        "Model Number                       : C9300-48P",
        "",
        "Switch Ports Model              SW Version        SW Image              Mode   ",
        "------ ----- -----              ----------        ----------            ----   ",
        "*    1 65    C9300-48P          17.09.04a         CAT9K_IOSXE           INSTALL",
        "     2 65    C9300-48P          17.09.04a         CAT9K_IOSXE           INSTALL",
        "     3 41    C9300-24P          17.09.04a         CAT9K_IOSXE           INSTALL",
        "",
        "",
        "Switch 02",
        "---------",
        f"Switch uptime                      : {UPTIME_LONG} ",
        "",
        "Base Ethernet MAC Address          : 0c:11:aa:00:02:00",
        "Model Number                       : C9300-48P",
        "",
        "Switch 03",
        "---------",
        f"Switch uptime                      : {UPTIME_SHORT} ",
        "",
        "Base Ethernet MAC Address          : 0c:11:aa:00:03:00",
        "Model Number                       : C9300-24P",
        "",
    ])

    switch = "\n".join([
        "Switch/Stack Mac Address : 0c11.aa00.0100 - Local Mac Address",
        "Mac persistency wait time: Indefinite",
        "                                             H/W   Current",
        "Switch#   Role    Mac Address     Priority Version  State ",
        "-------------------------------------------------------------",
        "*1       Active   0c11.aa00.0100     15     V02     Ready                ",
        " 2       Standby  0c11.aa00.0200     14     V02     Ready                ",
        " 3       Member   0c11.aa00.0300     1      V01     Ready                ",
    ])

    # Ring is 1 -> 2 -> 3 -> 1, but the 3 -> 1 cable is down (half ring).
    neighbors = "\n".join([
        "  Switch #    Port 1       Port 2",
        "  --------    ------       ------",
        "      1         2           None",
        "      2         3             1",
        "      3       None            2",
    ])
    stack_ports = "\n".join([
        "  Switch #    Port 1       Port 2",
        "  --------    ------       ------",
        "    1           OK          DOWN",
        "    2           OK           OK",
        "    3          DOWN          OK",
    ])

    # VLAN 85 was never named, so IOS shows its default name (the page hides it)
    vlans = "\n".join([
        "",
        "VLAN Name                             Status    Ports",
        "---- -------------------------------- --------- -------------------------------",
        "1    default                          active    Gi1/1/1, Gi1/1/2, Gi1/1/3, Gi1/1/4",
        "20   STAFF-DATA                       active    Gi1/0/1, Gi1/0/2, Gi1/0/3, Gi1/0/9",
        "                                                Gi1/0/12, Gi1/0/19, Gi1/0/33",
        "21   FACULTY-OFFICES                  active    Gi1/0/31, Gi1/0/32",
        "30   STUDENT-LAB                      active    Gi1/0/4, Gi1/0/7, Gi1/0/14",
        "40   VOICE-PHONES                     active    ",
        "50   PRINTERS                         active    Gi1/0/11, Gi1/0/13",
        "60   WIFI-AP                          active    Gi1/0/15, Gi1/0/38",
        "70   SECURITY-CAMERAS                 active    Gi3/0/3",
        "85   VLAN0085                         active    ",
        "99   NET-MGMT                         active    Gi1/0/10",
        "1002 fddi-default                     act/unsup ",
        "1003 token-ring-default               act/unsup ",
        "1004 fddinet-default                  act/unsup ",
        "1005 trnet-default                    act/unsup ",
    ])

    return {
        "prompt": "DEMO-IDF-E3#",
        "switch": switch,
        "neighbors": neighbors,
        "stack_ports": stack_ports,
        "version": version,
        "status": "\n".join(status),
        "vlans": vlans,
        "power": "\n".join(power),
        "config": "\n".join(run + [
            "interface TenGigabitEthernet1/1/1", " description UPLINK-CORE", " switchport mode trunk", "!",
            "interface Vlan99", " ip address 10.99.4.11 255.255.255.0", "!"]),
        "link": "\n".join(link),
        "interfaces": "\n".join(intf),
    }


# ---------------------------------------------------------------------------
# A made-up Dell PowerConnect 5500 stack: unit 1 is a 5548P, unit 2 a 5524P.
# Up 412 days, so ports with no traffic since it started are Inactive, ports that
# had traffic but are down now are Unsure (a Dell doesn't record when).
# ---------------------------------------------------------------------------
DELL_UNITS = [(1, "5548P", 48), (2, "5524P", 24)]
DELL_VLAN_NAMES = {"1": "1", "20": "STAFF-DATA", "30": "STUDENT-LAB", "50": "PRINTERS", "60": "WIFI-AP",
                   "99": "NET-MGMT", "40": "VOICE-PHONES"}


def _ranges(ports):
    """['gi1/0/1', 'gi1/0/2', 'gi1/0/3', 'gi1/0/7'] -> 'gi1/0/1-3,gi1/0/7' (how a Dell prints lists)."""
    out, run = [], []
    for p in ports + [None]:
        if p and run and p.rsplit("/", 1)[0] == run[-1].rsplit("/", 1)[0] \
                and int(p.rsplit("/", 1)[1]) == int(run[-1].rsplit("/", 1)[1]) + 1:
            run.append(p)
            continue
        if run:
            out.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1].rsplit('/', 1)[1]}")
        run = [p] if p else []
    return ",".join(out)


def dell_demo_outputs():
    rng = random.Random(11)
    status = ["", "                                             Flow Link          Back   Mdix",
              "Port     Type         Duplex  Speed Neg      ctrl State       Pressure Mode",
              "-------- ------------ ------  ----- -------- ---- ----------- -------- -------"]
    desc = ["", "Port      Description", "-------  -----------"]
    cin = ["", "    Port      InUcastPkts  InMcastPkts  InBcastPkts    InOctets",
           "---------- ------------ ------------ ------------ ------------"]
    cout = ["", "    Port      OutUcastPkts OutMcastPkts OutBcastPkts   OutOctets",
            "---------- ------------ ------------ ------------ ------------"]
    run = ["config-file-header", "DEMO-B2-DELL", "v4.1.0.14 / R1.0.2", "@", "voice vlan id 40",
           "username admin password encrypted FAKE0123456789abcdef privilege 15",
           "snmp-server community FAKE-COMMUNITY ro view Default"]
    for v, n in DELL_VLAN_NAMES.items():
        if v != "1":
            run += [f"interface vlan {v}", f"name {n}", "exit"]
    untagged, tagged = {}, {}
    poe = ["", "Unit  Power  Nominal Power  Consumed Power  Usage Threshold  Traps",
           "----  -----  -------------  --------------  ---------------  -------",
           "1     On     370 Watts      46 Watts (12%)  95               Disable",
           "2     On     370 Watts      21 Watts (5%)   95               Disable", "",
           "Port       Powered Device   State   Status     Priority  Class",
           "---------  ---------------  ------  ---------  --------  -------"]

    for unit, _, count in DELL_UNITS:
        for port in list(range(1, count + 1)) + ["te1", "te2"]:
            name = f"gi{unit}/0/{port}" if isinstance(port, int) else f"te{unit}/0/{port[2:]}"
            uplink = not isinstance(port, int)
            roll = rng.random()
            kind = "uplink" if uplink else ("up" if roll < 0.5 else "used" if roll < 0.68 else
                                             "shut" if roll < 0.73 else "never")
            vlan = "trunk" if uplink or (rng.random() < 0.06) else rng.choice(
                ["20"] * 5 + ["30"] * 4 + ["50", "60", "99", "1", "1"])
            if (unit, port) == (2, 7):
                kind = "used"                       # PoE fault, link down
            up = kind in ("up", "uplink")
            speed = "10000" if uplink else ("100" if (unit, port) in ((1, 9), (1, 14)) else "1000")
            half = (unit, port) == (1, 14)
            ptype = "10G-Fiber" if uplink else "1G-Copper"
            if not uplink:
                if (unit, port) == (2, 7):
                    poe.append(f"{name:<10} {'':<16} Auto    Fault      low       class0")
                elif up and vlan == "20":
                    poe.append(f"{name:<10} {'Phone':<16} Auto    On         low       class2")
                else:
                    poe.append(f"{name:<10} {'':<16} Auto    Searching  low       class0")
            status.append(f"{name:<8} {ptype:<12} {('Half' if half else 'Full') if up else '  --':<6}  {speed if up else '  --':<5} "
                          f"{'Enabled ' if up else '   --   '} {'Off' if up else ' --'}  {'Up' if up else 'Down':<11} "
                          f"{'Disabled' if up else '   --   '} {'On' if up else '--'}")
            label = "" if rng.random() < 0.15 else (
                "UPLINK-CORE" if uplink else {"20": f"Rm B-{200 + port // 4} Desk {port % 4 + 1}",
                                              "30": f"Lab B-{210 + port // 8} PC{port:02d}",
                                              "50": f"Printer B-{200 + port // 6}", "60": f"AP-B{unit}-{port:02d}",
                                              "99": "Mgmt", "1": "", "trunk": f"AP-B{unit}-{port:02d}"}.get(vlan, ""))
            desc.append(f"{name:<9} {label}")
            packets = 0 if kind in ("never", "shut") else rng.randint(2000, 9_000_000)
            cin.append(f"  {name:<9} {packets:>12} {packets // 50:>12} {packets // 80:>12} {packets * 600:>12}")
            cout.append(f"  {name:<9} {packets // 2:>12} {packets // 40:>12} {packets // 90:>12} {packets * 400:>12}")

            lines = []
            if label:
                lines.append(f'description "{label}"' if " " in label else f"description {label}")
            if vlan == "trunk":
                lines += ["switchport mode trunk", "switchport trunk allowed vlan add 20,60,99"]
                for v in ("20", "60", "99"):
                    tagged.setdefault(v, []).append(name)
            elif vlan == "20" and port % 2 == 0:
                # Desk port set up the other common Dell way: general mode, PVID 20, voice VLAN 40 tagged
                lines += ["switchport mode general", "switchport general pvid 20",
                          "switchport general allowed vlan add 20 untagged",
                          "switchport general allowed vlan add 40 tagged"]
                untagged.setdefault("20", []).append(name)
                tagged.setdefault("40", []).append(name)
            elif vlan != "1":
                lines.append(f"switchport access vlan {vlan}")
                untagged.setdefault(vlan, []).append(name)
                if vlan == "20":
                    lines.append("voice vlan enable")
                    tagged.setdefault("40", []).append(name)     # the Dell puts it in the voice VLAN, tagged
            else:
                untagged.setdefault("1", []).append(name)
            if not uplink and vlan != "trunk" and rng.random() < 0.7:
                lines.append("spanning-tree portfast")
            if kind == "shut":
                lines.append("shutdown")
            if lines:                         # a Dell only lists ports that have something set
                run += [f"interface {'gigabitethernet' if not uplink else 'tengigabitethernet'}{name[2:]}"] + lines + ["exit"]
    status += ["", "                                          Flow    Admin Back   Mdix",
               "Ch       Type    Duplex  Speed  Neg      control  State Pressure Mode",
               "-------- ------- ------  -----  -------- -------  ----- -------- ----",
               "Po1         --     --      --      --       --    Down     --     --"]

    vlan = ["", "Created by: D-Default, S-Static, G-GVRP, R-Radius Assigned VLAN, V-Voice VLAN", "",
            "Vlan       Name           Tagged Ports      UnTagged Ports      Created by",
            "---- ----------------- ------------------ ------------------ ----------------"]
    for v, n in DELL_VLAN_NAMES.items():
        t = _ranges(tagged.get(v, [])).split(",") if tagged.get(v) else [""]
        u = _ranges(untagged.get(v, [])).split(",") if untagged.get(v) else [""]
        for i in range(max(len(t), len(u))):        # long lists wrap onto more lines
            tv = t[i] if i < len(t) else ""
            uv = u[i] if i < len(u) else ""
            first = i == 0
            # same columns as the header: Vlan 0, Name 11, Tagged 26, UnTagged 44, Created by 64
            vlan.append(f"{(' ' + v) if first else '':<11}{(n if first else ''):<15}{tv:<18}{uv:<20}"
                        f"{('S' if v != '1' else 'DV') if first else ''}")
    system = "\n".join([
        "", "System Description:                       PowerConnect 5548P",
        "System Up Time (days,hour:min:sec):       412,03:24:11", "System Contact:",
        "System Name:                              DEMO-B2-DELL", "System Location:                          B Building",
        "", "Unit    Type", "----    ----------------------",
        " 1      PowerConnect 5548P", " 2      PowerConnect 5524P"])
    stack = "\n".join([
        "", "Unit   MAC Address         Software   Master    Uplink   Downlink   Status",
        "----   -----------------   --------   -------   ------   --------   ------",
        " 1     f8:b1:56:10:20:30   4.1.0.14   Enabled     2         2       master",
        " 2     f8:b1:56:10:20:40   4.1.0.14   Enabled     1         1       backup"])
    return {
        "prompt": "DEMO-B2-DELL#", "system": system, "stack": stack,
        "status": "\n".join(status), "description": "\n".join(desc),
        "counters": "\n".join(cin + [""] + cout), "vlans": "\n".join(vlan), "config": "\n".join(run),
        "power": "\n".join(poe),
    }
