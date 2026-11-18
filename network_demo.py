"""
network_demo.py - A made-up campus, as Observium's database would hold it, for the
Network map's sample page and tests.

  * two Nexus 7000 cores, a firewall Observium monitors, and an edge router it doesn't
  * distribution: B and E are Catalyst 4507s, C is a pair of Catalyst 9500s
  * closets: Catalysts (one daisy-chained), a Dell that only reports the link from its own
    end, an Aruba, one switch that's down, one closet straight off the core
  * things the map leaves out: phones, access points, a VMware host Observium monitors
"""

DOMAIN = ".lgcc.example.edu"

# name, ip, building, location, model, os, type, on the switch list
DEVICES = [
    ("CORE-1", "10.0.0.1", "M Building", "Main MDF", "N7K-C7010", "nxos", "network"),
    ("CORE-2", "10.0.0.2", "M Building", "Main MDF", "N7K-C7010", "nxos", "network"),
    ("FW-1", "10.0.0.5", "M Building", "Main MDF", "ASA5525", "asa", "firewall"),
    ("B-DIST", "10.2.0.1", "B Building", "B MDF", "WS-C4507R+E", "ios", "network"),
    ("C-DIST-1", "10.3.0.1", "C Building", "C MDF", "C9500-24Y4C", "iosxe", "network"),
    ("C-DIST-2", "10.3.0.2", "C Building", "C MDF", "C9500-24Y4C", "iosxe", "network"),
    ("E-DIST", "10.5.0.1", "E Building", "E MDF", "WS-C4507R+E", "ios", "network"),
    ("M1-IDF", "10.0.1.11", "M Building", "M1 IDF", "C9300-48P", "iosxe", "network"),
    ("VMHOST-1", "10.3.9.10", "C Building", "C MDF", "VMware ESXi", "vmware", "server"),
]
CLOSETS = {   # building: (distribution switch(es), how many closets)
    "B": (["B-DIST"], 6),
    "C": (["C-DIST-1", "C-DIST-2"], 8),
    "E": (["E-DIST"], 10),
}
DOWN = {"B4-IDF"}


def _closets():
    out = []
    for b, (_, count) in CLOSETS.items():
        for i in range(1, count + 1):
            name = f"{b}{i}-IDF"
            model = ("PowerConnect 5548P" if name == "B2-IDF" else "ArubaS3500-48P" if name == "C4-IDF"
                     else "WS-C2960X-48FPD-L" if b == "B" else "WS-C3650-48PD" if b == "E" and i % 2 else "C9300-48P")
            os_ = "powerconnect" if "Power" in model else "arubaos" if "Aruba" in model else "ios"
            out.append((name, f"10.{ {'B': 2, 'C': 3, 'E': 5}[b]}.1.{10 + i}", f"{b} Building", f"{b}{i} IDF", model, os_,
                        "network"))
    out.append(("B3-IDF-2", "10.2.1.30", "B Building", "B3 IDF", "WS-C2960X-48FPD-L", "ios", "network"))
    return out


def demo_observium():
    """{"devices", "ports", "neighbours"} as network.read_observium returns them."""
    rows = DEVICES + _closets()
    devices, ids = [], {}
    for n, (name, ip, _, loc, model, os_, kind) in enumerate(rows, start=1):
        ids[name] = n
        devices.append({"device_id": n, "hostname": (name.lower() + DOMAIN) if name.startswith("CORE") else ip,
                        "sysName": name + DOMAIN, "ip": ip, "hardware": model, "os": os_, "type": kind,
                        "location": f"LaGuardia, {loc}", "status": 0 if name in DOWN else 1, "disabled": 0,
                        "ignore": 0})
    ports, neighbours = {}, []

    def port(device, name, descr=None):
        pid = len(ports) + 1
        ports[pid] = {"port_id": pid, "device_id": ids[device], "ifName": name, "ifDescr": descr or name,
                      "ifAlias": ""}
        return pid

    def link(a, pa, b, pb, protocol="cdp", both=True):
        """Observium records a link from each end that reports it."""
        ia, ib = port(a, pa), port(b, pb)
        neighbours.append(_row(ids[a], ia, protocol, ib, b, pb))
        if both:
            neighbours.append(_row(ids[b], ib, protocol, ia, a, pa))

    link("CORE-1", "Eth1/1", "CORE-2", "Eth1/1")
    link("CORE-1", "Eth1/2", "CORE-2", "Eth1/2")
    link("CORE-1", "Eth4/1", "FW-1", "Gi0/0", "lldp")
    link("CORE-2", "Eth4/1", "FW-1", "Gi0/1", "lldp")
    link("CORE-1", "Eth3/1", "M1-IDF", "Te1/1/1")
    for n, dist in enumerate(["B-DIST", "C-DIST-1", "C-DIST-2", "E-DIST"], start=1):
        link("CORE-1", f"Eth2/{n}", dist, "Te1/1" if "4507" in devices[ids[dist] - 1]["hardware"] else "Te1/0/1")
        link("CORE-2", f"Eth2/{n}", dist, "Te2/1" if "4507" in devices[ids[dist] - 1]["hardware"] else "Te1/0/2")
    link("C-DIST-1", "Te1/0/24", "C-DIST-2", "Te1/0/24")
    for b, (dists, count) in CLOSETS.items():
        for i in range(1, count + 1):
            closet = f"{b}{i}-IDF"
            for d, dist in enumerate(dists):
                here = f"Gi3/{i}" if "4507" in devices[ids[dist] - 1]["hardware"] else f"Te1/0/{4 + i}"
                there = "GE0/0/47" if closet == "C4-IDF" else "gi1/0/49" if closet == "B2-IDF" else f"Te{d + 1}/1/1"
                if closet == "C4-IDF" and d == 1:
                    continue                                      # the Aruba has one uplink
                if closet == "B2-IDF":                            # only the Dell reports this one (by LLDP)
                    link(closet, there, dist, here, "lldp", both=False)
                elif closet == "C4-IDF":
                    link(dist, here, closet, there, "lldp")
                else:
                    link(dist, here, closet, there)
    link("B3-IDF", "Gi1/0/50", "B3-IDF-2", "Gi1/0/49")
    link("C-DIST-1", "Te1/0/20", "VMHOST-1", "vmnic0", "cdp")
    # Neighbours Observium doesn't monitor: the edge router (kept), a phone and an access point (left out)
    for core, p, far in (("CORE-1", "Eth5/1", "Gi0/0/0"), ("CORE-2", "Eth5/1", "Gi0/0/1")):
        neighbours.append(_row(ids[core], port(core, p), "cdp", None, "INET-RTR-1" + DOMAIN, far,
                               "cisco ISR4451-X/K9", "Cisco IOS XE Software, Version 17.03.04a", "10.255.0.1"))
    neighbours.append(_row(ids["E3-IDF"], port("E3-IDF", "Gi1/0/1"), "cdp", None, "SEP00112233AA01", "Port 1",
                           "Cisco IP Phone 8845", "sip88xx.14-1-1", "10.5.40.21"))
    neighbours.append(_row(ids["E3-IDF"], port("E3-IDF", "Gi1/0/28"), "cdp", None, "AP-E1-28", "GigabitEthernet0",
                           "cisco AIR-AP2802I-B-K9", "Cisco AP Software", "10.5.60.28"))
    return {"devices": devices, "ports": ports, "neighbours": neighbours}


def _row(device_id, port_id, protocol, remote_port_id, remote_hostname, remote_port, platform="", version="",
         address=""):
    return {"device_id": device_id, "port_id": port_id, "protocol": protocol, "remote_port_id": remote_port_id,
            "remote_hostname": remote_hostname if "." in remote_hostname or remote_port_id is None
            else remote_hostname + DOMAIN, "remote_port": remote_port, "remote_platform": platform,
            "remote_version": version, "remote_address": address, "active": 1}


def demo_switch_list():
    """The Port Analyzer switch list for the made-up campus (by IP address, like most lists)."""
    rows = [r for r in DEVICES if r[6] == "network"] + _closets()
    return [{"address": ip, "type": "dell_powerconnect" if "Power" in model else "aruba_mas" if "Aruba" in model
             else "cisco_nxos" if os_ == "nxos" else "cisco_ios", "protocol": "telnet", "building": building,
             "location": loc} for name, ip, building, loc, model, os_, _ in rows]
