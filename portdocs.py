"""
portdocs.py - Port documentation: one CSV row per port, for every switch on the list.

For each switch it reads what the Stack topology page reads (Cisco Catalyst and Dell
PowerConnect: VLANs, tagged VLANs, use, PoE, problems) or what the main check reads
(Cisco Nexus and Aruba), plus the MAC address table, and writes:

    data/docs/<date-time>/all-switches.csv     every port of every switch
    data/docs/<date-time>/<switch>.csv         one file per switch

Each switch is logged into once. Passwords are never written anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import analyzer
import switches as sw
import topology

# ---------------------------------------------------------------------------
# The MAC address table
# ---------------------------------------------------------------------------
MAC_COMMANDS = {
    "cisco_ios": ("show mac address-table dynamic", "show mac-address-table dynamic"),
    "cisco_nxos": ("show mac address-table dynamic",),
    "dell_powerconnect": ("show mac address-table dynamic", "show bridge address-table"),
    "aruba_mas": ("show mac-address-table",),
}
_MAC = re.compile(r"(?<![\w.:-])([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})(?![\w.:-])",
                  re.I)
_PORT = re.compile(r"(?<![\w/])((?:GigabitEthernet|FastEthernet|TenGigabitEthernet|TwoGigabitEthernet|"
                   r"FortyGigabitEthernet|TwentyFiveGigE|HundredGigE|Ethernet|Port-channel|"
                   r"Gi|Fa|Te|Tw|Twe|Fo|Hu|Eth|Po|gi|fa|te|xg|ch|po|GE|ge|Pc|pc)\d+(?:/\d+){0,3})(?![\w/])")


def mac_dotted(mac):
    """Any MAC format -> 0011.2233.4455."""
    digits = re.sub(r"[^0-9a-f]", "", mac.lower())
    return ".".join(digits[i:i + 4] for i in (0, 4, 8))


def mac_sightings(output):
    """'show mac address-table' (Cisco IOS, Nexus, Dell or Aruba) -> [(mac, vlan, port), ...].

    Port-channels are kept; entries on the CPU or a router are left out.
    """
    found = []
    for line in (output or "").splitlines():
        m = _MAC.search(line)
        if not m:
            continue
        rest = line[:m.start()] + " " + line[m.end():]
        port = _PORT.search(rest)
        if not port:
            continue
        vlan = next((t for t in rest[:port.start()].replace("*", " ").split()
                     if t.isdigit() and 1 <= int(t) <= 4094), "")
        found.append((mac_dotted(m.group(1)), vlan, port.group(1)))
    return found


def is_port_channel(port):
    return bool(re.match(r"(?i)po|port-channel|ch\d|pc", port))


def parse_mac_table(output):
    """'show mac address-table' -> {port key: [(mac, vlan), ...]} for front ports.

    The port key is analyzer.match_key, so Gi1/0/5, GigabitEthernet1/0/5 and gi1/0/5 match.
    Entries on the CPU, a router or a port-channel are left out: they aren't front ports.
    """
    found = {}
    for mac, vlan, port in mac_sightings(output):
        if is_port_channel(port):
            continue
        bucket = found.setdefault(analyzer.match_key(port), [])
        if (mac, vlan) not in bucket:
            bucket.append((mac, vlan))
    return found


def mac_columns(vlan, macs):
    """(MAC addresses, MAC count) for one port. Trunks and routed ports just say what they are."""
    if vlan in ("trunk", "routed"):
        return ("Trunk port" if vlan == "trunk" else "Routed port"), ""
    found = sorted({mac for mac, _ in macs})          # the same MAC on two VLANs counts once
    return ", ".join(found), len(found)


# ---------------------------------------------------------------------------
# One switch -> rows
# ---------------------------------------------------------------------------
COLUMNS = [
    ("Switch", "switch"), ("Address", "address"), ("Building", "building"), ("Location", "location"),
    ("SNMP location", "snmp_location"),
    ("Member/slot", "member"), ("Port", "port"), ("Interface", "interface"), ("Description", "description"),
    ("VLAN", "vlan"), ("VLAN name", "vlan_name"), ("Tagged VLANs", "tagged"), ("Status", "status"),
    ("Speed", "speed"), ("Duplex", "duplex"), ("Result", "result"), ("Last active", "last_active"),
    ("Why", "why"), ("Issues", "issues"), ("Power", "power"), ("MAC addresses", "macs"),
    ("MAC count", "mac_count"),
]
# The Status column uses the switch's own words, as 'show interfaces status' prints them: connected, notconnect,
# disabled, err-disabled (and sfpAbsent, inactive, ... as the switch says them). Switches that only say up/down
# (Dell, Aruba) get the same words.
STATUS = {"up": "connected", "down": "notconnect", "admin down": "disabled", "administratively down": "disabled",
          "err-disable": "err-disabled", "errdisabled": "err-disabled", "notconnec": "notconnect",
          "notconnected": "notconnect"}


def status_word(port):
    word = (port.get("state") or port.get("status") or "").strip()
    return STATUS.get(word.lower(), word)
def _link(value):
    """'a-1000' -> '1000', 'a-full' -> 'Full', 'auto' (no link) -> ''."""
    value = re.sub(r"^a-", "", (value or "").strip())
    return "" if value.lower() in ("auto", "--", "n/a") else value.capitalize() if value.isalpha() else value


def _vlan_word(vlan):
    return {"trunk": "Trunk", "routed": "Routed"}.get(vlan, vlan)


def _cisco_tagged(port):
    """A Cisco port's other VLANs, from its config: the voice VLAN, or what a trunk allows."""
    if not port.get("config"):
        return []
    prof = topology.parse_profile(port["config"])
    if port["vlan"] == "trunk":
        return sorted(topology.vlan_set(prof["allowed"]), key=int) if prof["allowed"] else ["all"]
    return [prof["voice_vlan"]] if prof["voice_vlan"] else []


def rows_from_stack(data, switch):
    """Stack topology's page data (Cisco Catalyst or Dell) -> CSV rows, MACs filled in later."""
    rows = []
    names = data.get("vlan_names", {})
    for m in data["members"]:
        for p in m["ports"]:
            if p.get("absent"):
                continue
            tagged = p.get("tagged")
            if tagged is None:
                tagged = _cisco_tagged(p)
            power = [n[len("Powering "):] for n in p.get("notes", []) if n.startswith("Powering ")]
            rows.append({
                "switch": data["hostname"], "address": switch["address"],
                "building": switch.get("building", ""), "location": switch.get("location", ""),
                "snmp_location": switch.get("snmp_location", ""),
                "member": m["number"], "port": p["num"], "interface": p["name"], "description": p["desc"],
                "vlan": _vlan_word(p["vlan"]), "vlan_name": names.get(p["vlan"], ""),
                "tagged": ", ".join(f"{v} {names[v]}" if v in names else v for v in tagged),
                "status": status_word(p),
                "speed": _link(p.get("speed")), "duplex": _link(p.get("duplex")),
                "result": topology.LABEL[p["activity"]], "last_active": p.get("last_active", ""),
                "why": p.get("why", ""), "issues": ", ".join(i["title"] for i in p.get("issues", [])),
                "power": "; ".join(power), "_vlan": p["vlan"], "_key": analyzer.match_key(p["name"]),
            })
    return rows


def _aruba_vlan_names(output):
    """Aruba 'show vlan': VLAN, Description, Ports."""
    names = {}
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d{1,4})\s+(\S+)", line)
        if m and not re.match(r"(?i)(ge|pc)\d|vlan0*" + m.group(1) + "$", m.group(2)):
            names[m.group(1)] = m.group(2)
    return names


def rows_from_check(info, raw, switch, now=None):
    """The main check's ports (Cisco Nexus, Aruba) -> CSV rows, MACs filled in later."""
    names = topology.parse_vlan_names(raw.get("show vlan brief", "")) or _aruba_vlan_names(raw.get("show vlan", ""))
    now = now or datetime.now()
    rows = []
    for p in info["ports"]:
        numbers = re.findall(r"\d+", p["port"])
        if p["status"] == "up":
            last = now.strftime("%Y-%m-%d")                      # same as the Stack topology rows: a date
        elif p.get("last_used_days") is not None:
            last = (now - timedelta(days=p["last_used_days"])).strftime("%Y-%m-%d")
        else:
            last = ""
        rows.append({
            "switch": info["name"], "address": switch["address"], "building": switch.get("building", ""),
            "location": switch.get("location", ""), "snmp_location": switch.get("snmp_location", ""),
            "member": numbers[0] if len(numbers) > 1 else "",
            "port": numbers[-1] if numbers else "", "interface": p["port"], "description": p["description"],
            "vlan": _vlan_word(p["vlan"]), "vlan_name": names.get(p["vlan"], ""), "tagged": "",
            "status": status_word(p), "speed": "", "duplex": "",
            "result": analyzer.RESULT_LABEL[p["result"]], "last_active": last,
            "why": p.get("why", ""), "issues": "", "power": "", "_vlan": p["vlan"],
            "_key": analyzer.match_key(p["port"]),
        })
    return rows


def _with_location(switch, snmp_location):
    """The switch with what it says its location is; its Location on the list stays, or is that when empty."""
    return {**switch, "snmp_location": snmp_location, "location": switch.get("location") or snmp_location}


def document_switch(switch, login, months, timeout=20, history_dir=None, debug_dir=None, now=None):
    """Log into one switch and return {"name", "rows", "problem", "macs"}. Never raises."""
    vendor = switch["type"]
    out = {"name": switch.get("name") or switch["address"], "rows": [], "problem": "", "macs": 0,
           "hostname": "", "snmp_location": ""}
    try:
        if vendor in topology.VENDORS:
            outputs = topology.collect_outputs(switch["address"], switch["protocol"], login, timeout,
                                               lambda msg: None, vendor,
                                               extra={"macs": MAC_COMMANDS[vendor],
                                                      "location": lambda conn: sw.read_location(conn, vendor)})
            out["hostname"], out["snmp_location"] = outputs.get("prompt", ""), outputs.get("location", "")
            switch = _with_location(switch, out["snmp_location"])
            history = topology.load_history(history_dir, switch["address"]) if history_dir else None
            where = ", ".join(x for x in (switch.get("building"), switch.get("location")) if x)
            data, history = topology.build_stack(outputs, switch["address"], months, history, now=now, where=where,
                                                 vendor=vendor)
            if history_dir and data["summary"]["total"]:
                topology.save_history(history_dir, switch["address"], history)
            out["name"] = data["hostname"] or out["name"]
            rows, mac_output = rows_from_stack(data, switch), outputs.get("macs", "")
            if not rows and debug_dir:
                topology.save_debug(debug_dir, switch["address"], vendor, outputs)
        else:
            info = {"name": out["name"], "ports": []}
            with sw.connect(switch["address"], vendor, switch["protocol"], login, timeout) as conn:
                out["hostname"] = sw.prompt_name(conn)
                info["name"] = out["hostname"] or out["name"]
                ports, uptime, raw = analyzer.READERS[vendor](conn)
                if vendor == "cisco_nxos":
                    _, raw["show vlan brief"] = sw.run_first_working(conn, ["show vlan brief"])
                _, mac_output = sw.run_first_working(conn, list(MAC_COMMANDS[vendor]), read_timeout=300)
                out["snmp_location"] = sw.read_location(conn, vendor)
            switch = _with_location(switch, out["snmp_location"])
            threshold = months * analyzer.DAYS_PER_MONTH
            info["ports"] = [analyzer.classify(p, threshold, months)
                             for p in sorted(ports, key=lambda p: analyzer.port_key(p["port"]))]
            out["name"] = info["name"]
            rows = rows_from_check(info, raw, switch, now)
            if not rows and debug_dir:
                analyzer.save_raw(debug_dir, switch, {**raw, "mac table": mac_output})
    except Exception as exc:
        out["problem"] = sw.friendly_error(exc)
        return out
    macs = parse_mac_table(mac_output)
    for r in rows:
        r["macs"], r["mac_count"] = mac_columns(r["_vlan"], macs.get(r["_key"], []))
        out["macs"] += r["mac_count"] or 0
    out["rows"] = rows
    if not rows:
        out["problem"] = "Logged in, but no ports could be read. Is the switch type right?"
    elif not mac_output.strip():
        out["problem"] = "The MAC address table couldn't be read, so that column is empty."
    return out


# ---------------------------------------------------------------------------
# CSV files
# ---------------------------------------------------------------------------
def _cell(value):
    """Text Excel would run as a formula (=, +, -, @ first) gets a ' in front."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def csv_text(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c[0] for c in COLUMNS])
    for r in rows:
        writer.writerow([_cell(r.get(key, "")) for _, key in COLUMNS])
    return buf.getvalue()


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:     # the BOM tells Excel it's UTF-8
        f.write(csv_text(rows))


def safe_name(text):
    return re.sub(r"[^\w.-]", "_", text).strip("_") or "switch"


def save_run(out_dir, results, scope, months):
    """Write the CSVs and a summary for one run. Returns the run's folder."""
    stamp = datetime.now()
    folder = Path(out_dir) / stamp.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    every, used, files = [], set(), []
    for res in results:
        if not res["rows"]:
            continue
        every += res["rows"]
        name = safe_name(res["name"])
        if name.lower() in used:                       # two switches with the same name
            name = safe_name(f"{res['name']}-{res['switch']['address']}")
        used.add(name.lower())
        write_csv(folder / f"{name}.csv", res["rows"])
        files.append(f"{name}.csv")
    write_csv(folder / "all-switches.csv", every)
    summary = {
        "created": stamp.strftime("%Y-%m-%d %H:%M"), "scope": scope, "months": months,
        "switches": len([r for r in results if r["rows"]]), "ports": len(every),
        "failed": [{"address": r["switch"]["address"], "name": r["name"], "problem": r["problem"]}
                   for r in results if not r["rows"]],
        "files": sorted(files, key=str.lower),
    }
    (folder / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return folder


def list_runs(out_dir, limit=20):
    out_dir = Path(out_dir)
    runs = []
    for folder in sorted(out_dir.glob("*/run.json"), reverse=True)[:limit] if out_dir.exists() else []:
        try:
            info = json.loads(folder.read_text(encoding="utf-8"))
        except ValueError:
            continue
        runs.append({"run": folder.parent.name, **info})
    return runs


def run_folder(out_dir, run):
    """The folder of one earlier run, or None. `run` comes from the browser, so it's checked."""
    if not re.fullmatch(r"\d{8}-\d{6}", run or ""):
        return None
    folder = Path(out_dir) / run
    return folder if (folder / "run.json").exists() else None


def zip_bytes(folder):
    """Every per-switch CSV of a run in one .zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.glob("*.csv")):
            if path.name != "all-switches.csv":
                z.write(path, path.name)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A run over many switches, reported through an analyzer.Job
# ---------------------------------------------------------------------------
def run_job(job, switches, logins, months, settings, out_dir, history_dir, debug_dir, scope_label, learn=None):
    job.state = "running"
    try:
        job.progress = {"done": 0, "total": len(switches)}
        job.log(f"Documenting {scope_label}...")
        results = []
        with ThreadPoolExecutor(max_workers=int(settings.get("parallel", 10))) as pool:
            futures = {}
            for s in switches:
                if job.stop_requested.is_set():
                    break
                futures[pool.submit(_unless_stopped, job, s, logins[s["protocol"]], months,
                                    int(settings.get("timeout", 20)), history_dir, debug_dir)] = s
            for fut in as_completed(futures):
                res = fut.result()
                job.progress["done"] += 1
                if res is None:
                    continue
                s = futures[fut]
                res["switch"] = s
                results.append(res)
                where = s["address"] if res["name"] == s["address"] else f"{res['name']} ({s['address']})"
                if not res["rows"]:
                    job.log(f"Couldn't document {where}: {res['problem']}", "error")
                else:
                    msg = f"{where}: {len(res['rows'])} ports, {res['macs']} MAC address{'' if res['macs'] == 1 else 'es'}"
                    job.log(msg + (f". {res['problem']}" if res["problem"] else ""), "warn" if res["problem"] else "info")
        if job.stop_requested.is_set():
            job.state = "stopped"
            job.log("Stopped. No CSVs were saved.", "warn")
            return
        results.sort(key=lambda r: (r["switch"].get("building", ""), r["switch"].get("location", ""),
                                    analyzer.port_key(r["switch"]["address"])))
        folder = save_run(out_dir, results, scope_label, months)
        job.report_file = folder
        analyzer.share_learned(job, learn, {r["switch"]["address"]: {"name": r["hostname"], "location": r["snmp_location"]}
                                            for r in results})
        done = [r for r in results if r["rows"]]
        failed = len(results) - len(done)
        job.log(f"Done. {sum(len(r['rows']) for r in done)} ports on {len(done)} switches"
                + (f". {failed} switch(es) couldn't be read." if failed else "."), "done")
        job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.log(f"The run stopped with an error: {sw.friendly_error(exc)}", "error")


def _unless_stopped(job, switch, login, months, timeout, history_dir, debug_dir):
    if job.stop_requested.is_set():
        return None
    return document_switch(switch, login, months, timeout, history_dir, debug_dir)
