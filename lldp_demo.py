"""
A made-up building for the LLDP neighbors page and its tests: what each switch would print.

    E3-IDF-SW1  Cisco Catalyst, two-member stack: desk phones (CDP and LLDP, the same phone both ways),
                access points, a printer and a camera that only speak LLDP, a phone from another maker,
                Windows PCs (their built-in LLDP sends only a name or MAC), PCs that say nothing, a PC
                behind a phone, a port shut down and one err-disabled
    E3-IDF-SW2  Cisco Catalyst, 24 ports, daisy-chained off SW1
    E2-IDF      Dell PowerConnect: phones and an access point by LLDP
    M2-ARUBA    Aruba Mobility Access Switch: its LLDP table has its own letters (A access point, P phone)
    CORE-1      Cisco Nexus: the closets, a router and a wireless controller
    E4-IDF      refuses the login
"""

import random

from network_demo import _cdp_block

IOS_HEAD = f"{'Port':<13}{'Name':<19}{'Status':<13}{'Vlan':<11}{'Duplex':<8}{'Speed':<7}Type\n"


def _ios_status(port, name, status, vlan, nxos=False):
    up = status == "connected"
    duplex, speed = ("full", "1000" if not port.startswith(("Te", "Eth")) else "10G") if up else ("auto", "auto")
    if not nxos:
        duplex, speed = ("a-full", "a-1000") if up and not port.startswith("Te") else (duplex, speed)
    return f"{port:<13}{(name or ('--' if nxos else ''))[:18]:<19}{status:<13}{vlan:<11}{duplex:<8}{speed:<7}" \
           f"{'10/100/1000BaseTX' if not port.startswith(('Te', 'Eth')) else 'SFP-10GBase-SR'}\n"


def _ios_desc(port, status, desc):
    proto = "up" if status == "connected" else "down"
    admin = "admin down" if status == "disabled" else proto
    return f"{port:<31}{admin:<15}{proto:<9}{desc}\n"


def _lldp_block(local, chassis, port, port_desc, name, sysdesc, caps, ip, model=""):
    med = f"\nMED Information:\n\n    Manufacturer: Cisco Systems, Inc.\n    Model: {model}\n" if model else ""
    return (f"------------------------------------------------\nLocal Intf: {local}\nChassis id: {chassis}\n"
            f"Port id: {port}\nPort Description: {port_desc}\nSystem Name: {name}\n\nSystem Description:\n"
            f"{sysdesc}\n\nTime remaining: 112 seconds\nSystem Capabilities: {caps}\nEnabled Capabilities: {caps}\n"
            f"Management Addresses:\n    IP: {ip}\n{med}")


def _windows_block(local, chassis, mac):
    """What a Catalyst shows for a Windows PC: its LLDP driver sends an id and its MAC, nothing else."""
    return (f"------------------------------------------------\nLocal Intf: {local}\nChassis id: {chassis}\n"
            f"Port id: {mac}\nPort Description - not advertised\nSystem Name - not advertised\n"
            f"System Description - not advertised\n\nTime remaining: 101 seconds\n"
            f"System Capabilities - not advertised\nEnabled Capabilities - not advertised\n"
            f"Management Addresses - not advertised\n")


def _mac(n):
    raw = f"00bb{n:08x}"
    return f"{raw[0:4]}.{raw[4:8]}.{raw[8:12]}"


def _catalyst(name, members, ports_per, plan, uplinks, building_octet):
    """plan(member, port) -> (kind, description) where kind is phone, ap, printer, camera, other-phone,
    pc, empty, disabled, errdisabled or (switch, its name, its port)."""
    status, desc, cdp, lldp = [IOS_HEAD], ["Interface                      Status         Protocol Description\n"], [], []
    n = 0
    for m in range(1, members + 1):
        for p in range(1, ports_per + 1):
            port, long = f"Gi{m}/0/{p}", f"GigabitEthernet{m}/0/{p}"
            kind, text = plan(m, p)
            n += 1
            st, vlan = "connected", "20"
            ip = f"10.0.{building_octet}.{(m - 1) * 50 + p}"
            mac = _mac(building_octet * 1000 + m * 100 + p)
            sep = "SEP" + mac.replace(".", "").upper()
            if kind in ("pc", "phone+pc") and p % 3:                 # Windows: its name, or just its MAC
                pc_mac = _mac(building_octet * 1000 + m * 100 + p + 500)
                lldp.append(_windows_block(port, f"{name[:2]}-PC-{m}{p:02d}" if p % 2 else pc_mac, pc_mac))
            if kind in ("phone", "phone+pc"):
                cdp.append(_cdp_block(sep, ip, "Cisco IP Phone 8845", "Host Phone Two-port Mac Relay", long, "Port 1",
                                      False))
                lldp.append(_lldp_block(port, mac, mac, "SW PORT", sep, "Cisco IP Phone CP-8845, V3, sip88xx.14-1",
                                        "B,T", ip, "CP-8845"))
            elif kind == "other-phone":
                lldp.append(_lldp_block(port, mac, mac, "LAN port", "", "Polycom VVX 450", "B,T", ip))
            elif kind == "ap":
                ap = f"AP-{name[:2]}-{m}{p:02d}"
                cdp.append(_cdp_block(ap, ip, "cisco C9120AXI-B", "Router Trans-Bridge Source-Route-Bridge IGMP", long,
                                      "GigabitEthernet0", False))
                lldp.append(_lldp_block(port, mac, "Gi0", "PoE Port", ap, "Cisco AP Software, C9120AXI-B", "B,R,W", ip))
                vlan = "trunk"
            elif kind == "printer":
                lldp.append(_lldp_block(port, mac, mac, "HP ETHERNET", f"NPI{mac[-6:].replace('.', '').upper()}",
                                        "HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153", "S", ip))
                vlan = "15"
            elif kind == "camera":
                lldp.append(_lldp_block(port, mac, mac, "eth0", f"axis-{mac[-4:]}", "AXIS P3245-LV Network Camera 10.12",
                                        "S", ip))
                vlan = "30"
            elif kind == "pc":
                pass
            elif kind == "empty":
                st = "notconnect"
            elif kind == "disabled":
                st, vlan = "disabled", "1"
            elif kind == "errdisabled":
                st = "err-disabled"
            else:                                            # (switch, name, port)
                _, other, their = kind
                cdp.append(_cdp_block(other, f"10.0.{building_octet}.2{m}{p}", "cisco WS-C2960X-48FPD-L",
                                      "Switch IGMP", long, their, False))
                vlan = "trunk"
            status.append(_ios_status(port, text, st, vlan))
            desc.append(_ios_desc(port, st, text))
        for u in range(1, 5):
            port, long = f"Te{m}/1/{u}", f"TenGigabitEthernet{m}/1/{u}"
            up = (m, u) in uplinks
            status.append(_ios_status(port, "Uplink to core" if up else "", "connected" if up else "notconnect",
                                      "trunk" if up else "1"))
            desc.append(_ios_desc(port, "connected" if up else "notconnect", "Uplink to core" if up else ""))
            if up:
                core, core_port = uplinks[(m, u)]
                cdp.append(_cdp_block(core, "10.0.0.1", "N7K-C7010", "Router Switch IGMP Filtering Supports-STP-Dispute",
                                      long, core_port, False))
    return {"prompt": name, "status": "".join(status), "descriptions": "".join(desc),
            "cdp": "".join(cdp), "lldp": "".join(lldp), "location": "E Building 3rd floor IDF"}


def _sw1_plan(m, p):
    rng = random.Random(m * 100 + p)
    if (m, p) == (1, 48):
        return ("switch", "E3-IDF-SW2", "GigabitEthernet1/0/24"), "To E3-IDF-SW2"
    if p in (12, 36):
        return "ap", f"AP E3{m}{p:02d} ceiling"
    if (m, p) == (1, 47):
        return "printer", "Printer E-305"
    if (m, p) == (2, 47):
        return "camera", "Camera hallway east"
    if (m, p) == (2, 46):
        return "other-phone", "Conference E-320"
    if (m, p) == (1, 44):
        return "disabled", "Spare"
    if (m, p) == (2, 30):
        return "errdisabled", "Rm E-330 desk 1"
    if (m, p) == (1, 5):
        return "phone+pc", "Rm E-305 desk 1"
    roll = rng.random()
    if roll < 0.55:
        return "phone", f"Rm E-3{p:02d} desk {m}"
    if roll < 0.75:
        return "pc", f"Rm E-3{p:02d} desk {m}"
    return "empty", ""


def _sw2_plan(m, p):
    if p == 24:
        return ("switch", "E3-IDF-SW1", "GigabitEthernet1/0/48"), "To E3-IDF-SW1"
    if p == 6:
        return "ap", "AP E3-lab"
    rng = random.Random(500 + p)
    return ("phone", f"Lab E-34{p % 10} bench") if rng.random() < 0.5 else ("pc" if rng.random() < 0.5 else "empty", "")


def _dell():
    status = ["                                             Flow Link          Back   Mdix\n"
              "Port     Type         Duplex  Speed Neg      ctrl State       Pressure Mode\n"
              "-------- ------------ ------  ----- -------- ---- ----------- -------- -------\n"]
    desc = ["Port      Description\n-------  -----------\n"]
    lldp = ["System capability supported: Bridge, Router\nSystem capability enabled: Bridge\n\n"
            "  Port         Device ID          Port ID          System Name    Capabilities    TTL\n"
            "---------  -----------------  ---------------  -------------  --------------  -----\n"]
    for p in range(1, 49):
        port = f"gi1/0/{p}"
        rng = random.Random(900 + p)
        roll = rng.random()
        mac = f"00:11:22:33:{p // 256:02x}:{p % 256:02x}"
        if p == 48:
            what, text = "ap", "AP E2 hallway"
            lldp.append(f"{port:<10} {mac:<18} {'Gi0':<16} {'AP-E2-48':<14} B, R, W         110\n")
        elif roll < 0.5:
            what, text = "phone", f"Rm E-2{p:02d}"
            lldp.append(f"{port:<10} {mac:<18} {mac:<18} {'SEP' + mac.replace(':', '').upper():<16} B, T            150\n")
        elif roll < 0.75:
            what, text = "pc", f"Rm E-2{p:02d}"
        else:
            what, text = "empty", ""
        up = what != "empty"
        status.append(f"{port:<8} 1G-Copper    {'Full' if up else '  --'}    {'1000' if up else '  --'}  "
                      f"{'Enabled' if up else '   --'}  Off  {'Up' if up else 'Down':<11} Disabled On\n")
        desc.append(f"{port:<9} {text}\n")
    return {"prompt": "E2-IDF", "status": "".join(status), "descriptions": "".join(desc), "lldp": "".join(lldp),
            "location": ""}


def _aruba():
    intf, lldp = [], ["Capability codes: (R)Router, (B)Bridge, (A)Access Point, (P)Phone, (O)Other\n"
                      "LLDP Neighbor Information\n-------------------------\n"
                      "Local Intf  Chassis ID         Capability  Remote Intf    Expiry-Time (Secs)  System Name\n"
                      "----------  ----------         ----------  -----------    ------------------  -----------\n"]
    plan = {0: ("ap", "AP M2 lounge"), 1: ("phone", "Rm M-201"), 2: ("phone", "Rm M-202"), 3: ("pc", "Rm M-203"),
            4: ("empty", ""), 5: ("phone", "Rm M-205")}
    for p in range(0, 24):
        port = f"GE0/0/{p}"
        what, text = plan.get(p, ("empty", ""))
        link = "Up" if what != "empty" else "Down"
        intf.append(f"{port} is administratively Up, Link is {link}, Line protocol is {link}\n"
                    f"Hardware is Gigabit Ethernet, Address is 00:0b:86:6a:23:{p:02x}\n"
                    f"Description: {text or port}\nlink status last changed 3 day 1 hr 0 min 0 sec\n")
        mac = f"00:0b:86:aa:00:{p:02x}"
        if what == "ap":
            lldp.append(f"{port:<11} {mac:<18} A           eth0           110                 AP-M2-LOUNGE\n")
        elif what == "phone":
            lldp.append(f"{port:<11} {mac:<18} B:P         Port 1         150                 SEP000B86AA00{p:02X}\n")
    return {"prompt": "M2-ARUBA", "interfaces": "".join(intf), "lldp": "".join(lldp), "location": "M Building 2nd floor"}


def _core():
    head = f"{'Port':<14}{'Name':<19}{'Status':<10}{'Vlan':<10}{'Duplex':<8}{'Speed':<8}Type\n"
    rows = [("Eth1/1", "E3-IDF-SW1 Te1/1/1", "connected"), ("Eth1/2", "E3-IDF-SW1 Te2/1/1", "connected"),
            ("Eth1/3", "E2-IDF", "connected"), ("Eth1/4", "M2-ARUBA", "connected"), ("Eth1/5", "E4-IDF", "connected"),
            ("Eth1/6", "--", "notconnec"), ("Eth1/7", "--", "xcvrAbsen"), ("Eth6/1", "Internet router", "connected"),
            ("Eth6/2", "WLC-1", "connected")]
    status = ["-" * 80 + "\n", head, "-" * 80 + "\n"]
    status += [f"{p:<14}{n[:18]:<19}{s:<10}{'trunk' if s == 'connected' else '1':<10}{'full':<8}{'10G':<8}10Gbase-SR\n"
               for p, n, s in rows]
    descs = "-" * 80 + "\nPort          Type   Speed   Description\n" + "-" * 80 + "\n" + "".join(
        f"{p:<14}eth    10G     {n}\n" for p, n, _ in rows)
    cdp = [_cdp_block("E3-IDF-SW1", "10.0.5.1", "cisco C9300-48P", "Router Switch IGMP", "Ethernet1/1",
                      "TenGigabitEthernet1/1/1", True),
           _cdp_block("E3-IDF-SW1", "10.0.5.1", "cisco C9300-48P", "Router Switch IGMP", "Ethernet1/2",
                      "TenGigabitEthernet2/1/1", True),
           _cdp_block("E4-IDF", "10.0.5.4", "cisco WS-C3850-48P", "Router Switch IGMP", "Ethernet1/5",
                      "TenGigabitEthernet1/1/1", True),
           _cdp_block("INET-RTR-1", "10.0.0.254", "cisco ISR4451-X/K9", "Router IGMP", "Ethernet6/1",
                      "GigabitEthernet0/0/0", True),
           _cdp_block("WLC-1", "10.0.0.20", "cisco AIR-CT5520-K9", "Router Switch Host", "Ethernet6/2",
                      "TenGigabitEthernet0/0/1", True)]
    lldp = ("Chassis id: 001a.2b00.0003\nPort id: te1/0/1\nLocal Port id: Eth1/3\nPort Description: Uplink\n"
            "System Name: E2-IDF\nSystem Description: PowerConnect 5548P\nTime remaining: 98 seconds\n"
            "System Capabilities: B\nEnabled Capabilities: B\nManagement Address: 10.0.5.2\n\n"
            "Chassis id: 000b.866a.2300\nPort id: GE0/0/47\nLocal Port id: Eth1/4\nPort Description: GE0/0/47\n"
            "System Name: M2-ARUBA\nSystem Description: ArubaOS (MODEL: ArubaS3500-48P), Version 7.4.1.9\n"
            "Time remaining: 98 seconds\nSystem Capabilities: B, R\nEnabled Capabilities: B\n"
            "Management Address: 10.0.1.12\n\n")
    return {"prompt": "CORE-1", "status": "".join(status), "descriptions": descs, "cdp": "".join(cdp), "lldp": lldp,
            "location": "M Building Main MDF"}


SWITCHES = [
    ({"address": "10.0.5.1", "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E3 IDF",
      "name": "E3-IDF-SW1"}, lambda: _catalyst("E3-IDF-SW1", 2, 48, _sw1_plan, {(1, 1): ("CORE-1", "Ethernet1/1"),
                                                                               (2, 1): ("CORE-1", "Ethernet1/2")}, 5)),
    ({"address": "10.0.5.3", "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E3 IDF",
      "name": ""}, lambda: _catalyst("E3-IDF-SW2", 1, 24, _sw2_plan, {}, 6)),
    ({"address": "10.0.5.2", "type": "dell_powerconnect", "protocol": "telnet", "building": "E Building",
      "location": "E2 IDF", "name": "E2-IDF"}, _dell),
    ({"address": "10.0.1.12", "type": "aruba_mas", "protocol": "telnet", "building": "M Building", "location": "",
      "name": "M2-ARUBA"}, _aruba),
    ({"address": "10.0.0.1", "type": "cisco_nxos", "protocol": "ssh", "building": "M Building",
      "location": "Main MDF", "name": "CORE-1"}, _core),
    ({"address": "10.0.5.4", "type": "cisco_ios", "protocol": "telnet", "building": "E Building", "location": "E4 IDF",
      "name": "E4-IDF"}, None),
]


def demo_results():
    """[(switch row, outputs or None, problem)] as if each switch had been read."""
    out = []
    for switch, make in SWITCHES:
        if make is None:
            out.append((switch, None, "login failed (check the username and password)"))
        else:
            out.append((switch, make(), ""))
    return out


def demo_outputs():
    """{address: outputs} for the pretend switches in the tests."""
    return {s["address"]: make() for s, make in SWITCHES if make}
