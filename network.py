"""
network.py - Network map: which switches on the list connect to which, drawn in Cisco's
three-layer model (core, distribution, access) with the Nexus core on top.

How it works:
  * Log into each switch once and read who it's plugged into: CDP and LLDP neighbors
    (Cisco speaks both, Dell and Aruba speak LLDP), plus its model, the MAC address table
    and, on Cisco, the ARP table.
  * Join the neighbors up into links. Each link is usually seen from both ends; it's kept once.
    Only switches are drawn: the ones on the list, plus any switch they see whose address is
    in the range you give (10.0.0.0/16 by default), drawn dashed because it wasn't logged
    into. Routers, servers, phones, access points and switches outside the range are left out.
  * A switch on the list that no neighbor reported (a Dell next to a Cisco without LLDP
    turned on, say) is looked for by its MAC address in the other switches' MAC tables.
  * Decide each switch's layer: the Nexus switches are the core (doing distribution too),
    everything plugged into them is access. You can change any switch's layer on the page,
    for example to make a switch distribution; that's saved.

The drawing itself is laid out by the page (templates/network.html).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import analyzer
import portdocs
import switches as sw
import topology

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
LAYERS = ("core", "distribution", "access")
DEFAULT_RANGE = "10.0.0.0/16"                  # where our switches' addresses are

COMMANDS = {
    "cisco_ios": {
        "version": ("show version",), "cdp": ("show cdp neighbors detail",),
        "lldp": ("show lldp neighbors detail",), "arp": ("show ip arp",),
        "macs": ("show mac address-table dynamic", "show mac-address-table dynamic"),
    },
    "cisco_nxos": {
        "version": ("show version",), "cdp": ("show cdp neighbors detail",),
        "lldp": ("show lldp neighbors detail",), "arp": ("show ip arp vrf all", "show ip arp"),
        "macs": ("show mac address-table dynamic",),
    },
    "dell_powerconnect": {
        "version": ("show system", "show version"), "lldp": ("show lldp neighbors",),
        "macs": ("show mac address-table dynamic", "show bridge address-table"),
    },
    "aruba_mas": {
        "version": ("show version",), "lldp": ("show lldp neighbor",), "macs": ("show mac-address-table",),
    },
}
BIG = {"arp", "macs"}                     # these can be long on a core switch


def collect(switch, login, timeout=20):
    """Log into one switch and run its commands. A command it doesn't know gives ''."""
    vendor = switch["type"]
    outputs = {}
    with sw.connect(switch["address"], vendor, switch["protocol"], login, timeout) as conn:
        outputs["prompt"] = sw.prompt_name(conn)
        for key, commands in COMMANDS[vendor].items():
            _, outputs[key] = sw.run_first_working(conn, list(commands), read_timeout=300 if key in BIG else 120)
        outputs["location"] = sw.read_location(conn, vendor)          # its SNMP location, for the switch list
    return outputs


# ---------------------------------------------------------------------------
# Reading neighbors
# ---------------------------------------------------------------------------
_IP = r"(\d{1,3}(?:\.\d{1,3}){3})"


def short_name(device_id):
    """'C4-DIST-1.lgcc.cuny.edu(FOX1234ABCD)' -> 'C4-DIST-1'. An IP address stays as it is."""
    name = re.sub(r"\(.*?\)\s*$", "", (device_id or "").strip()).strip()
    if re.fullmatch(_IP, name):
        return name
    return name.split(".")[0]


def parse_cdp(output):
    """'show cdp neighbors detail' (IOS or NX-OS) -> neighbors."""
    found = []
    for block in re.split(r"(?m)^\s*-{5,}\s*$", output or ""):
        dev = re.search(r"(?m)^\s*Device ID\s*:\s*(.+?)\s*$", block)
        intf = re.search(r"(?m)Interface\s*:\s*([^,\n]+?)\s*,\s*Port ID \(outgoing port\)\s*:\s*(.+?)\s*$", block)
        if not dev or not intf:
            continue
        sysname = re.search(r"(?m)^\s*System Name\s*:\s*(\S+)", block)
        ip = re.search(r"(?i)(?:IP(?:v4)? address|IPv4 Address)\s*:\s*" + _IP, block)
        platform = re.search(r"(?m)Platform\s*:\s*([^,\n]+)", block)
        caps = re.search(r"(?m)Capabilities\s*:\s*(.+?)\s*$", block)
        found.append({
            "local": intf.group(1).strip(), "port": intf.group(2).strip(),
            "name": short_name(sysname.group(1) if sysname else dev.group(1)),
            "ip": ip.group(1) if ip else "", "chassis": "",
            "platform": platform.group(1).strip() if platform else "",
            "caps": caps.group(1).strip() if caps else "", "source": "CDP",
        })
    return found


def parse_lldp_detail(output):
    """'show lldp neighbors detail' (IOS or NX-OS) -> neighbors."""
    text = output or ""
    if re.search(r"(?m)^\s*Local Intf\s*:", text):           # IOS: blocks between dashed lines
        blocks = re.split(r"(?m)^\s*-{5,}\s*$", text)
    else:                                                    # NX-OS: a block starts at each "Chassis id"
        blocks = re.split(r"(?m)^(?=\s*Chassis id\s*:)", text)
    found = []
    for block in blocks:
        local = re.search(r"(?m)^\s*(?:Local Intf|Local Port id)\s*:\s*(\S+)", block)
        if not local:
            continue
        def field(name):
            m = re.search(r"(?m)^\s*" + name + r"\s*:\s*(.*?)\s*$", block)
            return m.group(1) if m else ""
        port, desc = field("Port id"), field("Port Description")
        if not re.search(r"\d", port) or re.fullmatch(r"[0-9a-fA-F.:-]{12,17}", port):
            port = desc or port                       # some devices send their MAC as the port id
        ip = re.search(r"(?m)^\s*(?:IP|Management Address(?:es)?)\s*:\s*" + _IP, block)
        sysdesc = re.search(r"(?ms)^\s*System Description\s*:\s*\n?(.*?)(?:\n\s*\n|\n\s*Time remaining)", block)
        found.append({
            "local": local.group(1), "port": port, "name": short_name(field("System Name")),
            "ip": ip.group(1) if ip else "", "chassis": field("Chassis id"),
            "platform": (sysdesc.group(1).strip().splitlines() or [""])[0][:80] if sysdesc else "",
            "caps": field("Enabled Capabilities") or field("System Capabilities"), "source": "LLDP",
        })
    return found


_CAP_LETTERS = r"[BRWTCDHSOPA]"


def parse_lldp_table(output, vendor):
    """Dell 'show lldp neighbors' or Aruba 'show lldp neighbor' (tables) -> neighbors."""
    found = []
    for line in (output or "").splitlines():
        if vendor == "aruba_mas":
            m = re.match(r"^\s*((?:GE|ge|Pc|pc)\S*\d)\s+(\S+)\s+(" + _CAP_LETTERS + r"(?::" + _CAP_LETTERS + r")*)"
                         r"\s+(\S+)\s+(\d+)\s+(\S+)", line)
            if m:
                found.append({"local": m.group(1), "chassis": m.group(2), "caps": m.group(3).replace(":", ","),
                              "port": m.group(4), "name": short_name(m.group(6)), "ip": "", "platform": "",
                              "source": "LLDP"})
        else:
            m = re.match(r"^\s*((?:gi|te|fa|xg|ch|Gi|Te|Fa)\S*\d)\s+(\S+)\s+(\S+)\s+(?:(.+?)\s+)?"
                         r"(" + _CAP_LETTERS + r"(?:\s*,\s*" + _CAP_LETTERS + r")*)\s+(\d+)\s*$", line)
            if m:
                found.append({"local": m.group(1), "chassis": m.group(2), "port": m.group(3),
                              "name": short_name(m.group(4) or ""), "caps": m.group(5), "ip": "",
                              "platform": "", "source": "LLDP"})
    return found


def is_network_gear(nb):
    """Switches, routers and firewalls only: no phones, access points, cameras or servers."""
    caps, platform = nb["caps"], nb["platform"]
    if re.search(r"(?i)phone|\bAIR-|access point|\bAP\d|C9\d{2}AX|camera|VMware|ESX|Linux|Windows", platform):
        return False
    if nb["source"] == "CDP":
        return bool(re.search(r"(?i)\b(router|switch)\b", caps)) and not re.search(r"(?i)\bphone\b", caps)
    letters = set(re.findall(r"\b([A-Z])\b", caps.upper().replace("BRIDGE", "B").replace("ROUTER", "R")
                             .replace("WLAN", "W").replace("TELEPHONE", "T")))
    return bool(letters & {"B", "R"}) and not letters & {"W", "T", "A", "P"}


def is_switch(nb):
    """A neighbor that says it's a switch (CDP 'Switch', LLDP 'B' for bridge)."""
    return bool(re.search(r"(?i)\bswitch\b|\bB\b", nb["caps"]))


def parse_ranges(text):
    """'10.0.0.0/16, 10.9.x.x' -> [IPv4Network]. Empty -> []. Raises ValueError with a message to show."""
    nets = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        token = part.lower().rstrip(".")
        if "/" not in token:                           # 10.0.x.x, 10.0.*, 10.0 -> the fixed octets
            octets = token.split(".")
            fixed = []
            for o in octets:
                if o in ("x", "*"):
                    break
                fixed.append(o)
            if len(octets) > 4 or any(o not in ("x", "*") for o in octets[len(fixed):]) or not fixed:
                fixed = None
            if fixed:
                token = ".".join(fixed + ["0"] * (4 - len(fixed))) + f"/{8 * len(fixed)}"
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            net = None
        if net is None or net.version != 4:
            raise ValueError(f"'{part}' isn't an address range. Use something like 10.0.0.0/16 or 10.0.x.x.")
        nets.append(net)
    return nets


def in_ranges(ip, nets):
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in nets)


def neighbors_from(outputs, vendor):
    """Every network-gear neighbor a switch reported, by CDP and LLDP."""
    if vendor in ("dell_powerconnect", "aruba_mas"):
        found = parse_lldp_table(outputs.get("lldp"), vendor)
    else:
        found = parse_cdp(outputs.get("cdp")) + parse_lldp_detail(outputs.get("lldp"))
    return [nb for nb in found if is_network_gear(nb)]


# ---------------------------------------------------------------------------
# Who a switch is: name, model, its own MAC addresses
# ---------------------------------------------------------------------------
def switch_identity(outputs, vendor):
    text = outputs.get("version") or ""
    model = ""
    if vendor == "cisco_ios":
        m = (re.search(r"(?im)^\s*Model [Nn]umber\s*:\s*(\S+)", text)
             or re.search(r"(?im)^\s*cisco\s+(\S+)\s+\(.*?\)\s+processor", text))
        model = m.group(1) if m else ""
    elif vendor == "cisco_nxos":
        m = re.search(r"(?im)^\s*cisco\s+(Nexus.*?)\s+(?:\(.*?\)\s+)?Chassis", text)
        model = m.group(1) if m else ""
    elif vendor == "dell_powerconnect":
        model = topology.parse_dell_system(text)["model"]
    else:
        m = re.search(r"(?i)MODEL:\s*([^)\s,]+)", text)
        model = m.group(1) if m else ""
    macs = {portdocs.mac_dotted(m) for m in re.findall(
        r"(?i)(?:Base ethernet MAC Address|System MAC Address|MAC Address)\s*:\s*([0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}|"
        r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})", text)}
    return {"hostname": outputs.get("prompt", ""), "model": model, "macs": macs}


def parse_arp(output):
    """'show ip arp' (IOS or NX-OS) -> {ip: mac}."""
    table = {}
    for line in (output or "").splitlines():
        ip = re.search(r"(?<![\d.])" + _IP + r"(?![\d.])", line)
        mac = portdocs._MAC.search(line)
        if ip and mac:
            table[ip.group(1)] = portdocs.mac_dotted(mac.group(1))
    return table


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------
_CORE = re.compile(r"(?i)nexus|\bn[3579]k|n[3579]k-|c7\d{3}\b|c9600|c96\d\d|ws-c65|c650\d|c68\d\d")


def layer_hint(model, kind=""):
    """('core', why) for a Nexus (or another core chassis), else (None, '')."""
    if kind == "cisco_nxos":
        return "core", "A Nexus: the core, doing distribution too"
    if model and _CORE.search(model):
        return "core", f"Its model ({model}) is a core switch"
    return None, ""


def _farthest(start, adjacency):
    """How many hops to the farthest switch reachable from `start`."""
    seen, queue, far = {start: 0}, deque([start]), 0
    while queue:
        n = queue.popleft()
        for m in adjacency[n]:
            if m not in seen:
                seen[m] = seen[n] + 1
                far = max(far, seen[m])
                queue.append(m)
    return far


def assign_layers(nodes, adjacency):
    """Set each node's automatic layer and the reason, in place."""
    core = {n for n, info in nodes.items() if info["hint"] == "core"}
    if not core:                                   # no core model: the best-connected switch(es)
        candidates = [n for n, info in nodes.items() if info["hint"] != "access" and adjacency[n]] \
            or [n for n in nodes if adjacency[n]]
        if candidates:
            most = max(len(adjacency[n]) for n in candidates)
            core = {n for n in candidates if len(adjacency[n]) == most}
            if len(core) > 1:                      # a tie: keep the one(s) in the middle of the network
                reach = {n: _farthest(n, adjacency) for n in core}
                core = {n for n in core if reach[n] == min(reach.values())}
    hops = {n: 0 for n in core}
    queue = deque(core)
    while queue:
        n = queue.popleft()
        for m in adjacency[n]:
            if m not in hops:
                hops[m] = hops[n] + 1
                queue.append(m)
    for n, info in nodes.items():
        if info["hint"]:
            info["layer_auto"], info["layer_why"] = info["hint"], info["hint_why"]
        elif n in core:
            info["layer_auto"], info["layer_why"] = "core", "No Nexus found, and it's linked to the most switches"
        elif n not in hops:
            info["layer_auto"], info["layer_why"] = "access", "Not linked to the core by anything found"
        elif hops[n] == 1:
            info["layer_auto"], info["layer_why"] = "access", "Plugged straight into the core"
        else:
            info["layer_auto"], info["layer_why"] = "access", f"Plugged into the core through {hops[n] - 1} other switch(es)"


# ---------------------------------------------------------------------------
# Building the map
# ---------------------------------------------------------------------------
def _port(name):
    """Short port names, the same whichever end reported them: Gi1/0/49, Te1/1, Eth1/1, GE0/0/47."""
    name = (name or "").strip()
    m = re.match(r"(?i)^(ethernet|eth)\s*(\d+(?:/\d+)*)$", name)
    if m:
        return "Eth" + m.group(2)
    m = re.match(r"(?i)^ge(\d+/\d+/\d+)$", name)
    if m:
        return "GE" + m.group(1)
    return topology.normalize_intf(name)


def _node(node_id, name, address, model="", building="", location="", type_="", on_list=False, listed=False):
    hint, why = layer_hint(model, type_)
    return {
        "id": node_id, "name": name, "address": address, "listed": listed, "on_list": on_list,
        "type": type_, "os": sw.TYPES.get(type_, type_), "model": model, "building": building,
        "building_guess": False, "location": location, "obs_location": "", "list_type": type_ if on_list else "",
        "list_address": address if on_list else "", "observium_id": None, "kind": "network", "read": False,
        "problem": "", "seen_by": [], "hint": hint, "hint_why": why, "_macs": set(),
    }


def build_map(results, overrides=None, scope="", now=None, also_draw=None, whole_list=None):
    """results: [{"switch": row from the list, "outputs": {...} or None, "problem": ""}] -> page data.

    also_draw: address ranges (from parse_ranges); a switch a neighbor reports in one of them is drawn
    even though it isn't on the list. whole_list: the whole switch list, when only part of it was read
    (one building), so a neighbor that's on the list but wasn't read is still drawn as yours."""
    overrides = overrides or {}
    also_draw = also_draw or []
    now = now or datetime.now()
    nodes, by_ip, by_name, by_mac = {}, {}, {}, {}
    for res in results:
        s = res["switch"]
        outputs = res.get("outputs") or {}
        ident = switch_identity(outputs, s["type"]) if outputs else {"hostname": "", "model": "", "macs": set()}
        node_id = s["address"]
        node = nodes[node_id] = _node(node_id, ident["hostname"] or s.get("name") or s["address"], s["address"],
                                      ident["model"], s.get("building", ""),
                                      s.get("location") or (outputs or {}).get("location", ""), s["type"], True, True)
        node.update(read=bool(outputs), problem=res.get("problem", ""), _macs=ident["macs"])
        by_ip[s["address"]] = node_id
        for name in (ident["hostname"], s.get("name")):     # what it calls itself, and its name on the list
            if name:
                by_name.setdefault(name.lower(), node_id)
        for mac in ident["macs"]:
            by_mac[mac] = node_id

    left_out = {}                                           # neighbors not on the list: name -> platform
    not_read = {s["address"]: s for s in (whole_list or []) if s["address"] not in nodes}
    not_read_names = {s["name"].lower(): s for s in not_read.values() if s.get("name")}
    reported = []                                           # (switch that reported it, neighbor)
    for res in results:
        if res.get("outputs"):
            reported += [(res["switch"]["address"], nb) for nb in neighbors_from(res["outputs"], res["switch"]["type"])]

    def chassis_mac(nb):
        raw = re.sub(r"[^0-9a-fA-F]", "", nb["chassis"] or "")
        return portdocs.mac_dotted(nb["chassis"]) if len(raw) == 12 and re.search(r"[0-9a-fA-F]{4}", nb["chassis"]) else ""

    # Switches the ones on the list see that get drawn too: on the list but not read this time, or in the range
    for _, nb in sorted(reported, key=lambda r: not r[1]["ip"]):         # the ones that give an address first
        if (nb["ip"] and nb["ip"] in by_ip) or (nb["name"] and nb["name"].lower() in by_name) \
                or (chassis_mac(nb) and chassis_mac(nb) in by_mac):
            continue
        model = re.sub(r"(?i)^cisco\s+", "", nb["platform"])
        s = not_read.get(nb["ip"]) or (not_read_names.get(nb["name"].lower()) if nb["name"] else None)
        if s:                                              # on the list, by its address or its name there
            node = _node(s["address"], s.get("name") or nb["name"] or s["address"], s["address"], model,
                         s.get("building", ""), s.get("location", ""), s["type"], True)
        elif nb["ip"] and is_switch(nb) and in_ranges(nb["ip"], also_draw):
            node = _node(nb["ip"], nb["name"] or nb["ip"], nb["ip"], model)
        else:
            continue
        nodes[node["id"]] = node
        for ip in {nb["ip"], node["address"]} - {""}:
            by_ip[ip] = node["id"]
        for name in (nb["name"], s.get("name") if s else ""):
            if name:
                by_name.setdefault(name.lower(), node["id"])
        if chassis_mac(nb):
            by_mac[chassis_mac(nb)] = node["id"]

    def resolve(nb):
        """The switch on the list a neighbor is, or None (it's left out)."""
        for key, table in ((nb["ip"], by_ip), (nb["name"].lower(), by_name), (chassis_mac(nb), by_mac)):
            if key and key in table:
                node = nodes[table[key]]
                if not node["read"] and nb["name"] and node["name"] == node["address"]:
                    node["name"] = nb["name"]               # couldn't log in, but a neighbor told us its name
                if not node["model"] and nb["platform"]:
                    node["model"] = re.sub(r"(?i)^cisco\s+", "", nb["platform"])
                    if not node["hint"]:
                        node["hint"], node["hint_why"] = layer_hint(node["model"], node["type"])
                return table[key]
        label = nb["name"] or nb["ip"] or nb["chassis"]
        if label and is_switch(nb):
            left_out.setdefault(label, re.sub(r"(?i)^cisco\s+", "", nb["platform"]))
        return None

    # Links, each kept once however many times (and from which ends) it was reported
    links = {}
    link_ports = {}                                         # node -> ports that lead to another node
    counts = {r["switch"]["address"]: 0 for r in results if r.get("outputs")}
    for here, nb in reported:
        counts[here] = counts.get(here, 0) + 1
        link_ports.setdefault(here, set()).add(analyzer.match_key(nb["local"]))       # leads somewhere else
        there = resolve(nb)
        if there is None or there == here:
            continue
        if not nodes[there]["listed"] and nodes[here]["name"] not in nodes[there]["seen_by"]:
            nodes[there]["seen_by"].append(nodes[here]["name"])
        a, b = sorted([(here, _port(nb["local"])), (there, _port(nb["port"]))])
        entry = links.setdefault((a[0], b[0]), {"a": a[0], "b": b[0], "pairs": {}})
        entry["pairs"].setdefault((a[1], b[1]), set()).add(nb["source"])
        link_ports.setdefault(there, set()).add(analyzer.match_key(nb["port"]))

    # Switches on the list that nothing reported: look for them in the MAC tables
    arp = {}
    tables = {}
    for res in results:
        outputs = res.get("outputs")
        if outputs:
            arp.update(parse_arp(outputs.get("arp")))
            tables[res["switch"]["address"]] = portdocs.mac_sightings(outputs.get("macs"))
    linked = {n for pair in links for n in pair}
    mac_found = 0
    for node_id, node in nodes.items():
        if not node["listed"] or node_id in linked:
            continue
        wanted = set(node["_macs"])
        if node["address"] in arp:
            wanted.add(arp[node["address"]])
        best = None
        for other, sightings in tables.items():
            if other == node_id:
                continue
            per_port = {}
            for mac, _, port in sightings:
                per_port.setdefault(port, set()).add(mac)
            for port, macs in per_port.items():
                if not macs & wanted or analyzer.match_key(port) in link_ports.get(other, set()):
                    continue
                if best is None or len(macs) < best[0]:     # fewest MACs behind it: the closest port
                    best = (len(macs), other, port)
        if best:
            _, other, port = best
            a, b = sorted([(other, _port(port)), (node_id, "")])
            links[(a[0], b[0])] = {"a": a[0], "b": b[0], "pairs": {(a[1], b[1]): {"MAC table"}}}
            mac_found += 1

    adjacency = {n: set() for n in nodes}
    for a, b in links:
        adjacency[a].add(b)
        adjacency[b].add(a)
    assign_layers(nodes, adjacency)
    for node_id, node in nodes.items():
        if not node["on_list"]:                    # the building of the closets it's plugged into, if they agree
            near = {nodes[m]["building"] for m in adjacency[node_id]
                    if nodes[m]["on_list"] and nodes[m]["building"] and nodes[m]["layer_auto"] != "core"}
            if len(near) == 1:
                node["building"], node["building_guess"] = near.pop(), True
    for node_id, node in nodes.items():
        node["layer"] = overrides.get(node_id) if overrides.get(node_id) in LAYERS else node["layer_auto"]
        node["links"] = len(adjacency[node_id])
        for key in ("hint", "hint_why", "_macs"):
            node.pop(key, None)

    warnings = []
    unread = [n for n in nodes.values() if n["listed"] and not n["read"]]
    if unread:
        warnings.append(f"{len(unread)} switch(es) on your list couldn't be read: "
                        + "; ".join(f"{n['name']} ({n['problem'] or 'no answer'})" for n in unread[:6])
                        + (" ..." if len(unread) > 6 else "") + ". They're drawn where their neighbors say they are.")
    if mac_found:
        warnings.append(f"{mac_found} switch(es) weren't reported by CDP or LLDP, so they were found by their MAC "
                        "address in another switch's MAC table (dashed line). The port on their own end isn't "
                        "known. Dell and Aruba switches only speak LLDP; a Cisco switch needs 'lldp run' to see them.")
    alone = [n for n in nodes.values() if n["listed"] and not adjacency[n["id"]] and n["read"]]
    if alone:
        warnings.append(f"{len(alone)} switch(es) aren't linked to any other switch on your list: "
                        + ", ".join(n["name"] for n in alone[:8]) + (" ..." if len(alone) > 8 else "") + ".")
    extra = [n for n in nodes.values() if not n["on_list"]]
    ranges = ", ".join(str(n) for n in also_draw)
    if extra:
        names = sorted((n["name"] for n in extra), key=str.lower)
        warnings.append(f"Drawn dashed: {len(names)} switch(es) in {ranges} that aren't on your list "
                        f"({', '.join(names[:8])}{' ...' if len(names) > 8 else ''}). They weren't logged into, so "
                        "only their links to your switches are shown; add them to the switch list to see what's "
                        "plugged into them.")
    if left_out:
        names = sorted(left_out, key=str.lower)
        warnings.append(f"Not drawn: {len(names)} neighbor(s) that call themselves switches but aren't on your list"
                        + (f" or in {ranges}" if ranges else "")
                        + f" ({', '.join(names[:8])}{' ...' if len(names) > 8 else ''}). If one of them is yours, "
                        "add it to the switch list and draw the map again.")

    out_links = []
    for (a, b), entry in sorted(links.items()):
        pairs = [{"a_port": pa, "b_port": pb, "found_by": " and ".join(sorted(src))}
                 for (pa, pb), src in sorted(entry["pairs"].items())]
        out_links.append({"a": a, "b": b, "pairs": pairs,
                          "mac": all(p["found_by"] == "MAC table" for p in pairs)})
    listed = [n for n in nodes.values() if n["listed"]]
    return {
        "title": "Network map", "source": scope, "drawn": now.strftime("%Y-%m-%d %H:%M"), "web": "",
        "ranges": ranges,
        "nodes": sorted(nodes.values(), key=lambda n: (n["building"], n["location"], n["name"].lower())),
        "links": out_links, "warnings": warnings,
        "summary": {"listed": len(listed), "read": len([n for n in listed if n["read"]]),
                    "links": len(out_links), "left_out": len(left_out), "extra": len(extra),
                    "not_in_run": len([n for n in nodes.values() if n["on_list"] and not n["listed"]]),
                    "layers": {layer: len([n for n in nodes.values() if n["layer"] == layer]) for layer in LAYERS}},
        "neighbors": counts,
    }


# ---------------------------------------------------------------------------
# Pages and the layer choices people make
# ---------------------------------------------------------------------------
def load_overrides(data_dir):
    path = Path(data_dir) / "layers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if v in LAYERS}
    except (OSError, ValueError):
        return {}


def save_override(data_dir, node_id, layer):
    """layer '' means back to automatic."""
    data = load_overrides(data_dir)
    if layer in LAYERS:
        data[node_id] = layer
    else:
        data.pop(node_id, None)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    (Path(data_dir) / "layers.json").write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def render_html(data):
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
    data_json = json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return env.get_template("network.html").render(data=data, data_json=data_json)


def save_page(data_dir, html, stamp=None):
    pages = Path(data_dir) / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    path = pages / f"network-{(stamp or datetime.now()):%Y%m%d-%H%M%S}.html"
    path.write_text(html, encoding="utf-8")
    return path


def list_pages(data_dir, limit=15):
    pages = Path(data_dir) / "pages"
    files = sorted(pages.glob("network-*.html"), reverse=True) if pages.exists() else []
    out = []
    for p in files[:limit]:
        m = re.match(r"network-(\d{8})-(\d{6})$", p.stem)
        when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None
        out.append({"file": p.name, "created": when.strftime("%Y-%m-%d %H:%M") if when else p.stem})
    return out


def save_debug(debug_dir, address, outputs):
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", address)
    path = debug_dir / f"{safe}-network.txt"
    with open(path, "w", encoding="utf-8") as f:
        for key, text in outputs.items():
            f.write(f"===== {key} =====\n{text}\n\n")
    return path


def run_job(job, switch_list, logins, settings, data_dir, debug_dir, scope_label, also_draw=None, whole_list=None,
            learn=None):
    """Read every switch, a few at a time, then draw and save the map."""
    job.state = "running"
    try:
        job.progress = {"done": 0, "total": len(switch_list)}
        job.log(f"Mapping {scope_label}...")
        results = []
        with ThreadPoolExecutor(max_workers=int(settings.get("parallel", 10))) as pool:
            futures = {pool.submit(_read_one, job, s, logins[s["protocol"]], int(settings.get("timeout", 20))): s
                       for s in switch_list}
            for fut in as_completed(futures):
                res = fut.result()
                job.progress["done"] += 1
                if res is None:
                    continue
                results.append(res)
                s = res["switch"]
                if res["outputs"] is None:
                    job.log(f"Couldn't read {s['address']}: {res['problem']}", "error")
                    continue
                name = res["outputs"].get("prompt") or s["address"]
                where = s["address"] if name == s["address"] else f"{name} ({s['address']})"
                found = neighbors_from(res["outputs"], s["type"])
                job.log(f"{where}: {len(found)} switch/router neighbor(s)"
                        + ("" if found else ", none by CDP or LLDP"), "info" if found else "warn")
                if not found and debug_dir:
                    save_debug(debug_dir, s["address"], res["outputs"])
        if job.stop_requested.is_set():
            job.state = "stopped"
            job.log("Stopped. Nothing was drawn.", "warn")
            return
        results.sort(key=lambda r: analyzer.port_key(r["switch"]["address"]))
        data = build_map(results, load_overrides(data_dir), scope_label, also_draw=also_draw, whole_list=whole_list)
        path = save_page(data_dir, render_html(data))
        job.report_file = path
        analyzer.share_learned(job, learn, {r["switch"]["address"]: {"name": r["outputs"].get("prompt", ""),
                                                                     "location": r["outputs"].get("location", "")}
                                            for r in results if r["outputs"]})
        s = data["summary"]
        job.log(f"Done. {s['read']} of {s['listed']} switches read, {s['links']} links, "
                f"{s['layers']['core']} core, {s['layers']['distribution']} distribution, "
                f"{s['layers']['access']} access"
                + (f", {s['extra']} not on your list drawn dashed" if s["extra"] else "") + ".", "done")
        job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.log(f"The map stopped with an error: {sw.friendly_error(exc)}", "error")


def _read_one(job, switch, login, timeout):
    if job.stop_requested.is_set():
        return None
    try:
        return {"switch": switch, "outputs": collect(switch, login, timeout), "problem": ""}
    except Exception as exc:
        return {"switch": switch, "outputs": None, "problem": sw.friendly_error(exc)}


def run_demo(data_dir=None, save=False, overrides=None):
    """A made-up campus, so you can see the page without any switches."""
    from network_demo import demo_results
    data = build_map(demo_results(), overrides or {}, "made-up sample campus", also_draw=parse_ranges(DEFAULT_RANGE))
    data["demo"] = True
    html = render_html(data)
    return data, html, save_page(data_dir, html) if save and data_dir else None
