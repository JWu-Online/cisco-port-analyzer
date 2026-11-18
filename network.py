"""
network.py - Network map: every switch Observium monitors, placed in Cisco's three-layer
model (core, distribution, access) and joined by the links Observium knows about.

Where the data comes from: Observium's MySQL database, read-only. Observium Community
Edition has no API, so the app reads three tables directly:
    devices      what Observium monitors: name, model, OS, type, location, up or down
    ports        port names (only the ports that have a neighbour)
    neighbours   the CDP/LLDP links Observium discovered over SNMP
Nothing is ever written to Observium's database. The password is asked for each time
and never saved.

Then:
  * each link is usually recorded from both ends (and by CDP and LLDP); it's kept once
  * neighbours Observium doesn't monitor are kept only if they look like network gear
    (routers, switches); phones, access points and servers are left out
  * each switch's layer comes from its model (a Catalyst 4500 is distribution, a 2960 is
    access...) or from where it sits; anyone can change it on the map, and that's saved
  * switches on the Port Analyzer switch list get their building and location from it

The drawing itself is laid out by the page (templates/network.html).
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import topology

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
LAYERS = ("core", "distribution", "access")

# Observium device types drawn on the map; servers, printers, UPSes and the like aren't
NETWORK_TYPES = {"network", "firewall", "wireless", "loadbalancer", "security", "", "unknown"}


# ---------------------------------------------------------------------------
# Reading Observium's database
# ---------------------------------------------------------------------------
class ObserviumError(Exception):
    """Something to tell the person in plain words."""


def connect_observium(host, port, database, user, password, timeout=10):
    """A read connection to Observium's MySQL database."""
    import pymysql                     # only needed for the Network map
    try:
        return pymysql.connect(host=host, port=int(port or 3306), user=user, password=password,
                               database=database or "observium", connect_timeout=timeout, read_timeout=120,
                               charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    except pymysql.err.MySQLError as exc:
        raise ObserviumError(friendly_db_error(exc, host, database, user, port)) from exc


def friendly_db_error(exc, host="", database="", user="", port=3306):
    args = getattr(exc, "args", ()) or ()
    code = args[0] if args and isinstance(args[0], int) else None
    text = str(args[1]) if len(args) > 1 else str(exc)
    if code == 1045:
        return f"The database refused the username or password for '{user}'."
    if code in (1044, 1049):
        return f"The database '{database}' doesn't exist on {host}, or '{user}' isn't allowed to use it."
    if code == 1142:
        return ("The database user can't read one of the tables it needs (devices, ports, neighbours). "
                "It needs SELECT on all three.")
    if code == 1130:
        return (f"{host} doesn't allow '{user}' to connect from this PC. The user has to be created for "
                "this PC's address (see 'Setting up database access').")
    if code in (2003, 2005, 2013):
        return (f"Couldn't reach the database on {host} port {port or 3306}. MySQL may only be listening on the "
                "server itself, or a firewall is blocking that port (see 'Setting up database access').")
    return text[:300] or exc.__class__.__name__


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _db_errors(func):
    """Turn the database library's errors into plain words."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ObserviumError:
            raise
        except Exception as exc:
            if exc.__class__.__module__.startswith("pymysql"):
                raise ObserviumError(friendly_db_error(exc)) from exc
            raise
    return wrapper


@_db_errors
def read_observium(conn):
    """{"devices": [...], "ports": {port_id: row}, "neighbours": [...]} from Observium's tables."""
    devices = _rows(conn, "SELECT * FROM devices")
    neighbours = _rows(conn, "SELECT * FROM neighbours")
    wanted = sorted({int(n["port_id"]) for n in neighbours if n.get("port_id")} |
                    {int(n["remote_port_id"]) for n in neighbours if n.get("remote_port_id")})
    ports = {}
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        marks = ",".join(["%s"] * len(chunk))
        for row in _rows(conn, f"SELECT port_id, device_id, ifName, ifDescr, ifAlias FROM ports "
                               f"WHERE port_id IN ({marks})", chunk):
            ports[int(row["port_id"])] = row
    return {"devices": devices, "ports": ports, "neighbours": neighbours}


@_db_errors
def count_observium(conn):
    """For 'Test connection': how much is there (and that every table can be read)."""
    devices = _rows(conn, "SELECT COUNT(*) AS n FROM devices")[0]["n"]
    links = _rows(conn, "SELECT COUNT(*) AS n FROM neighbours")[0]["n"]
    _rows(conn, "SELECT port_id, device_id, ifName FROM ports LIMIT 1")
    return {"devices": int(devices), "links": int(links)}


# ---------------------------------------------------------------------------
# Names, gear and layers
# ---------------------------------------------------------------------------
_IP = r"\d{1,3}(?:\.\d{1,3}){3}"


def short_name(device_id):
    """'C4-DIST-1.lgcc.cuny.edu(FOX1234ABCD)' -> 'C4-DIST-1'. An IP address stays as it is."""
    name = re.sub(r"\(.*?\)\s*$", "", (device_id or "").strip()).strip()
    if re.fullmatch(_IP, name):
        return name
    return name.split(".")[0]


_NOT_GEAR = re.compile(r"(?i)phone|\bAIR-|access point|\bAP\d|C9\d{2}AX|camera|VMware|ESXi?\b|Linux|Windows|"
                       r"printer|polycom|yealink|axis|ubuntu|debian|red hat|server")
_GEAR = re.compile(r"(?i)cisco|catalyst|nexus|ws-c|\bc\d{4}|\bn\dk|\bios\b|dell|powerconnect|force10|aruba|procurve|"
                   r"\bhp\b|hpe|juniper|junos|fortigate|fortinet|palo alto|pan-os|\basa\b|\bisr\d|\basr\d|"
                   r"switch|router|firewall|arista|extreme|brocade|ruckus icx|mikrotik")


def looks_like_gear(hostname, platform, version=""):
    """A neighbour Observium doesn't monitor: keep it only if it's a router, switch or firewall."""
    text = f"{platform or ''} {version or ''}"
    if re.fullmatch(r"(?i)SEP[0-9A-F]{12}|[0-9a-f]{2}([ :.-]?[0-9a-f]{2}){5}\s*", hostname or "") or _NOT_GEAR.search(text):
        return False
    return bool(_GEAR.search(text))


_HINTS = [
    ("core", r"nexus\s*7|n7k|c7\d{3}\b|nexus\s*95|n9k-c95|c9600|c96\d\d|ws-c65|c650\d|c68\d\d"),
    ("distribution", r"ws-c45|c45\d\d|c4507|c4510|c9400|c94\d\d|c9500|c95\d\d|c3850-(?:12|24|48)xs|nexus\s*[35]|n[35]k"),
    ("access", r"ws-c29|c29\d\d|ws-c3[5-8]|c3[5-8]\d\d|c9200|c92\d\d|c9300|c93\d\d|\bie-|c1000|catalyst 1000|"
               r"powerconnect|\b55\d\dp?\b|s3500|s2500|aruba\s*s\d|nexus\s*2|n2k|cbs\d|\bsg\d"),
]


def layer_hint(model, kind=""):
    """('core'|'distribution'|'access'|None, why) from a model name, or from being a router/firewall."""
    for layer, pattern in _HINTS:
        if model and re.search(pattern, model, re.I):
            return layer, f"Its model ({model}) is usually {'an' if layer == 'access' else 'a'} {layer} switch"
    if kind in ("firewall", "router"):
        return "core", f"A {kind}: drawn with the core, where the network leaves campus"
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


def _false(value):
    return str(value).strip().lower() in ("0", "false")


# ---------------------------------------------------------------------------
# Building the map
# ---------------------------------------------------------------------------
def build_map(obs, switch_list=(), overrides=None, source="", now=None, web=""):
    """Observium's rows (from read_observium) -> page data."""
    overrides = overrides or {}
    now = now or datetime.now()
    listed = {}
    for s in switch_list:
        listed.setdefault(s["address"].lower(), s)
        listed.setdefault(short_name(s["address"]).lower(), s)

    nodes, by_device, by_name = {}, {}, {}
    left_out = 0
    for d in obs["devices"]:
        if d.get("disabled") and not _false(d.get("disabled")):
            continue
        kind = str(d.get("type") or "").lower()
        if kind not in NETWORK_TYPES:
            left_out += 1
            continue
        hostname = str(d.get("hostname") or "").strip()
        name = short_name(str(d.get("sysName") or "") or hostname) or hostname
        on_list = (listed.get(hostname.lower()) or listed.get(str(d.get("ip") or "").lower())
                   or listed.get(name.lower()))
        model = str(d.get("hardware") or "").strip()
        hint, why = layer_hint(model, kind)
        node_id = hostname.lower()
        down = _false(d.get("status", 1))
        nodes[node_id] = {
            "id": node_id, "name": name, "address": hostname, "listed": True, "observium_id": d.get("device_id"),
            "model": model, "os": str(d.get("os") or ""), "kind": kind,
            "building": on_list.get("building", "") if on_list else "",
            "location": (on_list.get("location", "") if on_list else "") or str(d.get("location") or "").strip(),
            "obs_location": str(d.get("location") or "").strip(),
            "on_list": bool(on_list), "list_address": on_list["address"] if on_list else "",
            "list_type": on_list.get("type", "") if on_list else "",
            "read": not down, "problem": "Observium shows it as down" if down else "",
            "hint": hint, "hint_why": why,
        }
        by_device[d.get("device_id")] = node_id
        by_name.setdefault(name.lower(), node_id)
        by_name.setdefault(hostname.lower(), node_id)
        if d.get("ip"):
            by_name.setdefault(str(d["ip"]).lower(), node_id)

    def unmonitored(n):
        """A neighbour Observium doesn't monitor -> its node, or None if it isn't network gear."""
        label = short_name(str(n.get("remote_hostname") or "")) or str(n.get("remote_address") or "")
        for key in (label.lower(), str(n.get("remote_address") or "").lower()):
            if key and key in by_name:
                return by_name[key]
        if not label or not looks_like_gear(label, n.get("remote_platform"), n.get("remote_version")):
            return None
        node_id = "n:" + label.lower()
        if node_id not in nodes:
            platform = re.sub(r"(?i)^cisco\s+", "", str(n.get("remote_platform") or "")).strip()
            kind = ("router" if re.search(r"(?i)\b(isr|asr)\d|\brouter\b", platform) else
                    "firewall" if re.search(r"(?i)\basa\b|firewall|fortigate|palo alto", platform) else "")
            hint, why = layer_hint(platform, kind)
            nodes[node_id] = {
                "id": node_id, "name": label, "address": str(n.get("remote_address") or ""), "listed": False,
                "observium_id": None, "model": platform, "os": "", "kind": kind, "building": "", "location": "",
                "obs_location": "", "on_list": False, "list_address": "", "list_type": "", "read": True,
                "problem": "", "hint": hint, "hint_why": why,
            }
            by_name[label.lower()] = node_id
        return node_id

    ports = obs["ports"]
    links, dropped = {}, 0
    for n in obs["neighbours"]:
        if "active" in n and _false(n.get("active")):
            continue
        here = by_device.get(n.get("device_id"))
        if not here:
            continue
        local = ports.get(int(n["port_id"])) if n.get("port_id") else None
        local_port = _port(local["ifName"] or local["ifDescr"]) if local else ""
        far = ports.get(int(n["remote_port_id"])) if n.get("remote_port_id") else None
        if far and far["device_id"] in by_device:
            there = by_device[far["device_id"]]
            far_port = _port(far["ifName"] or far["ifDescr"])
        elif far:                                  # a device left out (a server, say)
            dropped += 1
            continue
        else:
            there = unmonitored(n)
            far_port = _port(str(n.get("remote_port") or ""))
            if not there:
                dropped += 1
                continue
        if there == here:
            continue
        (a, pa), (b, pb) = sorted([(here, local_port), (there, far_port)])
        pair = links.setdefault((a, b), {})
        pair.setdefault((pa, pb), set()).add(str(n.get("protocol") or "").upper() or "Observium")

    out_links = []
    for (a, b), pairs in sorted(links.items()):
        # The same link seen from an end that couldn't name the far port: fold it into the full one
        for (pa, pb) in [k for k in pairs if "" in k]:
            full = [k for k in pairs if "" not in k and pa in ("", k[0]) and pb in ("", k[1])]
            if full:
                pairs[full[0]].update(pairs.pop((pa, pb)))
        out_links.append({"a": a, "b": b, "mac": False,
                          "pairs": [{"a_port": pa, "b_port": pb, "found_by": " and ".join(sorted(src))}
                                    for (pa, pb), src in sorted(pairs.items())]})

    adjacency = {node_id: set() for node_id in nodes}
    for link in out_links:
        adjacency[link["a"]].add(link["b"])
        adjacency[link["b"]].add(link["a"])
    # A lone non-switch device with no links (a wireless controller, say) isn't worth drawing
    for node_id in [k for k, v in nodes.items() if not adjacency[k] and v["kind"] not in ("network", "", "unknown")]:
        del nodes[node_id], adjacency[node_id]
    assign_layers(nodes, adjacency)
    for node_id, node in nodes.items():
        node["layer"] = overrides.get(node_id) if overrides.get(node_id) in LAYERS else node["layer_auto"]
        node["links"] = len(adjacency[node_id])
        node.pop("hint", None)
        node.pop("hint_why", None)

    warnings = []
    down = sorted((n for n in nodes.values() if not n["read"]), key=lambda n: n["name"])
    if down:
        warnings.append(f"Observium shows {len(down)} of these as down: "
                        + ", ".join(n["name"] for n in down[:8]) + (" ..." if len(down) > 8 else "") + ".")
    alone = sorted((n for n in nodes.values() if n["listed"] and not adjacency[n["id"]]), key=lambda n: n["name"])
    if alone:
        warnings.append(f"{len(alone)} device(s) have no links in Observium: "
                        + ", ".join(n["name"] for n in alone[:8]) + (" ..." if len(alone) > 8 else "")
                        + ". Observium learns links from CDP/LLDP; a Cisco only sees a Dell or Aruba if it runs LLDP.")
    monitored = [n for n in nodes.values() if n["listed"]]
    return {
        "title": "Network map", "source": source, "drawn": now.strftime("%Y-%m-%d %H:%M"), "web": web,
        "nodes": sorted(nodes.values(), key=lambda n: (not n["listed"], n["building"], n["location"],
                                                       n["name"].lower())),
        "links": out_links, "warnings": warnings,
        "summary": {"listed": len(monitored), "read": len([n for n in monitored if n["read"]]),
                    "other": len(nodes) - len(monitored), "links": len(out_links), "left_out": left_out,
                    "dropped": dropped,
                    "layers": {layer: len([n for n in nodes.values() if n["layer"] == layer]) for layer in LAYERS}},
    }


def draw_from_observium(conn_args, switch_list, data_dir, web="", connect=None):
    """Read Observium, draw the map and save it. Returns (data, saved page path)."""
    conn = (connect or connect_observium)(**conn_args)
    try:
        obs = read_observium(conn)
    finally:
        conn.close()
    data = build_map(obs, switch_list, load_overrides(data_dir), f"Observium on {conn_args['host']}", web=web)
    return data, save_page(data_dir, render_html(data))


# ---------------------------------------------------------------------------
# Pages, saved settings and the layer choices people make
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


CONNECTION_FIELDS = ("host", "port", "database", "user", "web")       # never the password
CONNECTION_DEFAULTS = {"host": "", "port": "3306", "database": "observium", "user": "", "web": ""}


def load_connection(data_dir):
    try:
        saved = json.loads((Path(data_dir) / "observium.json").read_text(encoding="utf-8"))
        return {k: str(saved.get(k) or CONNECTION_DEFAULTS[k]) for k in CONNECTION_FIELDS}
    except (OSError, ValueError):
        return dict(CONNECTION_DEFAULTS)


def save_connection(data_dir, fields):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    clean = {k: str(fields.get(k, "")).strip() for k in CONNECTION_FIELDS}
    (Path(data_dir) / "observium.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")


def render_html(data):
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
    data_json = json.dumps(data, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
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


def run_demo(data_dir=None, save=False, overrides=None):
    """A made-up campus, so you can see the page without Observium."""
    from network_demo import demo_observium, demo_switch_list
    data = build_map(demo_observium(), demo_switch_list(), overrides or {}, "made-up sample campus")
    data["demo"] = True
    html = render_html(data)
    return data, html, save_page(data_dir, html) if save and data_dir else None
