"""
A made-up campus for the Device tracker's demo page: five daily scans, so the last one shows every kind of
change (moved, new, missing, back, VLAN changed). Addresses and names are invented.
"""

import random
from datetime import datetime, timedelta

import tracker

SWITCHES = [
    {"address": "10.0.1.11", "name": "C449_2960S", "type": "cisco_ios", "building": "C Building", "location": "C449"},
    {"address": "10.0.1.12", "name": "C510_2960S", "type": "cisco_ios", "building": "C Building", "location": "C510"},
    {"address": "10.0.2.12", "name": "B2-IDF", "type": "dell_powerconnect", "building": "B Building", "location": "B2 IDF"},
    {"address": "10.0.5.13", "name": "E3-IDF", "type": "cisco_ios", "building": "E Building", "location": "E3 IDF"},
]
CORE = {"address": "10.0.0.1", "name": "CORE-1", "type": "cisco_nxos", "building": "Main", "location": "MDF"}
DATA, VOICE = 10, 40


def _mac(rng, prefix):
    return prefix + "." + ".".join(f"{rng.randrange(65536):04x}" for _ in range(2))


def _base(rng):
    """Where everything is on day 1: {switch address: {port: [devices]}}"""
    campus = {s["address"]: {} for s in SWITCHES}
    special = {
        "phone-b2": {"kind": "phone", "mac": "0004.f2a1.0005", "name": "", "model": "Polycom VVX-411", "ip": "10.0.40.55",
                     "lldp": True},
        "pc-adm": {"kind": "pc", "mac": "a4bb.6d1c.0007", "name": "DESKTOP-ADM-07", "lldp": True},
        "printer": {"kind": "unknown", "mac": "0017.c8aa.0101", "ip": "10.0.10.24"},
        "ap-c510": {"kind": "ap", "mac": "7069.5a10.0203", "name": "AP-C510-2", "model": "C9120AXI-B",
                    "ip": "10.0.50.12", "lldp": True},
        "plotter": {"kind": "unknown", "mac": "3c2a.f4aa.0404"},           # unplugged for a few days, then back
        "kiosk": {"kind": "unknown", "mac": "00e0.4caa.0909"},             # goes missing
        "lab-pc": {"kind": "pc", "mac": "a4bb.6d1c.0011", "name": "LAB-E3-11", "lldp": True},
    }
    places = {"phone-b2": ("10.0.2.12", "Gi1/0/5"), "pc-adm": ("10.0.1.11", "Gi1/0/22"),
              "printer": ("10.0.1.11", "Gi1/0/4"), "ap-c510": ("10.0.1.12", "Gi1/0/48"),
              "plotter": ("10.0.5.13", "Gi1/0/31"), "kiosk": ("10.0.1.12", "Gi1/0/12"),
              "lab-pc": ("10.0.5.13", "Gi1/0/14")}
    for key, (sw, port) in places.items():
        campus[sw].setdefault(port, []).append(dict(special[key], id=key))
    for s in SWITCHES:
        used = set(campus[s["address"]])
        for i in range(1, 47):
            port = f"Gi1/0/{i}"
            if port in used or rng.random() < 0.3:
                continue
            if rng.random() < 0.55:          # a phone with a PC behind it
                campus[s["address"]][port] = [
                    {"kind": "phone", "mac": _mac(rng, "0011"), "name": f"SEP0011{rng.randrange(16**8):08X}",
                     "model": "Cisco IP Phone 8845", "cdp": True},
                    {"kind": "unknown", "mac": _mac(rng, "a4bb")}]
            else:
                campus[s["address"]][port] = [{"kind": "unknown", "mac": _mac(rng, rng.choice(["a4bb", "0017", "accc"]))}]
    return campus


def _results(campus):
    results = []
    for s in SWITCHES:
        r = {"address": s["address"], "name": s["name"], "type": s["type"], "building": s["building"],
             "location": s["location"], "neighbors": [], "macs": [], "arp": {}, "port_vlan": {}, "aliases": {},
             "problem": "", "notes": ["MAC table: one per VLAN (Cisco, 4 VLANs)"], "lldp_ok": True, "macs_ok": True,
             "mac_problem": "", "ports": 52}
        for port, devs in campus[s["address"]].items():
            r["port_vlan"][port] = DATA
            for d in devs:
                vlan = VOICE if d["kind"] == "phone" else d.get("vlan", DATA)
                if d["kind"] != "ap":
                    r["macs"].append({"mac": d["mac"], "port": port, "vlan": vlan})
                if d.get("cdp"):
                    r["neighbors"].append({"local": port, "source": "CDP", "name": d["name"], "chassis": "",
                                           "port": "Port 1", "ip": "", "caps": "Host Phone", "platform": d["model"],
                                           "model": "", "kind": "phone"})
                elif d.get("lldp"):
                    caps = {"phone": "B T", "ap": "B W", "pc": ""}[d["kind"]]
                    r["neighbors"].append({"local": port, "source": "LLDP", "name": d.get("name", ""),
                                           "chassis": d["mac"], "port": d["mac"], "ip": d.get("ip", ""), "caps": caps,
                                           "platform": d.get("model", ""), "model": "", "kind": d["kind"]})
                if d.get("ip") and d["kind"] == "unknown":
                    r["arp"][d["mac"]] = d["ip"]
        r["macs"] += [{"mac": f"00de.fb00.{i:04x}", "port": "Gi1/0/52", "vlan": DATA} for i in range(60)]   # uplink
        r["neighbors"].append({"local": "Gi1/0/52", "source": "CDP", "name": "CORE-1", "chassis": "",
                               "port": "Ethernet4/1", "ip": "10.0.0.1", "caps": "Router Switch IGMP",
                               "platform": "N7K-C7010", "model": "", "kind": "network"})
        r["aliases"] = {"Gi1/0/4": "C449 printer", "Gi1/0/52": "uplink to core"} if s["name"] == "C449_2960S" else {}
        results.append(r)
    return results


def _move(campus, key, to_switch, to_port):
    for ports in campus.values():
        for port, devs in list(ports.items()):
            for d in list(devs):
                if d.get("id") == key:
                    devs.remove(d)
                    if not devs:
                        del ports[port]
                    campus[to_switch].setdefault(to_port, []).append(d)
                    return d


def _take(campus, key):
    for ports in campus.values():
        for port, devs in ports.items():
            for d in devs:
                if d.get("id") == key:
                    devs.remove(d)
                    return port, d


def run_demo():
    rng = random.Random(7)
    campus = _base(rng)
    history = {"version": 1, "scans": [], "devices": {}}
    settings = dict(tracker.DEFAULT_SETTINGS)
    start = datetime(2026, 11, 20, 10, 0)
    away = {}
    for day in range(5):
        now = start + timedelta(days=day)
        if day == 1:                                     # the plotter is unplugged for three scans
            away["plotter"] = _take(campus, "plotter")
        if day == 2:
            away["kiosk"] = _take(campus, "kiosk")       # the kiosk is gone from here on
        if day == 4:
            port, d = away.pop("plotter")
            campus["10.0.5.13"].setdefault(port, []).append(d)
            _move(campus, "printer", "10.0.1.12", "Gi1/0/17")
            _move(campus, "phone-b2", "10.0.5.13", "Gi1/0/9")
            _move(campus, "pc-adm", "10.0.1.12", "Gi1/0/30")
            campus["10.0.5.13"]["Gi1/0/47"] = [{"kind": "unknown", "mac": "accc.8e55.0047"}]
            campus["10.0.1.11"]["Gi1/0/47"] = [{"kind": "ap", "mac": "7069.5a10.0447", "name": "AP-C449-3",
                                                "model": "C9120AXI-B", "ip": "10.0.50.31", "lldp": True}]
            for devs in campus["10.0.5.13"].values():
                for d in devs:
                    if d.get("id") == "lab-pc":
                        d["vlan"] = 30                    # the lab PC's port was moved to the lab VLAN
        results = _results(campus)
        devices = tracker.devices_from(results, SWITCHES + [CORE], settings)
        changes, history, baseline = tracker.compare(devices, results, history, settings, now)
    _summary, page = tracker.page_data(results, devices, changes, history, baseline, "scheduled", now)
    page["demo"] = True
    return page
