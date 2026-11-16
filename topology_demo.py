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


def _status_line(name, desc, status, vlan, duplex, speed, ptype):
    return f"{name:<13}{desc[:18]:<19}{status:<13}{vlan:<11}{duplex:>6} {speed:>6} {ptype}"


def demo_outputs():
    status = ["", "Port         Name               Status       Vlan       Duplex  Speed Type"]
    link = ["", "Port           Name               Down Time      Up Time"]
    intf = []

    recent_tokens = ["1w2d", "2w5d", "4w1d", "6w3d", "9w0d", "13w4d", "17w2d"]
    old_tokens = ["38w2d", "44w6d", "52w1d"]

    for num, port, vlan, kind, desc, boot_token in _ports():
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
        status.append(_status_line(short, desc, st, vlan, "a-full" if up else "auto",
                                   "a-1000" if up else "auto", "10/100/1000BaseTX"))
        link.append(f"{short:<15}{desc[:18]:<19}{down:<15}{'12w3d' if up else ''}")
        head = {
            "connected": "is up, line protocol is up (connected)",
            "notconnect": "is down, line protocol is down (notconnect)",
            "disabled": "is administratively down, line protocol is down (disabled)",
            "err-disabled": "is down, line protocol is down (err-disabled)",
        }[st]
        intf += [
            f"{long_} {head} ",
            f"  Hardware is Gigabit Ethernet, address is 0c11.aa00.{num:02d}{port:02d} (bia 0c11.aa00.{num:02d}{port:02d})",
            *([f"  Description: {desc}"] if desc else []),
            "  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec, ",
            f"  Last input {li}, output {lo}, output hang never",
            '  Last clearing of "show interface" counters never',
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
        "link": "\n".join(link),
        "interfaces": "\n".join(intf),
    }
