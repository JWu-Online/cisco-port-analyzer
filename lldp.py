"""
LLDP neighbors: what's plugged into each port, on every switch on the list (or one building, or one switch).

Each switch is logged into once and asked for its ports (status and description) and for what it hears over
LLDP, and CDP on Cisco. A neighbor is anything that told the switch who it is: phones, access points, other
switches and routers, computers, and some printers and cameras. Windows 10 and 11 announce themselves with
their built-in LLDP driver, but only with their computer name (or MAC address) and MAC, nothing else: those
are "Computer". A device that sends nothing shows as "link up, nothing said who it is" (the MAC addresses
on it are in Port documentation).

A run is saved in data/lldp/<date-time>/:
    data.json        what the page shows
    devices.csv      one row per device found, every switch
    every-port.csv   one row per port (and per device, when a port has two), every switch
    switches/*.csv   every port of one switch, one file each
    run.json         a summary for the list of runs

The web page is templates/lldp.html (run it) and templates/lldp_view.html (look through a run).
"""

import csv
import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import analyzer
import network
import portdocs
import switches as sw

COMMANDS = {
    "cisco_ios": {"status": ("show interfaces status",), "descriptions": ("show interfaces description",),
                  "lldp": ("show lldp neighbors detail",), "cdp": ("show cdp neighbors detail",)},
    "cisco_nxos": {"status": ("show interface status",), "descriptions": ("show interface description",),
                   "lldp": ("show lldp neighbors detail",), "cdp": ("show cdp neighbors detail",)},
    "dell_powerconnect": {"status": ("show interfaces status",), "descriptions": ("show interfaces description",),
                          "lldp": ("show lldp neighbors",)},
    "aruba_mas": {"interfaces": ("show interface gigabitethernet", "show interface"), "lldp": ("show lldp neighbor",)},
}
BIG = {"interfaces", "lldp", "cdp"}

KINDS = {"phone": "Phone", "ap": "Access point", "network": "Switch or router", "pc": "Computer",
         "other": "Other device"}
KIND_WORDS = {"phone": ("phone", "phones"), "ap": ("access point", "access points"),
              "network": ("switch or router", "switches or routers"), "pc": ("computer", "computers"),
              "other": ("other device", "other devices")}
_RANK = {"phone": 0, "ap": 1, "network": 2, "pc": 3, "other": 4}
_COMPUTER_OS = re.compile(r"(?i)\bwindows\b|\blinux\b|ubuntu|debian|red ?hat|centos|rocky|fedora|mac ?os|darwin")

# 'show interfaces status' words; switches that only say up/down get the same ones
_WORDS = {"up": "connected", "down": "notconnect", "notconnec": "notconnect", "xcvrabsen": "xcvrAbsent",
          "err-disable": "err-disabled", "errdisabled": "err-disabled", "admin down": "disabled"}

_AP = re.compile(r"(?i)\bAIR-(?!CT)|access point|\bAP[-\s]?\d{3}|\bC9\d{2,3}AX|\bAP Software\b|\bIAP\b|Aruba\s*AP"
                 r"|\bMR\d{2}\b")
_CAP_WORDS = (("WLAN ACCESS POINT", "W"), ("ACCESS POINT", "W"), ("TELEPHONE", "T"), ("BRIDGE", "B"),
              ("ROUTER", "R"), ("STATION ONLY", "S"), ("STATION", "S"), ("REPEATER", "P"), ("DOCSIS", "C"),
              ("OTHER", "O"))
_PHYSICAL_SKIP = re.compile(r"(?i)^(ap|vl|lo|po|tu|nv|mg|bdi|ch)\d")


def status_word(word):
    word = (word or "").strip()
    return _WORDS.get(word.lower(), word)


# ---------------------------------------------------------------------------
# Reading one switch
# ---------------------------------------------------------------------------
def collect(switch, login, timeout=20):
    """Log in once and run the commands. A command the switch doesn't know gives ''."""
    vendor = switch["type"]
    outputs = {}
    with sw.connect(switch["address"], vendor, switch["protocol"], login, timeout) as conn:
        outputs["prompt"] = sw.prompt_name(conn)
        for key, commands in COMMANDS[vendor].items():
            _, outputs[key] = sw.run_first_working(conn, list(commands), read_timeout=300 if key in BIG else 120)
        outputs["location"] = sw.read_location(conn, vendor)          # its SNMP location, for the switch list
    return outputs


def _descriptions(output):
    """'show interfaces description' (Cisco, Dell): {port key: description}, read from the header's column."""
    lines = (output or "").splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"(?i)\bDescription\b", line)
        if not m or not re.match(r"(?i)\s*(Interface|Port)\b", line):
            continue
        found = {}
        for row in lines[i + 1:]:
            if not row.strip() or set(row.strip()) <= set("- "):
                continue
            desc = row[m.start():].strip() if len(row) > m.start() else ""
            found[analyzer.match_key(row.split()[0])] = "" if desc == "--" else desc
        return found
    return {}


def ports_from(outputs, vendor):
    """Every front-panel port: [{port, description, status, vlan}]."""
    ports = []
    if vendor in ("cisco_ios", "cisco_nxos"):
        descs = _descriptions(outputs.get("descriptions"))
        table = analyzer.column_table(outputs.get("status"), ["Port", "Name", "Status", "Vlan", "Duplex"])
        for name, row in table.items():
            if analyzer.SKIP_PORTS.match(name) or _PHYSICAL_SKIP.match(name) or not re.search(r"\d", name):
                continue
            short = analyzer.short_port(name)
            desc = descs.get(analyzer.match_key(short))
            if desc is None:
                desc = "" if row.get("name") == "--" else row.get("name", "")
            ports.append({"port": short, "description": desc, "status": status_word(row.get("status", "").split(" ")[0]),
                          "vlan": row.get("vlan", "")})
    elif vendor == "dell_powerconnect":
        raw = {"show interfaces status": outputs.get("status") or "",
               "show interfaces description": outputs.get("descriptions") or ""}
        for p in analyzer.parse_dell(raw, None):
            ports.append({"port": p["port"], "description": p["description"], "status": status_word(p["status"]),
                          "vlan": ""})
    else:
        for p in analyzer.parse_aruba_interfaces(outputs.get("interfaces"), None):
            ports.append({"port": p["port"], "description": p["description"], "status": status_word(p["status"]),
                          "vlan": ""})
    return ports


def _letters(caps):
    text = (caps or "").upper()
    for word, letter in _CAP_WORDS:
        text = text.replace(word, letter)
    return set(re.findall(r"\b([A-Z])\b", text))


def _blank(value):
    """What switches print for a field the device didn't send ('null' on a Nexus) -> ''."""
    value = (value or "").strip()
    return "" if re.fullmatch(r"(?i)null|-+|not advertised|n/?a|none", value) else value


def kind_of(nb, vendor=""):
    """phone, ap (access point), network (switch or router), pc (computer) or other, from what the device
    said about itself."""
    caps = _blank(nb.get("caps"))
    about = f"{_blank(nb.get('platform'))} {_blank(nb.get('model'))}".strip()
    name = _blank(nb.get("name"))
    if nb.get("source") == "CDP":
        words = caps.lower()
        if "phone" in words or re.search(r"(?i)phone", about):
            return "phone"
        if _AP.search(about):
            return "ap"
        return "network" if re.search(r"\b(router|switch)\b", words) else "other"
    letters = _letters(caps)
    phone, ap = ("P", "A") if vendor == "aruba_mas" else ("T", "W")   # Aruba's table has its own letters
    if phone in letters or re.search(r"(?i)phone", about) or re.match(r"(?i)^SEP[0-9a-f]{6,}", name):
        return "phone"
    if ap in letters or _AP.search(about):
        return "ap"
    if letters & {"B", "R"}:
        return "network"
    if _COMPUTER_OS.search(about):
        return "pc"
    # Windows' own LLDP driver sends an id (its computer name or MAC) and its MAC, and nothing else
    if not (name or about or caps or _blank(nb.get("ip"))) and nb.get("source") == "LLDP":
        return "pc"
    return "other"


_MAC_TEXT = re.compile(r"(?i)^(?:[0-9a-f]{2}([:-]))(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$|^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$"
                       r"|^[0-9a-f]{12}$")


def _mac(text):
    """A chassis or port id that's a MAC address -> 0011.2233.4455; anything else -> ''."""
    text = (text or "").strip()
    return portdocs.mac_dotted(text) if _MAC_TEXT.match(text) else ""


def _sep_mac(name):
    m = re.match(r"(?i)^SEP([0-9a-f]{12})$", name or "")
    return portdocs.mac_dotted(m.group(1)) if m else ""


def _same_device(a, b):
    if _blank(a["name"]) and _blank(a["name"]).lower() == _blank(b["name"]).lower():
        return True
    if a["ip"] and a["ip"] == b["ip"]:
        return True
    macs_a = {_mac(a["chassis"]), _sep_mac(a["name"])} - {""}
    macs_b = {_mac(b["chassis"]), _sep_mac(b["name"])} - {""}
    return bool(macs_a & macs_b)


def merge(found):
    """One entry per device on a port: CDP and LLDP often both report the same phone or access point."""
    out = []
    for nb in found:
        same = next((o for o in out if _same_device(o, nb)), None)
        if same is None:
            out.append({**nb, "sources": {nb["source"]}})
        else:
            _fold(same, nb)
    # One by CDP and one by LLDP, the same kind, and nothing else to match them on: the same device, said two
    # ways. (A phone and the PC behind it are two kinds, so they stay two.)
    if len(out) == 2 and len(out[0]["sources"]) == len(out[1]["sources"]) == 1 \
            and out[0]["sources"] != out[1]["sources"] and out[0]["kind"] == out[1]["kind"] \
            and not (_blank(out[0]["name"]) and _blank(out[1]["name"])):
        _fold(out[0], out.pop())
    return out


def _fold(into, nb):
    for k in ("name", "ip", "platform", "model", "port", "chassis", "caps"):
        if not into.get(k) and nb.get(k):
            into[k] = nb[k]
    if nb.get("source") == "CDP" and nb.get("platform"):      # CDP's platform reads better than a description
        into["platform"] = nb["platform"]
    into["sources"] = into.get("sources", set()) | nb.get("sources", {nb.get("source")})
    if _RANK[nb["kind"]] < _RANK[into["kind"]]:
        into["kind"] = nb["kind"]


def _device(nb):
    """A neighbor as the page and the CSVs show it."""
    name, chassis, port = _blank(nb["name"]), _blank(nb["chassis"]), _blank(nb["port"])
    mac = _mac(chassis) or _sep_mac(name) or _mac(port)
    model = _blank(nb.get("model")) or _blank(nb.get("platform"))
    model = re.sub(r"(?i)^cisco\s+", "", model)
    # No name sent: its IP, or the id it sent when that's a name (Windows sends its computer name), or its MAC
    shown = name or nb["ip"] or (chassis if chassis and not _mac(chassis) else "") or mac or model or "(no name)"
    return {"name": shown, "ip": nb["ip"], "model": model[:80], "their_port": port, "caps": _blank(nb["caps"]),
            "mac": mac, "kind": nb["kind"], "found_by": " + ".join(sorted(nb["sources"]))}


def neighbors_from(outputs, vendor):
    """Everything a switch heard about, by CDP and LLDP: phones, access points, switches, anything."""
    if vendor in ("dell_powerconnect", "aruba_mas"):
        found = network.parse_lldp_table(outputs.get("lldp"), vendor)
    else:
        found = network.parse_cdp(outputs.get("cdp")) + network.parse_lldp_detail(outputs.get("lldp"))
        med = _med_models(outputs.get("lldp"))
        for nb in found:
            if nb["source"] == "LLDP":
                nb["model"] = med.get((nb["local"], nb["chassis"]), "")
    for nb in found:
        nb["kind"] = kind_of(nb, vendor)
    return found


def _med_models(output):
    """IOS LLDP-MED 'Model:' lines (phones say theirs there): {(local port, chassis id): model}."""
    models = {}
    for block in re.split(r"(?m)^\s*-{5,}\s*$", output or ""):
        local = re.search(r"(?m)^\s*Local Intf\s*:\s*(\S+)", block)
        chassis = re.search(r"(?m)^\s*Chassis id\s*:\s*(\S+)", block)
        model = re.search(r"(?m)^\s*Model\s*:\s*(.+?)\s*$", block)
        if local and model:
            models[(local.group(1), chassis.group(1) if chassis else "")] = model.group(1)
    return models


def document(switch, outputs, problem=""):
    """One switch's ports, each with what's plugged into it."""
    vendor = switch["type"]
    ports = ports_from(outputs, vendor) if outputs else []
    by_key = {analyzer.match_key(p["port"]): p for p in ports}
    heard = {}
    for nb in neighbors_from(outputs, vendor) if outputs else []:
        key = analyzer.match_key(nb["local"])
        if key not in by_key:                    # a port the port list didn't show (a mgmt port, say)
            by_key[key] = {"port": analyzer.short_port(nb["local"]), "description": "", "status": "", "vlan": ""}
            ports.append(by_key[key])
        heard.setdefault(key, []).append(nb)
    for p in ports:
        p["neighbors"] = [_device(nb) for nb in merge(heard.get(analyzer.match_key(p["port"]), []))]
    ports.sort(key=lambda p: analyzer.port_key(p["port"]))
    hostname = (outputs or {}).get("prompt", "")
    snmp = (outputs or {}).get("location", "")
    return {
        "name": hostname or switch.get("name") or switch["address"], "address": switch["address"],
        "type": vendor, "building": switch.get("building", ""), "location": switch.get("location") or snmp,
        "snmp_location": snmp, "hostname": hostname, "problem": problem, "ports": ports,
        "counts": counts([p for p in ports]),
    }


def counts(ports):
    c = {k: 0 for k in KINDS}
    for p in ports:
        for d in p["neighbors"]:
            c[d["kind"]] += 1
    return c


def read_switch(switch, login, timeout=20, debug_dir=None):
    """Never raises: a switch that can't be read comes back with its problem."""
    try:
        outputs = collect(switch, login, timeout)
    except Exception as exc:
        return document(switch, None, sw.friendly_error(exc))
    data = document(switch, outputs)
    if not data["ports"]:
        data["problem"] = "Logged in, but no ports could be read. Is the switch type right?"
    elif not any(p["neighbors"] for p in data["ports"]) and not (outputs.get("lldp") or outputs.get("cdp") or "").strip():
        data["problem"] = "LLDP (and CDP) gave nothing. Is LLDP turned on (lldp run)?"
    if data["problem"] and debug_dir:
        save_debug(debug_dir, switch, outputs)
    return data


def save_debug(debug_dir, switch, outputs):
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", switch["address"])
    path = debug_dir / f"{safe}-lldp.txt"
    with open(path, "w", encoding="utf-8") as f:
        for key, text in outputs.items():
            f.write(f"===== {key} =====\n{text}\n\n")
    return path


# ---------------------------------------------------------------------------
# CSVs
# ---------------------------------------------------------------------------
COLUMNS = [
    ("Switch", "switch"), ("Address", "address"), ("Building", "building"), ("Location", "location"),
    ("Port", "port"), ("Port description", "description"), ("Status", "status"), ("VLAN", "vlan"),
    ("Connected device", "device"), ("Device type", "kind"), ("Device IP", "ip"), ("Model / platform", "model"),
    ("Its port", "their_port"), ("Capabilities", "caps"), ("Device MAC", "mac"), ("Found by", "found_by"),
]


def rows(switch_data, every_port=True):
    """CSV rows for one switch: a row per device; ports with none get an empty row when every_port."""
    out = []
    base = {k: switch_data[k] for k in ("address", "building", "location")}
    base["switch"] = switch_data["name"]
    for p in switch_data["ports"]:
        port = {**base, "port": p["port"], "description": p["description"], "status": p["status"], "vlan": p["vlan"]}
        if not p["neighbors"]:
            if every_port:
                out.append(port)
            continue
        for d in p["neighbors"]:
            out.append({**port, "device": d["name"], "kind": KINDS[d["kind"]], "ip": d["ip"], "model": d["model"],
                        "their_port": d["their_port"], "caps": d["caps"], "mac": d["mac"], "found_by": d["found_by"]})
    return out


def csv_text(rows_):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c[0] for c in COLUMNS])
    for r in rows_:
        writer.writerow([portdocs._cell(r.get(key, "")) for _, key in COLUMNS])
    return buf.getvalue()


def write_csv(path, rows_):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:     # the BOM tells Excel it's UTF-8
        f.write(csv_text(rows_))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def page_data(results, scope, created=None):
    switches = sorted(results, key=lambda s: (s["building"], s["location"], analyzer.port_key(s["name"])))
    total = {k: 0 for k in KINDS}
    for s in switches:
        for k, n in s["counts"].items():
            total[k] += n
    return {"created": (created or datetime.now()).strftime("%Y-%m-%d %H:%M"), "scope": scope,
            "kinds": KINDS, "counts": total, "switches": switches,
            "read": len([s for s in switches if s["ports"]]), "failed": len([s for s in switches if not s["ports"]])}


def save_run(out_dir, data):
    """Write the page data, the CSVs and a summary. Returns the run's folder."""
    stamp = datetime.now()
    folder = Path(out_dir) / stamp.strftime("%Y%m%d-%H%M%S")
    (folder / "switches").mkdir(parents=True, exist_ok=True)
    every, devices, used = [], [], set()
    for s in data["switches"]:
        if not s["ports"]:
            continue
        mine = rows(s)
        every += mine
        devices += [r for r in mine if r.get("device")]
        name = portdocs.safe_name(s["name"])
        if name.lower() in used:
            name = portdocs.safe_name(f"{s['name']}-{s['address']}")
        used.add(name.lower())
        write_csv(folder / "switches" / f"{name}.csv", mine)
    write_csv(folder / "devices.csv", devices)
    write_csv(folder / "every-port.csv", every)
    (folder / "data.json").write_text(json.dumps(data), encoding="utf-8")
    summary = {
        "created": data["created"], "scope": data["scope"], "switches": data["read"], "devices": len(devices),
        "counts": data["counts"], "failed": [{"address": s["address"], "name": s["name"], "problem": s["problem"]}
                                             for s in data["switches"] if not s["ports"]],
    }
    (folder / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return folder


def list_runs(out_dir, limit=20):
    return portdocs.list_runs(out_dir, limit)


def run_folder(out_dir, run):
    return portdocs.run_folder(out_dir, run)


def load_data(folder):
    return json.loads((Path(folder) / "data.json").read_text(encoding="utf-8"))


def zip_bytes(folder):
    """Every switch's CSV of a run in one .zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted((Path(folder) / "switches").glob("*.csv")):
            z.write(path, path.name)
    return buf.getvalue()


def run_job(job, switches, logins, settings, out_dir, debug_dir, scope_label, learn=None):
    job.state = "running"
    try:
        job.progress = {"done": 0, "total": len(switches)}
        job.log(f"Reading LLDP neighbors on {scope_label}...")
        results = []
        with ThreadPoolExecutor(max_workers=int(settings.get("parallel", 10))) as pool:
            futures = {}
            for s in switches:
                if job.stop_requested.is_set():
                    break
                futures[pool.submit(_unless_stopped, job, s, logins[s["protocol"]], int(settings.get("timeout", 20)),
                                    debug_dir)] = s
            for fut in as_completed(futures):
                res = fut.result()
                job.progress["done"] += 1
                if res is None:
                    continue
                results.append(res)
                where = res["address"] if res["name"] == res["address"] else f"{res['name']} ({res['address']})"
                if not res["ports"]:
                    job.log(f"Couldn't read {where}: {res['problem']}", "error")
                    continue
                found = sum(res["counts"].values())
                parts = [f"{n} {KIND_WORDS[k][n != 1]}" for k, n in res["counts"].items() if n]
                msg = f"{where}: {len(res['ports'])} ports, {found} device{'' if found == 1 else 's'}" + \
                      (f" ({', '.join(parts)})" if parts else "")
                job.log(msg + (f". {res['problem']}" if res["problem"] else ""), "warn" if res["problem"] else "info")
        if job.stop_requested.is_set():
            job.state = "stopped"
            job.log("Stopped. Nothing was saved.", "warn")
            return
        data = page_data(results, scope_label)
        folder = save_run(out_dir, data)
        job.report_file = folder                  # (the page gets its name: the run's id)
        analyzer.share_learned(job, learn, {s["address"]: {"name": s["hostname"], "location": s["snmp_location"]}
                                            for s in results})
        c = data["counts"]
        job.log(f"Done. {sum(c.values())} devices on {data['read']} switches: {c['phone']} phones, {c['ap']} access "
                f"points, {c['network']} switches or routers, {c['other']} other"
                + (f". {data['failed']} switch(es) couldn't be read." if data["failed"] else "."), "done")
        job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.log(f"The run stopped with an error: {sw.friendly_error(exc)}", "error")


def _unless_stopped(job, switch, login, timeout, debug_dir):
    if job.stop_requested.is_set():
        return None
    return read_switch(switch, login, timeout, debug_dir)


def run_demo():
    """A made-up building, so you can see the page without logging into anything."""
    from lldp_demo import demo_results
    data = page_data([document(s, out, problem) for s, out, problem in demo_results()], "made-up sample building")
    data["demo"] = True
    return data
