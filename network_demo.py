"""
network_demo.py - A made-up campus for the Network map's sample page and tests.

What the switches print is generated, so the sample goes through the same reading as
real switches:
  * two Nexus 7000s doing core and distribution, with every closet plugged into them
  * closets: Catalyst 9300/3650/2960 stacks (one daisy-chained), a Catalyst 4507 chassis,
    an Aruba seen by LLDP, a Dell nothing reports (found by its MAC address), one that
    refuses the login
  * neighbors that aren't on the switch list, which the map leaves out: an edge router, a
    lab switch someone plugged in, a wireless controller, phones and access points
"""

SWITCHES = [
    # address, type, building, location, hostname, model
    ("10.0.0.1", "cisco_nxos", "M Building", "Main MDF", "CORE-1", "Nexus7000 C7010"),
    ("10.0.0.2", "cisco_nxos", "M Building", "Main MDF", "CORE-2", "Nexus7000 C7010"),
    ("10.2.1.11", "cisco_ios", "B Building", "B1 IDF", "B1-IDF", "C9300-48P"),
    ("10.2.1.12", "dell_powerconnect", "B Building", "B2 IDF", "B2-IDF", "5548P"),
    ("10.2.1.13", "cisco_ios", "B Building", "B3 IDF", "B3-IDF", "WS-C2960X-48FPD-L"),
    ("10.2.1.14", "cisco_ios", "B Building", "B3 IDF", "B3-IDF-2", "WS-C2960X-48FPD-L"),
    ("10.2.1.15", "cisco_ios", "B Building", "B4 IDF", "B4-IDF", "C9300-48P"),
    ("10.2.1.27", "cisco_ios", "B Building", "B127 IDF", "B127-4507", "WS-C4507R+E"),
    ("10.3.1.11", "cisco_ios", "C Building", "C1 IDF", "C1-IDF", "C9300-48P"),
    ("10.3.1.12", "cisco_ios", "C Building", "C2 IDF", "C2-IDF", "C9300-48P"),
    ("10.3.1.13", "cisco_ios", "C Building", "C3 IDF", "C3-IDF", "C9300-24P"),
    ("10.3.1.14", "aruba_mas", "C Building", "C4 IDF", "C4-IDF", "ArubaS3500-48P"),
    ("10.3.1.15", "cisco_ios", "C Building", "C5 IDF", "C5-IDF", "C9300-48P"),
    ("10.0.1.11", "cisco_ios", "M Building", "M1 IDF", "M1-IDF", "C9300-48P"),
] + [(f"10.5.1.{10 + i}", "cisco_ios", "E Building", f"E{i} IDF", f"E{i}-IDF",
      "WS-C3650-48PD" if i % 2 else "C9300-48P") for i in range(1, 9)]
REFUSES_LOGIN = {"10.2.1.15"}

# (switch A, port on A, switch B, port on B, how: "cdp" both ends, "lldp" both ends)
LINKS = [
    ("CORE-1", "Ethernet1/1", "CORE-2", "Ethernet1/1", "cdp"),
    ("CORE-1", "Ethernet1/2", "CORE-2", "Ethernet1/2", "cdp"),
    ("CORE-1", "Ethernet2/1", "B1-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("CORE-2", "Ethernet2/1", "B1-IDF", "TenGigabitEthernet2/1/1", "cdp"),
    ("CORE-1", "Ethernet2/3", "B3-IDF", "GigabitEthernet1/0/49", "cdp"),
    ("B3-IDF", "GigabitEthernet1/0/50", "B3-IDF-2", "GigabitEthernet1/0/49", "cdp"),
    ("CORE-1", "Ethernet2/5", "B4-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("CORE-1", "Ethernet2/7", "B127-4507", "TenGigabitEthernet1/1", "cdp"),
    ("CORE-2", "Ethernet2/7", "B127-4507", "TenGigabitEthernet2/1", "cdp"),
    ("CORE-1", "Ethernet3/14", "C4-IDF", "GE0/0/47", "lldp"),
    ("CORE-1", "Ethernet4/1", "M1-IDF", "TenGigabitEthernet1/1/1", "cdp"),
] + [link for i, name in enumerate(["C1-IDF", "C2-IDF", "C3-IDF", "C5-IDF"], start=1) for link in (
    ("CORE-1", f"Ethernet3/{i}", name, "TenGigabitEthernet1/1/1", "cdp"),
    ("CORE-2", f"Ethernet3/{i}", name, "TenGigabitEthernet2/1/1", "cdp"))] \
  + [link for i in range(1, 9) for link in (
    ("CORE-1", f"Ethernet5/{i}", f"E{i}-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("CORE-2", f"Ethernet5/{i}", f"E{i}-IDF", "TenGigabitEthernet2/1/1", "cdp"))]
DELL = "B2-IDF"
DELL_UPLINK = ("CORE-1", "Ethernet2/2")            # nothing reports this link

# Seen by CDP, not on the switch list: (switch, its port, name, platform, capabilities, their port)
NOT_LISTED = [
    ("CORE-1", "Ethernet6/1", "INET-RTR-1", "cisco ISR4451-X/K9", "Router IGMP", "GigabitEthernet0/0/0"),
    ("CORE-2", "Ethernet6/1", "INET-RTR-1", "cisco ISR4451-X/K9", "Router IGMP", "GigabitEthernet0/0/1"),
    ("E2-IDF", "GigabitEthernet1/0/44", "LAB-SW-TEMP", "cisco WS-C2960-24TT-L", "Switch IGMP", "GigabitEthernet0/1"),
    ("CORE-2", "Ethernet6/2", "WLC-1", "cisco AIR-CT5520-K9", "Router Switch Host", "TenGigabitEthernet0/0/1"),
    ("E3-IDF", "GigabitEthernet1/0/1", "SEP00112233AA01", "Cisco IP Phone 8845", "Host Phone Two-port Mac Relay",
     "Port 1"),
    ("E3-IDF", "GigabitEthernet1/0/28", "AP-E3-28", "cisco AIR-AP2802I-B-K9", "Router Trans-Bridge", "GigabitEthernet0"),
]


def _info():
    return {s[4]: {"address": s[0], "type": s[1], "building": s[2], "location": s[3], "model": s[5]}
            for s in SWITCHES}


def _mac(n):
    return f"00:1a:2b:00:{n // 256:02x}:{n % 256:02x}"


def _dotted(n):
    raw = _mac(n).replace(":", "")
    return f"{raw[0:4]}.{raw[4:8]}.{raw[8:12]}"


def _cdp_block(name, ip, platform, caps, local, remote, nxos):
    if nxos:
        return (f"----------------------------------------\nDevice ID:{name}(FOX1{sum(map(ord, name)) % 10**6:06d})\n"
                f"System Name: {name}\n\nInterface address(es):\n    IPv4 Address: {ip}\n"
                f"Platform: {platform}, Capabilities: {caps}\n"
                f"Interface: {local}, Port ID (outgoing port): {remote}\nHoldtime: 151 sec\n")
    return (f"-------------------------\nDevice ID: {name}.lgcc.example.edu\nEntry address(es): \n"
            f"  IP address: {ip}\nPlatform: {platform},  Capabilities: {caps}\n"
            f"Interface: {local},  Port ID (outgoing port): {remote}\nHoldtime : 150 sec\n\n"
            f"Version :\nCisco IOS Software\n")


def _platform(info):
    if info["type"] == "cisco_nxos":
        return "N7K-C7010", "Router Switch IGMP Filtering Supports-STP-Dispute"
    return "cisco " + info["model"], "Switch IGMP"


def demo_results():
    """[{"switch", "outputs", "problem"}] as if every switch on SWITCHES had been read."""
    info = _info()
    cdp, lldp = {n: [] for n in info}, {n: [] for n in info}
    for a, pa, b, pb, how in LINKS:
        for here, local, there, remote in ((a, pa, b, pb), (b, pb, a, pa)):
            if how == "cdp":
                plat, caps = _platform(info[there])
                cdp[here].append(_cdp_block(there, info[there]["address"], plat, caps, local, remote,
                                            info[here]["type"] == "cisco_nxos"))
            else:
                lldp[here].append((local, there, remote))
    for here, local, name, platform, caps, remote in NOT_LISTED:
        cdp[here].append(_cdp_block(name, "10.255.0." + str(len(name)), platform, caps, local, remote,
                                    info[here]["type"] == "cisco_nxos"))

    results = []
    for n, (name, d) in enumerate(info.items(), start=1):
        switch = {"address": d["address"], "type": d["type"], "protocol": "telnet",
                  "building": d["building"], "location": d["location"]}
        if d["address"] in REFUSES_LOGIN:
            results.append({"switch": switch, "outputs": None,
                            "problem": "login failed (check the username and password)"})
            continue
        out = {"prompt": name, "macs": "", "arp": ""}
        if d["type"] == "cisco_nxos":
            out["version"] = (f"Cisco Nexus Operating System (NX-OS) Software\n  cisco {d['model']} (10 Slot) Chassis "
                              f"(\"Supervisor Module-2\")\n  Device name: {name}\n")
            out["cdp"] = "".join(cdp[name])
            out["lldp"] = "".join(
                f"Chassis id: {_dotted(99)}\nPort id: {remote}\nLocal Port id: {local.replace('Ethernet', 'Eth')}\n"
                f"Port Description: {remote}\nSystem Name: {there}\n"
                f"System Description: ArubaOS (MODEL: {info[there]['model']}), Version 7.4.1.9\n"
                f"Time remaining: 98 seconds\nSystem Capabilities: B, R\nEnabled Capabilities: B\n"
                f"Management Address: {info[there]['address']}\n\n" for local, there, remote in lldp[name])
        elif d["type"] == "cisco_ios":
            out["version"] = (f"{name} uptime is 2 years\ncisco {d['model']} (MPC8572) processor (revision 10)\n"
                              if "45" in d["model"] else
                              f"{name} uptime is 1 year, 2 weeks\nModel Number                       : {d['model']}\n"
                              f"Base Ethernet MAC Address          : {_mac(n)}\n")
            out["cdp"] = "".join(cdp[name])
            out["lldp"] = ""
        elif d["type"] == "dell_powerconnect":
            out["version"] = (f"System Description:  Dell PowerConnect 5548P\nSystem Name:  {name}\n"
                              f"System MAC Address:  {_mac(n)}\n")
            out["lldp"] = ("  Port         Device ID          Port ID          System Name    Capabilities    TTL\n"
                           "---------  -----------------  ---------------  -------------  --------------  -----\n")
        else:
            out["version"] = f"ArubaOS (MODEL: {d['model']}), Version 7.4.1.9\n"
            out["lldp"] = ("Capability codes: (R)Router, (B)Bridge, (A)Access Point, (P)Phone, (O)Other\n"
                           "LLDP Neighbor Information\n-------------------------\n"
                           "Local Intf  Chassis ID         Capability  Remote Intf    Expiry-Time (Secs)  System Name\n"
                           "----------  ----------         ----------  -----------    ------------------  -----------\n"
                           + "".join(f"{local:<11} {_mac(200):<18} B:R         {remote:<14} 98                  {there}\n"
                                     for local, there, remote in lldp[name]))
        results.append({"switch": switch, "outputs": out, "problem": ""})

    # The Dell nobody reports: its MAC is in CORE-1's table on the port it's plugged into. CORE-2 sees it
    # too, but on its link to CORE-1, so that isn't taken as the Dell's port.
    by_name = {r["outputs"]["prompt"]: r for r in results if r["outputs"]}
    dell_n = list(info).index(DELL) + 1
    core, port = DELL_UPLINK
    rows = [f"*   99     {_dotted(dell_n)}   dynamic  0         F      F    {port}"]
    rows += [f"*   20     {_dotted(1000 + i)}   dynamic  0         F      F    {port}" for i in range(30)]
    rows += [f"*   20     {_dotted(2000 + i)}   dynamic  0         F      F    Ethernet2/1" for i in range(60)]
    by_name[core]["outputs"]["macs"] = "\n".join(rows) + "\n"
    by_name[core]["outputs"]["arp"] = f"{info[DELL]['address']}   00:05:22  {_dotted(dell_n)}  Vlan99\n"
    by_name["CORE-2"]["outputs"]["macs"] = f"*   99     {_dotted(dell_n)}   dynamic  0         F      F    Ethernet1/1\n"
    return results


def demo_switch_list():
    return [{"address": s[0], "type": s[1], "protocol": "telnet", "building": s[2], "location": s[3]} for s in SWITCHES]
