"""
network_demo.py - A made-up campus for the Network map's sample page and tests.

What the switches print is generated, so the sample goes through the same reading as
real switches:
  * two Nexus 7000 cores and an edge router that isn't on the list
  * four distribution switches (B and E: Catalyst 4507; C: a pair of Catalyst 9500)
  * Cisco access switches (one daisy-chained), one directly on the core, an Aruba seen
    by LLDP, a Dell nothing reports (found by its MAC address), one that refuses the login
  * phones and access points in the CDP output, which the map leaves out
"""

SWITCHES = [
    # address, type, building, location, hostname, model
    ("10.0.0.1", "cisco_nxos", "M Building", "Main MDF", "CORE-1", "Nexus7000 C7010"),
    ("10.0.0.2", "cisco_nxos", "M Building", "Main MDF", "CORE-2", "Nexus7000 C7010"),
    ("10.2.0.1", "cisco_ios", "B Building", "B MDF", "B-DIST", "WS-C4507R+E"),
    ("10.3.0.1", "cisco_ios", "C Building", "C MDF", "C-DIST-1", "C9500-24Y4C"),
    ("10.3.0.2", "cisco_ios", "C Building", "C MDF", "C-DIST-2", "C9500-24Y4C"),
    ("10.5.0.1", "cisco_ios", "E Building", "E MDF", "E-DIST", "WS-C4507R+E"),
    ("10.2.1.11", "cisco_ios", "B Building", "B1 IDF", "B1-IDF", "C9300-48P"),
    ("10.2.1.12", "dell_powerconnect", "B Building", "B2 IDF", "B2-IDF", "5548P"),
    ("10.2.1.13", "cisco_ios", "B Building", "B3 IDF", "B3-IDF", "WS-C2960X-48FPD-L"),
    ("10.2.1.14", "cisco_ios", "B Building", "B3 IDF", "B3-IDF-2", "WS-C2960X-48FPD-L"),
    ("10.2.1.15", "cisco_ios", "B Building", "B4 IDF", "B4-IDF", "C9300-48P"),
    ("10.3.1.11", "cisco_ios", "C Building", "C1 IDF", "C1-IDF", "C9300-48P"),
    ("10.3.1.12", "cisco_ios", "C Building", "C2 IDF", "C2-IDF", "C9300-48P"),
    ("10.3.1.13", "cisco_ios", "C Building", "C3 IDF", "C3-IDF", "C9300-24P"),
    ("10.3.1.14", "aruba_mas", "C Building", "C4 IDF", "C4-IDF", "ArubaS3500-48P"),
    ("10.5.1.11", "cisco_ios", "E Building", "E1 IDF", "E1-IDF", "WS-C3650-48PD"),
    ("10.5.1.12", "cisco_ios", "E Building", "E2 IDF", "E2-IDF", "WS-C3650-48PD"),
    ("10.5.1.13", "cisco_ios", "E Building", "E3 IDF", "E3-IDF", "C9300-48P"),
    ("10.0.1.11", "cisco_ios", "M Building", "M1 IDF", "M1-IDF", "C9300-48P"),
]
REFUSES_LOGIN = {"10.2.1.15"}

# (switch A, port on A, switch B, port on B, how: "cdp" both ends, "lldp" both ends)
LINKS = [
    ("CORE-1", "Ethernet1/1", "CORE-2", "Ethernet1/1", "cdp"),
    ("CORE-1", "Ethernet1/2", "CORE-2", "Ethernet1/2", "cdp"),
    ("CORE-1", "Ethernet2/1", "B-DIST", "TenGigabitEthernet1/1", "cdp"),
    ("CORE-2", "Ethernet2/1", "B-DIST", "TenGigabitEthernet2/1", "cdp"),
    ("CORE-1", "Ethernet2/2", "C-DIST-1", "TenGigabitEthernet1/0/1", "cdp"),
    ("CORE-2", "Ethernet2/2", "C-DIST-1", "TenGigabitEthernet1/0/2", "cdp"),
    ("CORE-1", "Ethernet2/3", "C-DIST-2", "TenGigabitEthernet1/0/1", "cdp"),
    ("CORE-2", "Ethernet2/3", "C-DIST-2", "TenGigabitEthernet1/0/2", "cdp"),
    ("C-DIST-1", "TenGigabitEthernet1/0/24", "C-DIST-2", "TenGigabitEthernet1/0/24", "cdp"),
    ("CORE-1", "Ethernet2/4", "E-DIST", "TenGigabitEthernet1/1", "cdp"),
    ("CORE-2", "Ethernet2/4", "E-DIST", "TenGigabitEthernet2/1", "cdp"),
    ("CORE-1", "Ethernet3/1", "M1-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("B-DIST", "GigabitEthernet3/1", "B1-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("B-DIST", "GigabitEthernet3/3", "B3-IDF", "GigabitEthernet1/0/49", "cdp"),
    ("B3-IDF", "GigabitEthernet1/0/50", "B3-IDF-2", "GigabitEthernet1/0/49", "cdp"),
    ("B-DIST", "GigabitEthernet3/5", "B4-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("C-DIST-1", "TenGigabitEthernet1/0/5", "C1-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("C-DIST-2", "TenGigabitEthernet1/0/5", "C1-IDF", "TenGigabitEthernet2/1/1", "cdp"),
    ("C-DIST-1", "TenGigabitEthernet1/0/6", "C2-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("C-DIST-2", "TenGigabitEthernet1/0/6", "C2-IDF", "TenGigabitEthernet2/1/1", "cdp"),
    ("C-DIST-1", "TenGigabitEthernet1/0/7", "C3-IDF", "TenGigabitEthernet1/1/1", "cdp"),
    ("C-DIST-2", "TenGigabitEthernet1/0/7", "C3-IDF", "TenGigabitEthernet1/1/2", "cdp"),
    ("C-DIST-1", "GigabitEthernet1/0/8", "C4-IDF", "GE0/0/47", "lldp"),
    ("E-DIST", "GigabitEthernet3/1", "E1-IDF", "GigabitEthernet1/1/1", "cdp"),
    ("E-DIST", "GigabitEthernet3/2", "E2-IDF", "GigabitEthernet1/1/1", "cdp"),
    ("E-DIST", "GigabitEthernet3/3", "E3-IDF", "TenGigabitEthernet1/1/1", "cdp"),
]
# Not on the list: seen by CDP from the cores
EDGE = [("CORE-1", "Ethernet4/1", "GigabitEthernet0/0/0"), ("CORE-2", "Ethernet4/1", "GigabitEthernet0/0/1")]
DELL = "B2-IDF"
DELL_UPLINK = ("B-DIST", "GigabitEthernet3/2")           # nothing reports this link


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
        return (f"----------------------------------------\nDevice ID:{name}(FOX1{abs(hash(name)) % 10**6:06d})\n"
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
    return "cisco " + info["model"], "Router Switch IGMP" if "45" in info["model"] or "95" in info["model"] \
        else "Switch IGMP"


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
    for core, local, remote in EDGE:
        cdp[core].append(_cdp_block("INET-RTR-1", "10.255.0.1", "cisco ISR4451-X/K9", "Router IGMP", local, remote,
                                    True))
    # Phones and access points the map must leave out
    cdp["E3-IDF"].append(_cdp_block("SEP00112233AA01", "10.5.40.21", "Cisco IP Phone 8845",
                                    "Host Phone Two-port Mac Relay", "GigabitEthernet1/0/1", "Port 1", False))
    cdp["E3-IDF"].append(_cdp_block("AP-E1-28", "10.5.60.28", "cisco AIR-AP2802I-B-K9", "Router Trans-Bridge",
                                    "GigabitEthernet1/0/28", "GigabitEthernet0", False))

    results = []
    for n, (name, d) in enumerate(info.items(), start=1):
        switch = {"address": d["address"], "type": d["type"], "protocol": "telnet",
                  "building": d["building"], "location": d["location"]}
        if d["address"] in REFUSES_LOGIN:
            results.append({"switch": switch, "outputs": None,
                            "problem": "login failed (check the username and password)"})
            continue
        out = {"prompt": name}
        if d["type"] == "cisco_nxos":
            out["version"] = (f"Cisco Nexus Operating System (NX-OS) Software\n  cisco {d['model']} (10 Slot) Chassis "
                              f"(\"Supervisor Module-2\")\n  Device name: {name}\n")
            out["cdp"] = "".join(cdp[name])
            out["lldp"] = ""
        elif d["type"] == "cisco_ios":
            out["version"] = (f"{name} uptime is 1 year, 2 weeks\nModel Number                       : {d['model']}\n"
                              f"Base Ethernet MAC Address          : {_mac(n)}\n") if not d["model"].startswith("WS-C45") \
                else (f"{name} uptime is 2 years\ncisco {d['model']} (MPC8572) processor (revision 10) with 4194304K\n")
            out["cdp"] = "".join(cdp[name])
            out["lldp"] = "".join(
                f"------------------------------------------------\nLocal Intf: {local}\nChassis id: {_dotted(99)}\n"
                f"Port id: {remote}\nPort Description: {remote}\nSystem Name: {there}\n\nSystem Description: \n"
                f"ArubaOS (MODEL: {info[there]['model']}), Version 7.4.1.9\n\nTime remaining: 98 seconds\n"
                f"System Capabilities: B,R\nEnabled Capabilities: B\nManagement Addresses:\n"
                f"    IP: {info[there]['address']}\n" for local, there, remote in lldp[name])
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
        out["macs"], out["arp"] = "", ""
        results.append({"switch": switch, "outputs": out, "problem": ""})

    # The Dell nobody reports: its MAC is in B-DIST's table on the port it's plugged into
    by_name = {r["outputs"]["prompt"]: r for r in results if r["outputs"]}
    dell_n = list(info).index(DELL) + 1
    dist, port = DELL_UPLINK
    rows = [f"  99    {_dotted(dell_n)}    dynamic ip,ipx,assigned,other {port}"]
    rows += [f"  20    {_dotted(1000 + i)}    dynamic ip,ipx,assigned,other {port}" for i in range(30)]
    rows += [f"  20    {_dotted(2000 + i)}    dynamic ip,ipx,assigned,other GigabitEthernet3/1" for i in range(60)]
    by_name[dist]["outputs"]["macs"] = "Unicast Entries\n vlan   mac address     type        protocols   port\n" + \
        "\n".join(rows) + "\n"
    by_name[dist]["outputs"]["arp"] = (f"Protocol  Address          Age (min)  Hardware Addr   Type   Interface\n"
                                       f"Internet  {info[DELL]['address']:<16} 3          {_dotted(dell_n)}  ARPA   Vlan99\n")
    # The core also sees the Dell's MAC, but on its link to B-DIST, so that's not taken as the Dell's port
    by_name["CORE-1"]["outputs"]["macs"] = (f"*   99     {_dotted(dell_n)}   dynamic  0         F      F    Ethernet2/1\n")
    return results


def demo_switch_list():
    return [{"address": s[0], "type": s[1], "protocol": "telnet", "building": s[2], "location": s[3]} for s in SWITCHES]
