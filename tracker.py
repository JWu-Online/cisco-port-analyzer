"""
Device tracker: read every switch over SNMP (read-only) and write down where each device is plugged in, then
compare with what was known before to see what moved.

Two ways a device is found on a port:
  * LLDP/CDP: phones, access points, switches and PCs that announce themselves (name, model, IP).
  * MAC table: every MAC address a switch has learned on a port, which catches the quiet ones too
    (printers, cameras, most PCs).
A MAC address seen on a port that leads to another switch or an access point isn't where that device is
(it's only passing through), so those ports are left out, and so is a port with more MACs than the
"busy port" limit (most likely an uplink that didn't say so).

Each scan writes devices.csv (every device and where it is) and changes.csv (what's different from before):
  Moved         on a different switch or port than the last place it was seen
  New           never seen before
  Missing       not seen for N scans in a row (default 3), on a switch that was read fine
  Back          seen again after being Missing
  VLAN changed  same port, different VLAN

Run it on a schedule with TrackDevices.bat (Windows Task Scheduler; the Device tracker page sets that up),
or by hand:  python tracker.py
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import analyzer
import lldp
import network
import portdocs
import snmpwalk

APP_DIR = Path(__file__).resolve().parent
KEYRING_SERVICE = "Port Analyzer device tracker"
SECRETS = ("community", "auth_password", "priv_password")
DEFAULT_SETTINGS = {
    "version": "2c", "username": "", "auth_protocol": "SHA", "priv_protocol": "AES",
    "time": "10:00", "days": "daily",           # when the scheduled scan runs: daily or weekdays
    "missing_after": 3,                          # scans in a row before a device counts as Missing
    "busy_limit": 40,                            # a port with more MACs than this is treated as an uplink
    "forget_days": 180,                          # a device not seen for this long is dropped from the history
    "keep_days": 90,                             # scan folders older than this are deleted
    "parallel": 10, "timeout": 3, "retries": 1,
    "dhcp_servers": "",                          # Windows DHCP servers to name MAC-only devices from (none built in)
}
CHANGES = {"moved": "Moved", "new": "New", "missing": "Missing", "back": "Back", "vlan": "VLAN changed"}
KINDS = {**lldp.KINDS, "unknown": "MAC address only"}

# ---------------------------------------------------------------------------
# What's asked for (standard tables, the same on Cisco, Dell and Aruba, plus Cisco's CDP and VLAN tables)
# ---------------------------------------------------------------------------
SYS_NAME = "1.3.6.1.2.1.1.5"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
LLDP_LOC_PORT = "1.0.8802.1.1.2.1.3.7.1"        # .2 id type  .3 id  .4 description
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"              # .4/.5 chassis  .6/.7 port  .8 port descr  .9 name  .10 descr  .12 caps
LLDP_REM_ADDR = "1.0.8802.1.1.2.1.4.2.1.3"       # the management address is in the index
CDP_CACHE = "1.3.6.1.4.1.9.9.23.1.2.1.1"         # .4 address  .6 device id  .7 its port  .8 platform  .9 caps
QBRIDGE_FDB = "1.3.6.1.2.1.17.7.1.2.2.1"         # (fdb id, mac) -> .2 bridge port  .3 status
QBRIDGE_VLAN_FDB = "1.3.6.1.2.1.17.7.1.4.2.1.3"  # (time, vlan) -> fdb id
QBRIDGE_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"      # bridge port -> its untagged VLAN
BRIDGE_FDB = "1.3.6.1.2.1.17.4.3.1"              # mac -> .2 bridge port  .3 status
BRIDGE_PORTS = "1.3.6.1.2.1.17.1.4.1.2"          # bridge port -> ifIndex
CISCO_VLANS = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"     # (domain, vlan) -> 1 when it's up
CISCO_PORT_VLAN = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"  # ifIndex -> access VLAN
ARP = "1.3.6.1.2.1.4.22.1.2"                      # (ifIndex, ip) -> mac

LEARNED, MGMT_SELF = 3, (4, 5)
LLDP_CAPS = "OPBWRTCS"      # bit 0 other, repeater, bridge, WLAN access point, router, telephone, DOCSIS, station
CDP_CAPS = ((0x01, "Router"), (0x02, "Trans-Bridge"), (0x04, "Source-Route-Bridge"), (0x08, "Switch"),
            (0x10, "Host"), (0x20, "IGMP"), (0x40, "Repeater"), (0x80, "Phone"))
_SKIP = analyzer.SKIP_PORTS


def _text(value):
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = "" if value is None else str(value)
    return text.replace("\x00", "").strip()


def _mac_bytes(value):
    """6 bytes -> 0011.2233.4455 (also a MAC sent as text)."""
    if isinstance(value, bytes) and len(value) == 6:
        return portdocs.mac_dotted(value.hex())
    return lldp._mac(_text(value))


def _mac_index(numbers):
    return portdocs.mac_dotted(bytes(numbers).hex()) if len(numbers) == 6 and all(0 <= n < 256 for n in numbers) else ""


def _ip_bytes(value):
    return ".".join(str(b) for b in value) if isinstance(value, bytes) and len(value) == 4 else ""


def _lldp_id(subtype, value, port=False):
    """An LLDP chassis or port id, by its type: a MAC (chassis type 4, port type 3), an address (5 / 4), or text."""
    mac_type, address_type = (3, 4) if port else (4, 5)
    if subtype == mac_type and isinstance(value, bytes) and len(value) == 6:
        return portdocs.mac_dotted(value.hex())
    if subtype == address_type and isinstance(value, bytes) and len(value) == 5 and value[0] == 1:     # IPv4
        return _ip_bytes(value[1:])
    text = _text(value)
    if isinstance(value, bytes) and len(value) == 6 and not text.isprintable():
        return portdocs.mac_dotted(value.hex())
    return text


def _bits(value, letters):
    """SNMP BITS (bit 0 = the first byte's top bit) -> 'B R' style letters."""
    if not isinstance(value, bytes):
        return ""
    out = []
    for i, letter in enumerate(letters):
        byte = i // 8
        if byte < len(value) and value[byte] & (0x80 >> (i % 8)):
            out.append(letter)
    return " ".join(out)


def _cdp_caps(value):
    if isinstance(value, bytes):
        text = _text(value)
        if re.fullmatch(r"(?i)(0x)?[0-9a-f ]{2,}", text):               # some send it as hex text
            number = int(re.sub(r"(?i)0x|\s", "", text), 16)
        else:
            number = int.from_bytes(value[-4:], "big")
    else:
        number = int(value or 0)
    return " ".join(word for bit, word in CDP_CAPS if number & bit)


# ---------------------------------------------------------------------------
# Reading one switch
# ---------------------------------------------------------------------------
def _table(rows):
    """[((column, *index), value)] -> {index: {column: value}}"""
    out = defaultdict(dict)
    for idx, value in rows:
        if idx:
            out[tuple(idx[1:])][idx[0]] = value
    return out


def _column(rows):
    return {idx: value for idx, value in rows}


def read_switch(walk, switch, timeout_note=""):
    """Everything the tracker needs from one switch. `walk(oid, vlan=None)` returns the table's rows (so tests
    can hand it made-up tables)."""
    out = {"address": switch["address"], "name": switch.get("name") or switch["address"],
           "type": switch.get("type", ""), "building": switch.get("building", ""),
           "location": switch.get("location", ""), "neighbors": [], "macs": [], "arp": {}, "port_vlan": {},
           "aliases": {}, "problem": "", "notes": [], "lldp_ok": False, "macs_ok": False, "mac_problem": ""}

    name_rows = walk(SYS_NAME)
    if name_rows:
        out["name"] = network.short_name(_text(name_rows[0][1])) or out["name"]
    if_names = {idx[0]: analyzer.short_port(_text(v)) for idx, v in walk(IF_NAME) if len(idx) == 1 and _text(v)}
    if not if_names:
        if_names = {idx[0]: analyzer.short_port(_text(v)) for idx, v in walk(IF_DESCR) if len(idx) == 1 and _text(v)}
    out["ports"] = len(if_names)
    for idx, v in walk(IF_ALIAS):
        if len(idx) == 1 and idx[0] in if_names and _text(v):
            out["aliases"][if_names[idx[0]]] = _text(v)

    # ---- LLDP ----
    local = {}
    for (num,), cols in _table(walk(LLDP_LOC_PORT)).items():
        subtype, pid, desc = cols.get(2), _text(cols.get(3)), _text(cols.get(4))
        if subtype in (5, 7) and pid and not pid.isdigit() and pid.isprintable():
            local[num] = analyzer.short_port(pid)
        elif subtype == 7 and pid.isdigit() and int(pid) in if_names:
            local[num] = if_names[int(pid)]
        elif num in if_names:
            local[num] = if_names[num]
        elif desc:
            local[num] = analyzer.short_port(desc)
    addresses = {}
    for idx, _v in walk(LLDP_REM_ADDR):
        # (time, local port, index, address type, length, address...)
        if len(idx) >= 9 and idx[3] == 1 and idx[4] == 4:
            addresses.setdefault((idx[1], idx[2]), ".".join(str(n) for n in idx[5:9]))
    rem = _table(walk(LLDP_REM))
    for idx, cols in rem.items():
        if len(idx) != 3:
            continue
        _time, num, remidx = idx
        port = local.get(num) or if_names.get(num) or str(num)
        about = _text(cols.get(10)).splitlines()
        out["neighbors"].append({
            "local": port, "source": "LLDP", "name": _text(cols.get(9)),
            "chassis": _lldp_id(cols.get(4), cols.get(5)), "port": _lldp_id(cols.get(6), cols.get(7), port=True),
            "ip": addresses.get((num, remidx), ""), "caps": _bits(cols.get(12), LLDP_CAPS),
            "platform": about[0][:120] if about else "", "model": ""})
    out["lldp_ok"] = bool(local) or bool(rem)

    # ---- CDP (Cisco) ----
    for (ifindex, _dev), cols in _table(walk(CDP_CACHE)).items():
        out["neighbors"].append({
            "local": if_names.get(ifindex, str(ifindex)), "source": "CDP",
            "name": network.short_name(_text(cols.get(6))), "chassis": "", "port": _text(cols.get(7)),
            "ip": _ip_bytes(cols.get(4)), "caps": _cdp_caps(cols.get(9)), "platform": _text(cols.get(8)),
            "model": ""})
    for nb in out["neighbors"]:
        nb["kind"] = lldp.kind_of(nb)
        out["lldp_ok"] = True

    # ---- MAC table ----
    bridge = {idx[0]: v for idx, v in walk(BRIDGE_PORTS) if len(idx) == 1 and isinstance(v, int)}
    out["macs"], how, problem = _read_macs(walk, if_names, bridge)
    out["macs_ok"], out["mac_problem"] = not problem, problem
    if how:
        out["notes"].append(how)
    if problem:
        out["notes"].append(problem)

    # ---- the VLAN on each port and IP addresses this switch knows ----
    for idx, v in walk(CISCO_PORT_VLAN):
        if len(idx) == 1 and idx[0] in if_names and isinstance(v, int):
            out["port_vlan"][if_names[idx[0]]] = v
    if not out["port_vlan"]:
        for idx, v in walk(QBRIDGE_PVID):
            ifindex = bridge.get(idx[0]) if len(idx) == 1 else None
            if ifindex in if_names and isinstance(v, int):
                out["port_vlan"][if_names[ifindex]] = v
    for idx, v in walk(ARP):
        mac = _mac_bytes(v)
        if len(idx) == 5 and mac and mac != "0000.0000.0000":
            out["arp"][mac] = ".".join(str(n) for n in idx[1:5])
    return out


def _read_macs(walk, if_names, bridge):
    """[{mac, port, vlan}] of learned MAC addresses, how they were read, and a problem if they couldn't be."""

    def port_of(bport, bridge_map):
        ifindex = bridge_map.get(bport)
        if ifindex is None and not bridge_map and bport in if_names:
            ifindex = bport
        return if_names.get(ifindex, "")

    # Q-BRIDGE (Dell, Aruba, Nexus, newer Cisco): one table with every VLAN
    q = _table(walk(QBRIDGE_FDB))
    if q:
        fdb_vlan = {}
        for idx, fdb in walk(QBRIDGE_VLAN_FDB):
            if len(idx) == 2 and isinstance(fdb, int):
                fdb_vlan.setdefault(fdb, set()).add(idx[1])
        macs = []
        for idx, cols in q.items():
            if len(idx) != 7:
                continue
            status = cols.get(3, LEARNED)
            port = port_of(cols.get(2), bridge)
            if status != LEARNED or not port:
                continue
            vlans = fdb_vlan.get(idx[0], {idx[0]})
            macs.append({"mac": _mac_index(idx[1:]), "port": port, "vlan": min(vlans) if len(vlans) == 1 else ""})
        return macs, "MAC table: Q-BRIDGE-MIB", ""

    # Cisco: one MAC table per VLAN ('community@vlan', or the context 'vlan-N' on SNMPv3)
    vlans = sorted(idx[1] for idx, state in walk(CISCO_VLANS)
                   if len(idx) == 2 and state == 1 and not 1002 <= idx[1] <= 1005)
    if vlans:
        macs = []
        for vlan in vlans:
            try:
                per_bridge = {idx[0]: v for idx, v in walk(BRIDGE_PORTS, vlan=vlan) if len(idx) == 1}
                rows = _table(walk(BRIDGE_FDB, vlan=vlan))
            except snmpwalk.SnmpError as exc:
                return macs, "MAC table: one per VLAN (Cisco)", (
                    f"Couldn't read the MAC table for VLAN {vlan}: {exc} On SNMPv3 the switch needs "
                    "'context vlan- match prefix' on its snmp-server group.")
            for idx, cols in rows.items():
                port = port_of(cols.get(2), per_bridge)
                if len(idx) == 6 and cols.get(3, LEARNED) == LEARNED and port:
                    macs.append({"mac": _mac_index(idx), "port": port, "vlan": vlan})
        return macs, f"MAC table: one per VLAN (Cisco, {len(vlans)} VLANs)", ""

    # Plain BRIDGE-MIB: one table, no VLANs
    rows = _table(walk(BRIDGE_FDB))
    macs = []
    for idx, cols in rows.items():
        port = port_of(cols.get(2), bridge)
        if len(idx) == 6 and cols.get(3, LEARNED) == LEARNED and port:
            macs.append({"mac": _mac_index(idx), "port": port, "vlan": ""})
    if not rows:
        return [], "", "No MAC table came back (the switch may not share it over SNMP)."
    return macs, "MAC table: BRIDGE-MIB", ""


def snmp_walker(session):
    return lambda oid, vlan=None: session.walk(oid, vlan=vlan)


def scan_switch(switch, creds, settings):
    """Read one real switch. Never raises: a problem goes in 'problem'."""
    try:
        with snmpwalk.Session(switch["address"], creds, timeout=float(settings.get("timeout", 3)),
                              retries=int(settings.get("retries", 1)),
                              port=int(os.environ.get("PA_SNMP_PORT", "161"))) as session:   # tests use another port
            return read_switch(snmp_walker(session), switch)
    except snmpwalk.SnmpError as exc:
        problem = str(exc)
    except Exception as exc:                                      # noqa: BLE001 - one switch never stops the scan
        problem = f"{type(exc).__name__}: {exc}"
    return {"address": switch["address"], "name": switch.get("name") or switch["address"],
            "type": switch.get("type", ""), "building": switch.get("building", ""),
            "location": switch.get("location", ""), "neighbors": [], "macs": [], "arp": {}, "port_vlan": {},
            "aliases": {}, "problem": problem, "notes": [], "lldp_ok": False, "macs_ok": False, "mac_problem": "",
            "ports": 0}


def test_switch(switch, creds, settings):
    """Read one switch and say what came back (for the Test button)."""
    started = time.time()
    r = scan_switch(switch, creds, settings)
    return {"ok": not r["problem"], "problem": r["problem"], "name": r["name"], "ports": r.get("ports", 0),
            "lldp": len([n for n in r["neighbors"] if n["source"] == "LLDP"]),
            "cdp": len([n for n in r["neighbors"] if n["source"] == "CDP"]),
            "macs": len(r["macs"]), "notes": r["notes"], "seconds": round(time.time() - started, 1)}


# ---------------------------------------------------------------------------
# Where is each device: one scan's results -> one entry per device
# ---------------------------------------------------------------------------
def _listed(switches, results):
    addresses = {s["address"] for s in switches}
    names = {(s.get("name") or "").lower() for s in switches} | {r["name"].lower() for r in results}
    return addresses, names - {""}


def devices_from(results, switches, settings):
    """{key: device}. A device's key is its MAC address, or its name when it only sent a name."""
    addresses, names = _listed(switches, results)
    busy = int(settings.get("busy_limit", DEFAULT_SETTINGS["busy_limit"]))
    arp = {}
    for r in results:
        arp.update(r["arp"])
    devices = {}

    def place_of(r, port, vlan, source, macs_here):
        return {"switch": r["address"], "switch_name": r["name"], "building": r["building"], "port": port,
                "vlan": vlan if vlan not in (None, "") else r["port_vlan"].get(port, ""),
                "port_description": r["aliases"].get(port, ""), "found_by": source, "macs_on_port": macs_here}

    def add(key, info, place):
        d = devices.get(key)
        if d is None:
            d = devices[key] = {"key": key, "mac": "", "name": "", "name_from": "", "ip": "", "kind": "unknown",
                                "model": "", "found_by": set(), "places": []}
        if not d["name"] and info.get("name"):
            d["name_from"] = info["found_by"]
        for k in ("mac", "name", "ip", "model"):
            if not d[k] and info.get(k):
                d[k] = info[k]
        if info.get("kind") and (d["kind"] == "unknown" or lldp._RANK.get(info["kind"], 9) < lldp._RANK.get(d["kind"], 9)):
            d["kind"] = info["kind"]
        d["found_by"] |= set(info["found_by"].split(" + "))
        same = next((p for p in d["places"] if p["switch"] == place["switch"] and p["port"] == place["port"]), None)
        if same is None:
            d["places"].append(place)
        else:
            # The VLAN its traffic is in (a phone's voice VLAN) beats the port's own VLAN
            if place["vlan"] not in ("", None) and (not same["vlan"] or place["found_by"] == "MAC table"):
                same["vlan"] = place["vlan"]
            if place["found_by"] not in same["found_by"].split(" + "):
                same["found_by"] = " + ".join(sorted(set(same["found_by"].split(" + ")) | {place["found_by"]}))

    for r in results:
        if r["problem"]:
            continue
        per_port = Counter(m["port"] for m in r["macs"])
        by_port = defaultdict(list)
        for nb in r["neighbors"]:
            by_port[nb["local"]].append(nb)
        passing = set()           # ports that lead to a switch or an access point: MACs there are just passing
        for port, found in by_port.items():
            for nb in lldp.merge(found):
                dev = lldp._device(nb)
                is_listed = dev["ip"] in addresses or network.short_name(nb.get("name") or "").lower() in names
                if nb["kind"] in ("network", "ap") or is_listed:
                    passing.add(port)
                if is_listed:
                    continue      # our own switches: the map shows where they are
                key = dev["mac"] or (f"name:{dev['name'].lower()}" if dev["name"] != "(no name)" else "") \
                    or (f"ip:{dev['ip']}" if dev["ip"] else "")
                if not key:
                    continue
                info = {"mac": dev["mac"], "name": "" if dev["name"] in (dev["mac"], dev["ip"]) else dev["name"],
                        "ip": dev["ip"],
                        "kind": dev["kind"], "model": dev["model"], "found_by": dev["found_by"]}
                add(key, info, place_of(r, port, "", dev["found_by"], per_port.get(port, 0)))
        for m in r["macs"]:
            port = m["port"]
            if port in passing or _SKIP.match(port) or per_port[port] > busy or not m["mac"]:
                continue
            add(m["mac"], {"mac": m["mac"], "found_by": "MAC table"},
                place_of(r, port, m["vlan"], "MAC table", per_port[port]))

    for d in devices.values():
        if not d["ip"] and d["mac"]:
            d["ip"] = arp.get(d["mac"], "")
        # Where it really is: a port where it said who it is beats one where only its MAC was seen, and a
        # port with fewer MACs beats a busier one
        d["places"].sort(key=lambda p: ("MAC table" == p["found_by"], p["macs_on_port"], p["switch_name"].lower(),
                                        analyzer.port_key(p["port"])))
        d["found_by"] = " + ".join(sorted(d["found_by"]))
    return devices


def apply_dhcp(devices, leases):
    """Name the devices only the MAC table found, from their DHCP lease (LLDP/CDP names always win), and fill an
    empty IP from it. Returns how many names it filled."""
    named = 0
    for d in devices.values():
        lease = leases.get(d["mac"]) if d["mac"] else None
        if not lease:
            continue
        if not d["ip"] and lease.get("ip"):
            d["ip"] = lease["ip"]
        if d["found_by"] == "MAC table" and not d["name"] and lease.get("name"):
            d["name"], d["name_from"] = lease["name"], "DHCP"
            named += 1
    return named


# ---------------------------------------------------------------------------
# Comparing with what was known before
# ---------------------------------------------------------------------------
def compare(devices, results, history, settings, now):
    """(changes, new history). The history remembers every device's last place, so a phone unplugged for a
    day and plugged back into the same port isn't a move."""
    missing_after = max(1, int(settings.get("missing_after", DEFAULT_SETTINGS["missing_after"])))
    forget = timedelta(days=int(settings.get("forget_days", DEFAULT_SETTINGS["forget_days"])))
    stamp = now.strftime("%Y-%m-%d %H:%M")
    complete = {r["address"] for r in results if not r["problem"] and r["macs_ok"]}
    read = {r["address"] for r in results if not r["problem"]}
    old = history.get("devices", {})
    baseline = not old
    changes, kept = [], {}

    def where(p):
        return {"switch": p["switch"], "switch_name": p["switch_name"], "building": p.get("building", ""),
                "port": p["port"], "vlan": p.get("vlan", "")}

    for key, d in devices.items():
        here = [where(p) for p in d["places"]]
        before = old.get(key)
        entry = {"mac": d["mac"], "name": d["name"], "name_from": d.get("name_from", ""), "ip": d["ip"],
                 "kind": d["kind"], "model": d["model"],
                 "places": here, "first_seen": before["first_seen"] if before else stamp, "last_seen": stamp,
                 "missed": 0, "missing": False}
        kept[key] = entry
        if before is None:
            if not baseline:
                changes.append(_change("new", d, None, here[0]))
            continue
        was = before.get("places") or []
        then = {(p["switch"], p["port"]) for p in was}
        now_at = {(p["switch"], p["port"]) for p in here}
        note = f"Missing since {before['last_seen']}" if before.get("missing") else ""
        if was and not (then & now_at):
            changes.append(_change("moved", d, was[0], here[0], note))
        elif before.get("missing"):
            changes.append(_change("back", d, was[0] if was else None, here[0], f"Last seen {before['last_seen']}"))
        else:
            same = next((p for p in was if (p["switch"], p["port"]) == (here[0]["switch"], here[0]["port"])), None)
            if same and same.get("vlan") not in ("", None) and here[0]["vlan"] not in ("", None) \
                    and str(same["vlan"]) != str(here[0]["vlan"]):
                changes.append(_change("vlan", d, same, here[0]))

    for key, before in old.items():
        if key in kept:
            continue
        last = _parse(before.get("last_seen"))
        if last and now - last > forget:
            continue                                     # not seen for months: let it go
        entry = dict(before)
        kept[key] = entry
        place = (before.get("places") or [{}])[0]
        found_by_mac_only = not before.get("name") or before.get("name_from") == "DHCP"
        counted = place.get("switch") in (complete if found_by_mac_only else read)
        if not counted:
            continue                                     # its switch wasn't read this time: no news isn't bad news
        entry["missed"] = int(before.get("missed", 0)) + 1
        if entry["missed"] == missing_after and not before.get("missing"):
            entry["missing"] = True
            changes.append(_change("missing", {**before, "key": key}, place, None, f"Last seen {before['last_seen']}"))

    order = list(CHANGES)
    changes.sort(key=lambda c: (order.index(c["change"]), (c["to"] or c["from"] or {}).get("switch_name", "").lower(),
                                analyzer.port_key((c["to"] or c["from"] or {}).get("port", ""))))
    scans = (history.get("scans") or [])[-49:] + [stamp]
    return changes, {"version": 1, "scans": scans, "devices": kept}, baseline


def _change(kind, d, before, after, note=""):
    return {"change": kind, "key": d.get("key", ""), "mac": d.get("mac", ""), "name": d.get("name", ""),
            "ip": d.get("ip", ""), "kind": d.get("kind", "unknown"), "model": d.get("model", ""),
            "from": before, "to": after, "note": note}


def _parse(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
DEVICE_COLUMNS = [("MAC", "mac"), ("IP", "ip"), ("Name", "name"), ("Name from", "name_from"), ("Type", "kind_name"), ("Model", "model"),
                  ("Switch", "switch_name"), ("Switch address", "switch"), ("Building", "building"),
                  ("Port", "port"), ("Port description", "port_description"), ("VLAN", "vlan"),
                  ("Found by", "found_by"), ("Also seen on", "also"), ("First seen", "first_seen")]
CHANGE_COLUMNS = [("Change", "change_name"), ("MAC", "mac"), ("IP", "ip"), ("Name", "name"), ("Type", "kind_name"),
                  ("From switch", "from_switch"), ("From port", "from_port"), ("From VLAN", "from_vlan"),
                  ("To switch", "to_switch"), ("To port", "to_port"), ("To VLAN", "to_vlan"), ("Note", "note")]


def device_rows(devices, history):
    rows = []
    for key, d in devices.items():
        p = d["places"][0]
        also = [f"{q['switch_name']} {q['port']}" for q in d["places"][1:]]
        rows.append({"key": key, "mac": d["mac"], "ip": d["ip"], "name": d["name"], "name_from": d.get("name_from", ""),
                     "kind": d["kind"],
                     "kind_name": KINDS.get(d["kind"], d["kind"]), "model": d["model"],
                     "switch_name": p["switch_name"], "switch": p["switch"], "building": p["building"],
                     "port": p["port"], "port_description": p["port_description"], "vlan": p["vlan"],
                     "found_by": d["found_by"], "also": "; ".join(also),
                     "first_seen": history["devices"].get(key, {}).get("first_seen", "")})
    rows.sort(key=lambda r: (r["building"].lower(), r["switch_name"].lower(), analyzer.port_key(r["port"]),
                             r["mac"]))
    return rows


def change_rows(changes):
    out = []
    for c in changes:
        f, t = c["from"] or {}, c["to"] or {}
        out.append({**{k: c[k] for k in ("change", "mac", "ip", "name", "kind", "model", "note", "key")},
                    "change_name": CHANGES[c["change"]], "kind_name": KINDS.get(c["kind"], c["kind"]),
                    "from_switch": f.get("switch_name", ""), "from_address": f.get("switch", ""),
                    "from_port": f.get("port", ""), "from_vlan": f.get("vlan", ""),
                    "to_switch": t.get("switch_name", ""), "to_address": t.get("switch", ""),
                    "to_port": t.get("port", ""), "to_vlan": t.get("vlan", ""),
                    "building": t.get("building") or f.get("building", "")})
    return out


def csv_text(columns, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c[0] for c in columns])
    for r in rows:
        writer.writerow([portdocs._cell(r.get(key, "")) for _, key in columns])
    return buf.getvalue()


def _write(path, text):
    """Write a whole file at once (a reader never sees half of it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8-sig" if path.suffix == ".csv" else "utf-8", newline="") as f:
        f.write(text)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:               # Windows: someone is reading it this very moment
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2)


def page_data(results, devices, changes, history, baseline, source, now, dhcp=None):
    """(summary for the list of scans, everything the scan's page shows)"""
    rows = device_rows(devices, history)
    crows = change_rows(changes)
    failed = [{"address": r["address"], "name": r["name"], "problem": r["problem"]} for r in results if r["problem"]]
    partial = [{"address": r["address"], "name": r["name"], "problem": r["mac_problem"]}
               for r in results if not r["problem"] and not r["macs_ok"]]
    counts = Counter(c["change"] for c in changes)
    summary = {"created": now.strftime("%Y-%m-%d %H:%M"), "source": source, "switches": len(results),
               "read": len(results) - len(failed), "failed": failed, "partial": partial,
               "devices": len(rows), "counts": {k: counts.get(k, 0) for k in CHANGES}, "baseline": baseline,
               "kinds": dict(Counter(r["kind"] for r in rows)), "dhcp": dhcp}
    page = {**summary, "devices_list": rows, "changes": crows, "change_names": CHANGES, "kind_names": KINDS,
            "switch_notes": [{"address": r["address"], "name": r["name"], "notes": r["notes"]} for r in results]}
    return summary, page


def save_scan(scans_dir, results, devices, changes, history, baseline, source, now, log_lines=(), dhcp=None):
    folder = Path(scans_dir) / now.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    summary, page = page_data(results, devices, changes, history, baseline, source, now, dhcp)
    _write(folder / "devices.csv", csv_text(DEVICE_COLUMNS, page["devices_list"]))
    _write(folder / "changes.csv", csv_text(CHANGE_COLUMNS, page["changes"]))
    _write(folder / "scan.json", json.dumps(page))
    if log_lines:
        _write(folder / "log.txt", "\n".join(f"{l['t']} {l['msg']}" for l in log_lines) + "\n")
    _write(folder / "run.json", json.dumps(summary, indent=2))
    return folder


def prune(scans_dir, keep_days, now):
    """Delete scan folders older than keep_days (the history of where devices were is kept)."""
    cutoff = now - timedelta(days=int(keep_days))
    gone = 0
    for folder in Path(scans_dir).glob("*/run.json") if Path(scans_dir).exists() else []:
        try:
            when = datetime.strptime(folder.parent.name, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(folder.parent, ignore_errors=True)
            gone += 1
    return gone


# ---------------------------------------------------------------------------
# Where things are kept
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "tracker"
        self.scans = self.dir / "scans"
        self.settings_file = self.dir / "settings.json"
        self.history_file = self.dir / "history.json"
        self.lock_file = self.dir / "scan.lock"
        self.progress_file = self.dir / "progress.json"
        self.last_log = self.dir / "last-scan.log"

    def settings(self):
        try:
            saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}

    def save_settings(self, settings):
        self.dir.mkdir(parents=True, exist_ok=True)
        clean = {k: settings[k] for k in DEFAULT_SETTINGS if k in settings}
        _write(self.settings_file, json.dumps(clean, indent=2))

    def history(self):
        try:
            return json.loads(self.history_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "scans": [], "devices": {}}

    def save_history(self, history):
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.history_file, json.dumps(history))

    def switches(self):
        path = self.data_dir / "switches.csv"
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{k: (r.get(k) or "").strip() for k in ("address", "type", "protocol", "building", "location", "name")}
                    for r in csv.DictReader(f) if (r.get("address") or "").strip()]

    # One scan at a time, whether the app or the scheduled task started it
    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - self.lock_file.stat().st_mtime
            except OSError:
                age = 0
            if age < 3 * 3600:
                return False
            self.lock_file.unlink(missing_ok=True)           # left behind by a scan that crashed hours ago
            return self.lock()
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        return True

    def unlock(self):
        self.lock_file.unlink(missing_ok=True)

    def write_progress(self, job, source, started):
        snap = job.snapshot(max(0, len(job.lines) - 300))
        try:
            self._progress(snap, job, source, started)
        except OSError:
            pass                                  # only the page's progress bar; never worth stopping a scan

    def _progress(self, snap, job, source, started):
        _write(self.progress_file, json.dumps({"state": job.state, "source": source, "started": started,
                                               "updated": time.time(), "progress": snap["progress"],
                                               "lines": snap["lines"], "scan": snap.get("report_file")}))

    def progress(self):
        try:
            return json.loads(self.progress_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------------------
# The saved SNMP login: Windows Credential Manager (encrypted, only your Windows account can read it)
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("The library that saves the SNMP login (keyring) isn't installed. Start the app with "
                           "RunAnalyzer.bat; it installs new libraries by itself.") from exc
    return keyring


def saved_secrets():
    """Which secrets are saved (never their values)."""
    kr = _keyring()
    return {name: bool(kr.get_password(KEYRING_SERVICE, name)) for name in SECRETS}


def save_secret(name, value):
    if name in SECRETS and value:
        _keyring().set_password(KEYRING_SERVICE, name, value)


def forget_secrets():
    kr = _keyring()
    for name in SECRETS:
        try:
            kr.delete_password(KEYRING_SERVICE, name)
        except Exception:      # noqa: BLE001 - wasn't saved
            pass


def credentials(settings, typed=None):
    """The SNMP login: typed in the page (for this scan) or the saved one."""
    typed = typed or {}
    creds = {"version": typed.get("version") or settings["version"],
             "username": typed.get("username") or settings["username"],
             "auth_protocol": typed.get("auth_protocol") or settings["auth_protocol"],
             "priv_protocol": typed.get("priv_protocol") or settings["priv_protocol"]}
    for name in SECRETS:
        creds[name] = typed.get(name) or ""
    if not all(creds[n] for n in SECRETS if _needed(creds, n)):
        kr = _keyring()
        for name in SECRETS:
            if not creds[name] and _needed(creds, name):
                creds[name] = kr.get_password(KEYRING_SERVICE, name) or ""
    return creds


def _needed(creds, name):
    if creds["version"] == "2c":
        return name == "community"
    if name == "auth_password":
        return creds["auth_protocol"] != "none"
    if name == "priv_password":
        return creds["priv_protocol"] != "none"
    return False


def missing_login(creds):
    if creds["version"] == "2c":
        return [] if creds["community"] else ["Enter the SNMP community string (or save it first)."]
    out = [] if creds["username"] else ["Enter the SNMPv3 username."]
    if _needed(creds, "auth_password") and not creds["auth_password"]:
        out.append("Enter the SNMPv3 authentication password (or save it first).")
    if _needed(creds, "priv_password") and not creds["priv_password"]:
        out.append("Enter the SNMPv3 privacy (encryption) password (or save it first).")
    return out


# ---------------------------------------------------------------------------
# Names from DHCP: the Windows DHCP server's leases, looked up by MAC (needs the DHCP PowerShell tools, RSAT).
# Runs as whoever runs the scan, with their Windows login: nothing is saved.
# ---------------------------------------------------------------------------
_SERVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_LEASE_STATES = ("Active", "ActiveReservation", "InactiveReservation")
_MAC_ID = re.compile(r"^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$")
DHCP_TIMEOUT = 120


def parse_servers(text):
    """'lgccdc1, lgccdc2' -> (['lgccdc1', 'lgccdc2'], problems). Only plain server names get through."""
    servers, problems = [], []
    for name in re.split(r"[,\s]+", str(text or "").strip()):
        if not name:
            continue
        if not _SERVER.match(name):
            problems.append(f"'{name}' isn't a server name (letters, numbers, dots and dashes only).")
        elif name.lower() not in (s.lower() for s in servers):
            servers.append(name)
    return servers, problems


def _dhcp_script(servers):
    names = ", ".join(f"'{s}'" for s in servers)          # already checked by parse_servers: no quotes possible
    return f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$out = New-Object System.Collections.ArrayList
foreach ($s in @({names})) {{
  try {{
    Get-DhcpServerv4Scope -ComputerName $s | Get-DhcpServerv4Lease -ComputerName $s |
      Where-Object {{ $_.ClientId -match '^([0-9A-Fa-f]{{2}}-){{5}}[0-9A-Fa-f]{{2}}$' }} | ForEach-Object {{
        [void]$out.Add([pscustomobject]@{{ server = $s; scope = [string]$_.ScopeId; ip = [string]$_.IPAddress;
          name = [string]$_.HostName; mac = [string]$_.ClientId; state = [string]$_.AddressState;
          expires = $(if ($_.LeaseExpiryTime) {{ $_.LeaseExpiryTime.ToString('s') }} else {{ '' }}) }})
      }}
  }} catch {{
    [void]$out.Add([pscustomobject]@{{ server = $s; error = $_.Exception.Message;
      id = [string]$_.FullyQualifiedErrorId; category = [string]$_.CategoryInfo.Category }})
  }}
}}
ConvertTo-Json -InputObject @($out) -Compress
"""


def _powershell(args, timeout):
    import subprocess
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no window
    r = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, **flags)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def pick_leases(rows):
    """{mac: lease} from the lookup's rows: active leases and reservations only; a reservation, else the lease
    that runs out last, wins."""
    best = {}
    for r in rows:
        if r.get("error") or r.get("state") not in _LEASE_STATES or not _MAC_ID.match(str(r.get("mac", ""))):
            continue
        mac = portdocs.mac_dotted(r["mac"].replace("-", "").lower())
        lease = {"name": network.short_name(str(r.get("name") or "").strip().rstrip(".")), "ip": str(r.get("ip") or ""),
                 "state": r["state"], "expires": str(r.get("expires") or ""), "server": str(r.get("server") or ""),
                 "scope": str(r.get("scope") or "")}
        rank = ("Reservation" in lease["state"], lease["expires"])
        if mac not in best or rank > best[mac][0]:
            best[mac] = (rank, lease)
    return {mac: lease for mac, (_rank, lease) in best.items()}


def _dhcp_problem(server, error, error_id="", category=""):
    """Why a server's lookup failed, in plain words. The DHCP cmdlets put the reason in the error's id
    ('WIN32 5,...') and category, not in the message."""
    low, eid = error.lower(), (error_id or "").upper()
    if "COMMANDNOTFOUND" in eid or "is not recognized" in low:
        return "The DHCP PowerShell tools (RSAT) aren't installed on this PC, so no names came from DHCP."
    if eid.startswith("WIN32 5,") or category == "PermissionDenied" or "access is denied" in low:
        return (f"Your Windows account can't read DHCP on {server}. Ask for it to be added to the DHCP Users group "
                "(read-only).")
    if eid.startswith(("WIN32 1722,", "WIN32 1753,")) or category == "ConnectionError":
        return f"{server} didn't answer. Is the name right, and is it a DHCP server that's up?"
    return f"{server}: {error.strip()[:300]}"


def _json_rows(out):
    """The JSON list the script prints last; a BOM or a warning line before it doesn't matter."""
    for line in reversed((out or "").splitlines()):
        text = line.strip().lstrip("\ufeff")
        if text.startswith("["):
            try:
                rows = json.loads(text)
            except ValueError:
                return None
            return rows if isinstance(rows, list) else None
    return None


def read_dhcp(servers, run=None, timeout=DHCP_TIMEOUT):
    """({mac: lease}, problem). Every server's active leases and reservations, by MAC. A server that fails is named
    in the problem; the others' leases are still used."""
    if not servers:
        return {}, ""
    if not IS_WINDOWS:
        return {}, "DHCP names are only looked up on Windows."
    run = run or _powershell
    import base64
    script = base64.b64encode(_dhcp_script(servers).encode("utf-16-le")).decode("ascii")     # no quoting trouble
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script]
    try:
        code, out, err = run(args, timeout)
    except Exception as exc:                                     # noqa: BLE001
        import subprocess
        if isinstance(exc, subprocess.TimeoutExpired):
            return {}, f"The DHCP lookup took over {timeout // 60} minutes, so it was stopped."
        return {}, f"Couldn't run PowerShell for the DHCP lookup: {exc}"
    rows = _json_rows(out)
    if code != 0 or not isinstance(rows, list):
        return {}, "The DHCP lookup failed: " + ((err or out).strip()[:300] or f"PowerShell exit code {code}")
    problems = []
    for r in rows:
        if isinstance(r, dict) and r.get("error"):
            text = _dhcp_problem(r.get("server", "?"), str(r["error"]), str(r.get("id") or ""),
                                 str(r.get("category") or ""))
            if text not in problems:
                problems.append(text)
    return pick_leases([r for r in rows if isinstance(r, dict)]), " ".join(problems)


# ---------------------------------------------------------------------------
# A whole scan
# ---------------------------------------------------------------------------
def run_scan(job, store, switches, creds, settings, source="manual", scan_one=None, now=None, dhcp_reader=None):
    """Read every switch, work out where each device is, compare, save. Updates `job` as it goes."""
    scan_one = scan_one or scan_switch
    job.state = "running"
    started = time.time()
    writer = threading.Event()

    def heartbeat():
        while not writer.wait(2):
            store.write_progress(job, source, started)

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        job.progress = {"done": 0, "total": len(switches)}
        job.log(f"Scanning {len(switches)} switches over SNMP (read-only)...")
        results = []
        with ThreadPoolExecutor(max_workers=int(settings.get("parallel", 10))) as pool:
            futures = {}
            for s in switches:
                if job.stop_requested.is_set():
                    break
                futures[pool.submit(_unless_stopped, job, scan_one, s, creds, settings)] = s
            for fut in as_completed(futures):
                r = fut.result()
                job.progress["done"] += 1
                if r is None:
                    continue
                results.append(r)
                where = r["address"] if r["name"] == r["address"] else f"{r['name']} ({r['address']})"
                if r["problem"]:
                    job.log(f"Couldn't read {where}: {r['problem']}", "error")
                else:
                    job.log(f"{where}: {len(r['neighbors'])} LLDP/CDP neighbors, {len(r['macs'])} MAC addresses"
                            + ("" if r["macs_ok"] else f" ({r['mac_problem']})"), "info" if r["macs_ok"] else "warn")
        if job.stop_requested.is_set():
            job.log("Stopped. Nothing was saved, and the history wasn't changed.", "warn")
            job.state = "stopped"
            return None
        if not any(not r["problem"] for r in results):
            job.log("No switch could be read, so nothing was saved and the history wasn't changed. Check the SNMP "
                    "login with Test.", "error")
            job.state = "error"
            return None
        now = now or datetime.now()
        devices = devices_from(results, switches, settings)
        dhcp = _dhcp_names(job, devices, settings, dhcp_reader or read_dhcp)
        history = store.history()
        changes, history, baseline = compare(devices, results, history, settings, now)
        folder = save_scan(store.scans, results, devices, changes, history, baseline, source, now, job.lines, dhcp)
        store.save_history(history)
        pruned = prune(store.scans, settings.get("keep_days", 90), now)
        counts = Counter(c["change"] for c in changes)
        if baseline:
            job.log(f"First scan: {len(devices)} devices written down. From the next scan on, you'll see what changed.",
                    "ok")
        else:
            job.log(f"{len(devices)} devices. Changes: " + (", ".join(
                f"{counts[k]} {CHANGES[k].lower()}" for k in CHANGES if counts.get(k)) or "none") + ".", "ok")
        if pruned:
            job.log(f"Deleted {pruned} scan(s) older than {settings.get('keep_days', 90)} days.")
        job.report_file = folder
        job.state = "done"
        return folder
    except Exception as exc:                                    # noqa: BLE001
        job.log(f"Something went wrong: {type(exc).__name__}: {exc}", "error")
        job.state = "error"
        return None
    finally:
        writer.set()
        store.write_progress(job, source, started)


def _dhcp_names(job, devices, settings, reader):
    """Look up DHCP (when servers are set) and name the MAC-only devices. None when there are no servers."""
    servers, _problems = parse_servers(settings.get("dhcp_servers", ""))
    if not servers:
        return None
    job.log(f"Looking up DHCP leases on {', '.join(servers)}...")
    try:
        leases, problem = reader(servers)
    except Exception as exc:                                     # noqa: BLE001 - names are extra; the scan isn't lost
        leases, problem = {}, f"The DHCP lookup failed: {type(exc).__name__}: {exc}"
    named = apply_dhcp(devices, leases)
    if leases:
        job.log(f"DHCP: {len(leases)} leases from {', '.join(servers)}, {named} devices named.", "ok")
    if problem:
        job.log(f"DHCP: {problem}", "warn")
    return {"servers": servers, "leases": len(leases), "named": named, "problem": problem}


def _unless_stopped(job, scan_one, switch, creds, settings):
    if job.stop_requested.is_set():
        return None
    return scan_one(switch, creds, settings)


# ---------------------------------------------------------------------------
# The daily run: a Windows Task Scheduler task that runs TrackDevices.bat as you, while you're logged on
# (the PC can be locked). The saved SNMP login is in your Windows account, so the task has to run as you.
# ---------------------------------------------------------------------------
TASK_NAME = "Port Analyzer device tracker"
IS_WINDOWS = os.name == "nt"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _schtasks(args):
    import subprocess
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no console window flashing up
    r = subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=30, **flags)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def schedule_status():
    """{'supported', 'on', 'next', 'last', 'result'} from Task Scheduler (Windows only)."""
    if not IS_WINDOWS:
        return {"supported": False, "on": False}
    try:
        code, text = _schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    except (OSError, ValueError) as exc:
        return {"supported": True, "on": False, "problem": str(exc)}
    if code != 0:
        return {"supported": True, "on": False}

    def field(name):
        m = re.search(rf"(?im)^{name}:\s*(.+?)\s*$", text)
        return m.group(1) if m else ""
    return {"supported": True, "on": True, "next": field("Next Run Time"), "last": field("Last Run Time"),
            "result": field("Last Result"), "status": field("Status"),
            "enabled": "disabled" not in field("Scheduled Task State").lower()}


def schedule_on(time_text, days, app_dir=APP_DIR):
    """Make (or remake) the task. Returns '' or what went wrong."""
    if not IS_WINDOWS:
        return "The daily scan can only be set up on Windows."
    if not _TIME.match(time_text or ""):
        return "Pick a time like 10:00."
    bat = Path(app_dir) / "TrackDevices.bat"
    args = ["/Create", "/F", "/TN", TASK_NAME, "/TR", f'"{bat}" --scheduled', "/ST", time_text]
    args += ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI"] if days == "weekdays" else ["/SC", "DAILY"]
    try:
        code, text = _schtasks(args)
    except (OSError, ValueError) as exc:
        return f"Couldn't set up the task: {exc}"
    return "" if code == 0 else f"Task Scheduler said: {text.strip()}"


def schedule_off():
    if not IS_WINDOWS:
        return ""
    try:
        code, text = _schtasks(["/Delete", "/F", "/TN", TASK_NAME])
    except (OSError, ValueError) as exc:
        return f"Couldn't remove the task: {exc}"
    return "" if code == 0 or "cannot find" in text.lower() else f"Task Scheduler said: {text.strip()}"


# ---------------------------------------------------------------------------
# Scheduled run (TrackDevices.bat runs this)
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan every switch on the list over SNMP and save what moved.")
    parser.add_argument("--scheduled", action="store_true", help="started by Windows Task Scheduler")
    args = parser.parse_args(argv)
    store = Store(os.environ.get("PA_DATA_DIR", APP_DIR / "data"))
    store.dir.mkdir(parents=True, exist_ok=True)
    job = analyzer.Job()
    source = "scheduled" if args.scheduled else "manual"

    def finish(code):
        store.last_log.write_text("\n".join(f"{l['t']} [{l['level']}] {l['msg']}" for l in job.lines) + "\n",
                                  encoding="utf-8")
        for l in job.lines[-5:]:
            print(f"{l['t']} {l['msg']}")
        return code

    settings = store.settings()
    switches = store.switches()
    if not switches:
        job.log("The switch list is empty. Add switches on the Port Analyzer page.", "error")
        return finish(1)
    try:
        creds = credentials(settings)
    except Exception as exc:                                     # noqa: BLE001
        job.log(f"Couldn't read the saved SNMP login: {exc}", "error")
        return finish(1)
    problems = missing_login(creds)
    if problems:
        job.log("No SNMP login saved. Open Device tracker in the app and save it. " + " ".join(problems), "error")
        return finish(1)
    if not store.lock():
        job.log("Another scan is already running, so this one didn't start.", "warn")
        return finish(0)
    try:
        folder = run_scan(job, store, switches, creds, settings, source=source)
    finally:
        store.unlock()
    return finish(0 if folder else 1)


if __name__ == "__main__":
    sys.exit(main())
