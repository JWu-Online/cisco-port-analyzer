"""
network.py - Network map: every switch on the list, placed in Cisco's three-layer model
(core, distribution, access) and joined by the links the switches report.

How it works:
  * Log into each switch once and read who it's plugged into: CDP and LLDP neighbors
    (Cisco speaks both, Dell and Aruba speak LLDP), plus its model, the MAC address table
    and, on Cisco, the ARP table.
  * Join the neighbors up into links. Each link is usually seen from both ends; it's kept once.
  * A switch on the list that no neighbor reported (a Dell next to a Cisco without LLDP
    turned on, say) is looked for by its MAC address in the other switches' MAC tables.
  * Decide each switch's layer from its model (a Catalyst 4500 is distribution, a 2960 is
    access...) and from where it sits (the switches below the core that have switches below
    them are distribution). You can change any switch's layer on the page; that's saved.

The drawing itself is laid out by the page (templates/network.html).
"""

from __future__ import annotations

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
_HINTS = [
    ("core", r"nexus\s*7|n7k|c7\d{3}\b|nexus\s*95|n9k-c95|c9600|c96\d\d|ws-c65|c650\d|c68\d\d"),
    ("distribution", r"ws-c45|c45\d\d|c4507|c4510|c9400|c94\d\d|c9500|c95\d\d|c3850-(?:12|24|48)xs|nexus\s*[35]|n[35]k"),
    ("access", r"ws-c29|c29\d\d|ws-c3[5-8]|c3[5-8]\d\d|c9200|c92\d\d|c9300|c93\d\d|\bie-|c1000|catalyst 1000|"
               r"powerconnect|\b55\d\dp?\b|s3500|s2500|aruba\s*s\d|nexus\s*2|n2k|cbs\d|\bsg\d"),
]


def layer_hint(model, caps=""):
    """('core'|'distribution'|'access'|None, why) from a model name."""
    for layer, pattern in _HINTS:
        if model and re.search(pattern, model, re.I):
            return layer, f"Its model ({model}) is usually {'an' if layer == 'access' else 'a'} {layer} switch"
    if caps and re.search(r"(?i)\brouter\b|\bR\b", caps) and not re.search(r"(?i)\bswitch\b|\bB\b", caps):
        return "core", "A router or firewall: drawn with the core, where the network leaves campus"
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
            info["layer_auto"], info["layer_why"] = "core", "Linked to more switches than any other"
        elif hops.get(n) == 1 and any(hops.get(m) == 2 for m in adjacency[n]):
            info["layer_auto"], info["layer_why"] = "distribution", "Between the core and other switches"
        elif n not in hops:
            info["layer_auto"], info["layer_why"] = "access", "Not linked to the core by anything found"
        else:
            info["layer_auto"], info["layer_why"] = "access", "At the edge: no switches below it"


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


def build_map(results, overrides=None, scope="", now=None):
    """results: [{"switch": row from the list, "outputs": {...} or None, "problem": ""}] -> page data."""
    overrides = overrides or {}
    now = now or datetime.now()
    nodes, by_ip, by_name, by_mac = {}, {}, {}, {}
    for res in results:
        s = res["switch"]
        outputs = res.get("outputs") or {}
        ident = switch_identity(outputs, s["type"]) if outputs else {"hostname": "", "model": "", "macs": set()}
        node_id = s["address"]
        hint, why = layer_hint(ident["model"])
        nodes[node_id] = {
            "id": node_id, "name": ident["hostname"] or s["address"], "address": s["address"], "listed": True,
            "type": s["type"], "type_name": sw.TYPES.get(s["type"], s["type"]), "model": ident["model"],
            "building": s.get("building", ""), "location": s.get("location", ""), "platform": "",
            "read": bool(outputs), "problem": res.get("problem", ""), "hint": hint, "hint_why": why,
            "_macs": ident["macs"],
        }
        by_ip[s["address"]] = node_id
        if ident["hostname"]:
            by_name[ident["hostname"].lower()] = node_id
        for mac in ident["macs"]:
            by_mac[mac] = node_id

    def resolve(nb):
        """The node a neighbor is: a switch on the list, or a device that isn't (added once)."""
        for key, table in ((nb["ip"], by_ip), (nb["name"].lower(), by_name),
                           (portdocs.mac_dotted(nb["chassis"]) if re.search(r"[0-9a-fA-F]{4}", nb["chassis"] or "")
                            and len(re.sub(r"[^0-9a-fA-F]", "", nb["chassis"])) == 12 else "", by_mac)):
            if key and key in table:
                node = nodes[table[key]]
                if not node["read"] and nb["name"] and node["name"] == node["address"]:
                    node["name"] = nb["name"]               # couldn't log in, but a neighbor told us its name
                if not node["model"] and nb["platform"]:
                    node["model"] = re.sub(r"(?i)^cisco\s+", "", nb["platform"])
                    node["hint"], node["hint_why"] = layer_hint(nb["platform"], nb["caps"])
                return table[key]
        label = nb["name"] or nb["ip"] or nb["chassis"]
        node_id = "n:" + label.lower()
        if node_id not in nodes:
            hint, why = layer_hint(nb["platform"], nb["caps"])
            nodes[node_id] = {
                "id": node_id, "name": label, "address": nb["ip"], "listed": False, "type": "", "type_name": "",
                "model": re.sub(r"(?i)^cisco\s+", "", nb["platform"]), "building": "", "location": "",
                "platform": nb["platform"], "read": False,
                "problem": "", "hint": hint, "hint_why": why, "_macs": set(),
            }
            if nb["ip"]:
                by_ip.setdefault(nb["ip"], node_id)
            if nb["name"]:
                by_name.setdefault(nb["name"].lower(), node_id)
        return node_id

    # Links, each kept once however many times (and from which ends) it was reported
    links = {}
    link_ports = {}                                         # node -> ports that lead to another node
    counts = {}
    for res in results:
        outputs = res.get("outputs")
        if not outputs:
            continue
        here = res["switch"]["address"]
        found = neighbors_from(outputs, res["switch"]["type"])
        counts[here] = len(found)
        for nb in found:
            there = resolve(nb)
            if there == here:
                continue
            a, b = sorted([(here, _port(nb["local"])), (there, _port(nb["port"]))])
            pair = (a[0], b[0])
            entry = links.setdefault(pair, {"a": a[0], "b": b[0], "pairs": {}})
            key = (a[1], b[1])
            entry["pairs"].setdefault(key, set()).add(nb["source"])
            link_ports.setdefault(here, set()).add(analyzer.match_key(nb["local"]))
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
        warnings.append(f"{len(alone)} switch(es) aren't linked to anything that was found: "
                        + ", ".join(n["name"] for n in alone[:8]) + (" ..." if len(alone) > 8 else "") + ".")

    out_links = []
    for (a, b), entry in sorted(links.items()):
        pairs = [{"a_port": pa, "b_port": pb, "found_by": " and ".join(sorted(src))}
                 for (pa, pb), src in sorted(entry["pairs"].items())]
        out_links.append({"a": a, "b": b, "pairs": pairs,
                          "mac": all(p["found_by"] == "MAC table" for p in pairs)})
    listed = [n for n in nodes.values() if n["listed"]]
    return {
        "title": "Network map", "scope": scope, "drawn": now.strftime("%Y-%m-%d %H:%M"),
        "nodes": sorted(nodes.values(), key=lambda n: (not n["listed"], n["building"], n["location"],
                                                       n["name"].lower())),
        "links": out_links, "warnings": warnings,
        "summary": {"listed": len(listed), "read": len([n for n in listed if n["read"]]),
                    "other": len(nodes) - len(listed), "links": len(out_links),
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


def run_job(job, switch_list, logins, settings, data_dir, debug_dir, scope_label):
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
        data = build_map(results, load_overrides(data_dir), scope_label)
        path = save_page(data_dir, render_html(data))
        job.report_file = path
        s = data["summary"]
        job.log(f"Done. {s['read']} of {s['listed']} switches read, {s['links']} links, "
                f"{s['layers']['core']} core, {s['layers']['distribution']} distribution, "
                f"{s['layers']['access']} access.", "done")
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
    data = build_map(demo_results(), overrides or {}, "made-up sample campus")
    data["demo"] = True
    html = render_html(data)
    return data, html, save_page(data_dir, html) if save and data_dir else None
