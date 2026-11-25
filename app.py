"""
Port Analyzer - local web app.

Run it with RunAnalyzer.bat (Windows) or start.sh (Mac/Linux), or:  python app.py
It only listens on this computer (127.0.0.1); nobody else on the network can reach it.
Passwords are kept in memory for the run and never written to disk.
"""

import csv
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request

import analyzer
import lldp
import network
import portdocs
import report
import switches as sw
import snmpwalk
import topology
import tracker
import tracker_demo

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PA_DATA_DIR", APP_DIR / "data"))
REPORTS_DIR = DATA_DIR / "reports"
DEBUG_DIR = DATA_DIR / "debug"
SWITCHES_FILE = DATA_DIR / "switches.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"
TOPOLOGY_DIR = DATA_DIR / "topology"
DOCS_DIR = DATA_DIR / "docs"
NETWORK_DIR = DATA_DIR / "network"
LLDP_DIR = DATA_DIR / "lldp"
TRACKER = tracker.Store(DATA_DIR)

DEFAULT_SETTINGS = {"months": 6, "default_protocol": "telnet", "parallel": 10, "timeout": 20}
SWITCH_FIELDS = ["address", "type", "protocol", "building", "location", "name"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
PORT = int(os.environ.get("PA_PORT", "5000"))
job = analyzer.Job()
job_lock = threading.Lock()
docs_job = analyzer.Job()          # Port documentation runs separately from the port check
docs_lock = threading.Lock()
net_job = analyzer.Job()           # and so does the Network map
net_lock = threading.Lock()
lldp_job = analyzer.Job()          # and LLDP neighbors
lldp_lock = threading.Lock()
tracker_job = analyzer.Job()       # and the Device tracker's scan started from the page
tracker_lock = threading.Lock()


@app.before_request
def local_only():
    """Only answer this computer's browser, and only JSON for changes, so other
    websites can't drive the app through your browser."""
    host = (request.host or "").lower()
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        abort(403)
    if request.method == "POST" and not request.is_json:
        abort(415)


# ---------------------------------------------------------------------------
# Saved data (all in the data/ folder, which git ignores)
# ---------------------------------------------------------------------------
def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except ValueError:
            pass
    return settings


def save_settings(settings):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_switches():
    if not SWITCHES_FILE.exists():
        return []
    with open(SWITCHES_FILE, newline="", encoding="utf-8-sig") as f:
        return [{k: (r.get(k) or "") for k in SWITCH_FIELDS} for r in csv.DictReader(f)]


def save_switches(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SWITCHES_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SWITCH_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


switches_lock = threading.Lock()          # a run filling in names and you saving the list don't collide


def learn_from_switches(found):
    """{address: {"name": hostname, "location": SNMP location}} -> fill the switch list's EMPTY Name and Location
    fields with them. Never changes one you typed. Returns (names filled, locations filled)."""
    names = places = 0
    with switches_lock:
        rows = load_switches()
        for r in rows:
            seen = found.get(r["address"]) or {}
            name = " ".join(str(seen.get("name") or "").split())[:80]
            place = " ".join(str(seen.get("location") or "").split())[:120]
            if name and not r["name"] and name != r["address"]:
                r["name"] = name
                names += 1
            if place and not r["location"]:
                r["location"] = place
                places += 1
        if names or places:
            save_switches(rows)
    return names, places


def clean_switch_rows(rows, default_protocol):
    out, problems, seen = [], [], set()
    for n, row in enumerate(rows, 1):
        address = str(row.get("address", "")).strip()
        if not address:
            continue
        vtype = sw.clean_type(row.get("type"))
        protocol = sw.clean_protocol(row.get("protocol") or default_protocol)
        if not vtype:
            problems.append(f"Row {n} ({address}): pick a switch type.")
            continue
        if not protocol:
            problems.append(f"Row {n} ({address}): connection must be SSH or Telnet.")
            continue
        if address.lower() in seen:
            problems.append(f"{address} is listed twice. Remove one of them.")
            continue
        seen.add(address.lower())
        out.append({"address": address, "type": vtype, "protocol": protocol,
                    "building": str(row.get("building", "")).strip(),
                    "location": str(row.get("location", "")).strip(),
                    "name": " ".join(str(row.get("name") or "").split())[:80]})
    return out, problems


def logins_from(payload):
    main = sw.Login(str(payload.get("username", "")).strip(), str(payload.get("password", "")),
                    str(payload.get("secret", "")))
    ssh = main
    if payload.get("ssh_different"):
        ssh = sw.Login(str(payload.get("ssh_username", "")).strip(),
                       str(payload.get("ssh_password", "")), str(payload.get("ssh_secret", "")))
    return {"telnet": main, "ssh": ssh}


def pick_switches(scope, all_switches):
    """'' = every switch, 'b:<building>' = one building, 's:<address>' = one switch."""
    if scope.startswith("b:"):
        b = scope[2:]
        return [s for s in all_switches if s["building"] == b], f"the switches in {b}"
    if scope.startswith("s:"):
        a = scope[2:]
        chosen = [s for s in all_switches if s["address"] == a]
        where = (chosen[0].get("name") or chosen[0]["location"]) if chosen else ""
        return chosen, f"{where} ({a})" if where else a
    return all_switches, f"every switch ({len(all_switches)})"


def report_path(name=None):
    if name:
        path = REPORTS_DIR / name
        return path if path.parent == REPORTS_DIR and path.exists() and path.suffix == ".json" else None
    files = sorted(REPORTS_DIR.glob("report-*.json")) if REPORTS_DIR.exists() else []
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Saved runs: listing and deleting what the tools saved (one at a time, or all of one kind)
# ---------------------------------------------------------------------------
SAVED = {   # kind: where it's saved and what its names look like. Nothing else in those folders is touched.
    "reports": {"dir": REPORTS_DIR, "name": r"report-\d{8}-\d{6}\.json"},
    "topology": {"dir": TOPOLOGY_DIR / "pages", "name": r"[\w.-]+-\d{8}-\d{6}\.html"},
    "docs": {"dir": DOCS_DIR, "name": r"\d{8}-\d{6}", "folder": True},
    "lldp": {"dir": LLDP_DIR, "name": r"\d{8}-\d{6}", "folder": True},
    "network": {"dir": NETWORK_DIR / "pages", "name": r"network-\d{8}-\d{6}\.html"},
    "tracker": {"dir": TRACKER.scans, "name": r"\d{8}-\d{6}", "folder": True},
}


def saved_items(kind):
    """Every saved run of one kind, sorted by name. A run of Port documentation or LLDP neighbors is a folder."""
    spec = SAVED[kind]
    if not spec["dir"].is_dir():
        return []
    found = []
    for path in spec["dir"].iterdir():
        if not re.fullmatch(spec["name"], path.name) or path.is_symlink():
            continue
        if spec.get("folder") and path.is_dir() and (path / "run.json").is_file():
            found.append(path)
        elif not spec.get("folder") and path.is_file():
            found.append(path)
    return sorted(found)


def _delete(path, folder):
    """Delete one saved run. '' when it's gone, or what went wrong (a CSV still open in Excel, say)."""
    try:
        shutil.rmtree(path) if folder else path.unlink()
        return ""
    except OSError as exc:
        return (f"Couldn't delete {path.name}: {exc.strerror or exc}. If it's open in another program "
                "(Excel, a browser), close it and try again.")


def delete_saved(kind, name):
    """(found, problem). `name` comes from the browser, so only a name in the list can be deleted."""
    if kind not in SAVED or not isinstance(name, str):
        return False, ""
    match = [p for p in saved_items(kind) if p.name == name]
    if not match:
        return False, ""
    return True, _delete(match[0], SAVED[kind].get("folder", False))


def report_list(limit=30):
    """The newest saved Port Analyzer reports: file, when, what was checked, months. Reads only their start."""
    out = []
    for path in reversed(saved_items("reports")[-limit:] if limit else saved_items("reports")):
        with open(path, encoding="utf-8") as f:
            head = f.read(2000)
        info = {"file": path.name, "created": "", "scope": "", "months": ""}
        for key in ("created", "scope"):
            m = re.search(r'"' + key + r'":\s*"((?:[^"\\]|\\.)*)"', head)
            if m:
                info[key] = json.loads(f'"{m.group(1)}"')
        m = re.search(r'"months":\s*([\d.]+)', head)
        info["months"] = m.group(1) if m else ""
        if not info["created"]:
            stamp = re.search(r"(\d{8})-(\d{4})", path.name)
            info["created"] = datetime.strptime(stamp.group(1) + stamp.group(2), "%Y%m%d%H%M").strftime(
                "%Y-%m-%d %H:%M") if stamp else path.stem
        out.append(info)
    return out


def parse_months(value):
    months = float(value)
    if not 0.5 <= months <= 120:
        raise ValueError
    return months


# ---------------------------------------------------------------------------
# Pages and API
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def state():
    latest = report_path()
    return jsonify({"settings": load_settings(), "switches": load_switches(),
                    "types": sw.TYPES, "models": sw.MODELS,
                    "latest_report": latest.name if latest else None, "job": job.snapshot(0)})


@app.post("/api/switches")
def set_switches():
    rows, problems = clean_switch_rows(request.json.get("switches", []), load_settings()["default_protocol"])
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    with switches_lock:
        save_switches(rows)
    return jsonify({"ok": True, "switches": rows})


@app.post("/api/settings")
def set_settings():
    incoming = request.json or {}
    settings = load_settings()
    problems = []
    settings["default_protocol"] = sw.clean_protocol(incoming.get("default_protocol")) or "telnet"
    for key, label, low, high in (("parallel", "Switches at once", 1, 50),
                                  ("timeout", "Login timeout", 5, 120)):
        try:
            value = int(incoming.get(key, settings[key]))
            if not low <= value <= high:
                raise ValueError
            settings[key] = value
        except (TypeError, ValueError):
            problems.append(f"{label} must be a number from {low} to {high}.")
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    save_settings(settings)
    return jsonify({"ok": True, "settings": settings})


@app.post("/api/run")
def run():
    global job
    payload = request.json or {}
    with job_lock:
        if job.state == "running":
            return jsonify({"ok": False, "problems": ["A check is already running."]}), 409
        problems = []
        try:
            months = parse_months(payload.get("months", 6))
        except (TypeError, ValueError):
            months = None
            problems.append("Months must be a number from 0.5 to 120.")
        chosen, label = pick_switches(str(payload.get("scope", "")), load_switches())
        if not chosen:
            problems.append("Add your switches first." if not load_switches() else "No switches match that choice.")
        logins = logins_from(payload)
        used = {s["protocol"] for s in chosen}
        if "telnet" in used and not logins["telnet"].password:
            problems.append("Enter the password.")
        if "ssh" in used and not logins["ssh"].password:
            problems.append("Enter the SSH password." if payload.get("ssh_different") else "Enter the password.")
        problems = list(dict.fromkeys(problems))
        if problems:
            return jsonify({"ok": False, "problems": problems}), 400
        settings = load_settings()
        settings["months"] = months
        save_settings(settings)
        job = analyzer.Job()
        threading.Thread(target=analyzer.run_job,
                         args=(job, chosen, logins, months, settings, REPORTS_DIR, DEBUG_DIR, label),
                         kwargs={"learn": learn_from_switches}, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    job.stop_requested.set()
    job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/job")
def job_status():
    return jsonify(job.snapshot(int(request.args.get("since", 0))))


@app.get("/api/reports")
def reports():
    return jsonify({"reports": report_list(), "total": len(saved_items("reports"))})


@app.post("/api/saved/delete")
def saved_delete():
    """Delete one saved run: {"kind": "reports"|"topology"|"docs"|"lldp"|"network", "name": its file or folder}."""
    payload = request.json or {}
    kind = str(payload.get("kind", ""))
    found, problem = delete_saved(kind, payload.get("name"))
    if not found:
        return jsonify({"ok": False, "problems": ["That one is already gone."]}), 404
    if problem:
        return jsonify({"ok": False, "problems": [problem]}), 409
    return jsonify({"ok": True, "left": len(saved_items(kind))})


@app.post("/api/saved/delete-all")
def saved_delete_all():
    """Delete every saved run of one kind. The page asks first; this also wants "confirm": true."""
    payload = request.json or {}
    kind = str(payload.get("kind", ""))
    if kind not in SAVED:
        abort(404)
    if payload.get("confirm") is not True:
        return jsonify({"ok": False, "problems": ["Say you're sure (confirm: true) to delete them all."]}), 400
    deleted, problems = 0, []
    for path in saved_items(kind):
        problem = _delete(path, SAVED[kind].get("folder", False))
        if problem:
            problems.append(problem)
        else:
            deleted += 1
    return jsonify({"ok": not problems, "deleted": deleted, "problems": problems}), (409 if problems else 200)


@app.get("/api/report")
def get_report():
    path = report_path(request.args.get("file"))
    if not path:
        return jsonify({"file": None})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["file"] = path.name
    return jsonify(data)


@app.post("/api/excel")
def excel():
    payload = request.json or {}
    path = report_path(payload.get("file"))
    if not path:
        abort(404)
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["ports"]
    wanted = payload.get("rows")
    if isinstance(wanted, list):
        rows = [rows[i] for i in wanted if isinstance(i, int) and 0 <= i < len(rows)]
    addresses = payload.get("switches")
    summaries = data["switches"]
    if isinstance(addresses, list):
        summaries = [s for s in summaries if s["address"] in set(addresses)]
    body = report.excel_bytes(summaries, rows, data["months"], data.get("scope", ""))
    stamp = datetime.strptime(data["created"], "%Y-%m-%d %H:%M").strftime("%Y-%m-%d")
    name = f"ports-unused-{data['months']:g}mo-{stamp}.xlsx"
    return Response(body, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Filename": name})


@app.post("/api/test-switch")
def test_switch():
    payload = request.json or {}
    rows, problems = clean_switch_rows([payload.get("switch", {})], load_settings()["default_protocol"])
    if problems or not rows:
        return jsonify({"ok": False, "message": (problems or ["Enter an address."])[0]}), 400
    login = logins_from(payload)[rows[0]["protocol"]]
    if not login.password:
        return jsonify({"ok": False, "message": "Enter the login under Log in and run first."}), 400
    started = datetime.now()
    settings = load_settings()
    info = analyzer.check_switch(rows[0], login, float(settings["months"]), settings["timeout"], DEBUG_DIR)
    seconds = round((datetime.now() - started).total_seconds(), 1)
    if info["problem"] and not info["ports"]:
        return jsonify({"ok": False, "message": info["problem"]})
    s = analyzer.summarize(info)
    return jsonify({"ok": True, "name": s["name"], "ports": s["ports"], "inactive": s["inactive"],
                    "unsure": s["unsure"], "uptime_days": s["uptime_days"], "problem": s["problem"],
                    "seconds": seconds, "hostname": info["hostname"], "snmp_location": info["snmp_location"]})


# ---------------------------------------------------------------------------
# Stack topology: one Cisco stack, every member's ports 1-48, VLANs and use
# ---------------------------------------------------------------------------
@app.get("/topology")
def topology_form():
    return render_template("topology_form.html")


@app.get("/api/topology/pages")
def topology_pages():
    return jsonify({"pages": topology.list_pages(TOPOLOGY_DIR), "total": len(saved_items("topology"))})


@app.post("/api/topology")
def topology_run():
    payload = request.json or {}
    address = str(payload.get("address", "")).strip()
    saved = next((s for s in load_switches() if s["address"] == address), None)
    settings = load_settings()
    protocol = sw.clean_protocol(payload.get("protocol") or (saved or {}).get("protocol")
                                 or settings["default_protocol"])
    vendor = (saved or {}).get("type") or sw.clean_type(payload.get("type") or "cisco_ios")
    problems = []
    if not address:
        problems.append("Pick a stack, or type its address.")
    elif vendor not in topology.VENDORS:
        problems.append(f"{address} is a {sw.TYPES.get(vendor, vendor)}. Only Cisco Catalyst and "
                        "Dell PowerConnect stacks can be drawn.")
    if not protocol:
        problems.append("Connection must be SSH or Telnet.")
    try:
        months = parse_months(payload.get("months", settings["months"]))
    except (TypeError, ValueError):
        months = None
        problems.append("Months must be a number from 0.5 to 120.")
    login = logins_from(payload)["telnet"]
    if not login.password:
        problems.append("Enter the password.")
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    where = ", ".join(x for x in ((saved or {}).get("building"), (saved or {}).get("location")) if x)
    try:
        data, _, path = topology.run_topology(address, protocol, login, months, int(settings["timeout"]),
                                              TOPOLOGY_DIR, where=where, progress=lambda msg: None,
                                              vendor=vendor)
    except Exception as exc:
        return jsonify({"ok": False, "problems": [f"Couldn't read {address}: {sw.friendly_error(exc)}"]}), 502
    return jsonify({"ok": True, "url": f"/topology/page/{path.name}", "summary": data["summary"]})


@app.post("/api/topology/cable-test")
def topology_cable_test():
    """Run the cable test (TDR) on one port. The page asks the user to confirm first: the link drops."""
    payload = request.json or {}
    address = str(payload.get("address", "")).strip()
    port = str(payload.get("port", "")).strip()
    saved = next((s for s in load_switches() if s["address"] == address), None)
    settings = load_settings()
    protocol = sw.clean_protocol(payload.get("protocol") or (saved or {}).get("protocol")
                                 or settings["default_protocol"])
    vendor = (saved or {}).get("type") or sw.clean_type(payload.get("vendor") or "cisco_ios")
    problems = []
    if not address or address.startswith("demo-"):
        problems.append("The cable test only works on a real switch, not the made-up sample.")
    elif vendor not in topology.VENDORS:
        problems.append("The cable test works on Cisco Catalyst and Dell PowerConnect switches only.")
    if not protocol:
        problems.append("Connection must be SSH or Telnet.")
    login = logins_from(payload)["telnet"]
    if not login.password:
        problems.append("Enter the password.")
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    try:
        result = topology.run_cable_test(address, vendor, protocol, login, port, int(settings["timeout"]),
                                         slow=bool(payload.get("chassis")))
    except ValueError as exc:
        return jsonify({"ok": False, "problems": [str(exc)]}), 400
    except Exception as exc:
        return jsonify({"ok": False, "problems": [f"Couldn't test {port} on {address}: "
                                                  f"{sw.friendly_error(exc)}"]}), 502
    if not result["ok"]:
        return jsonify({"ok": False, "problems": [result["problem"]], "raw": result.get("raw", "")}), 409
    return jsonify(result)


@app.get("/topology/page/<name>")
def topology_page(name):
    pages = (TOPOLOGY_DIR / "pages").resolve()
    path = (pages / name).resolve()
    if path.parent != pages or path.suffix != ".html" or not path.is_file():
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


@app.get("/topology/demo")
def topology_demo():
    vendor = "dell_powerconnect" if request.args.get("type", "").lower().startswith("dell") else "cisco_ios"
    _, html, _ = topology.run_demo(save=False, vendor=vendor)
    return html


# ---------------------------------------------------------------------------
# Port documentation: CSVs of every port on every switch, with MAC addresses
# ---------------------------------------------------------------------------
@app.get("/port-docs")
def docs_page():
    return render_template("docs.html")


@app.post("/api/docs/run")
def docs_run():
    global docs_job
    payload = request.json or {}
    with docs_lock:
        if docs_job.state == "running":
            return jsonify({"ok": False, "problems": ["CSVs are already being made."]}), 409
        problems = []
        try:
            months = parse_months(payload.get("months", 6))
        except (TypeError, ValueError):
            months = None
            problems.append("Months must be a number from 0.5 to 120.")
        chosen, label = pick_switches(str(payload.get("scope", "")), load_switches())
        if not chosen:
            problems.append("Add your switches on the Port Analyzer page first." if not load_switches()
                            else "No switches match that choice.")
        logins = logins_from(payload)
        used = {s["protocol"] for s in chosen}
        if "telnet" in used and not logins["telnet"].password:
            problems.append("Enter the password.")
        if "ssh" in used and not logins["ssh"].password:
            problems.append("Enter the SSH password." if payload.get("ssh_different") else "Enter the password.")
        problems = list(dict.fromkeys(problems))
        if problems:
            return jsonify({"ok": False, "problems": problems}), 400
        docs_job = analyzer.Job()
        threading.Thread(target=portdocs.run_job,
                         args=(docs_job, chosen, logins, months, load_settings(), DOCS_DIR, TOPOLOGY_DIR,
                               DEBUG_DIR, label),
                         kwargs={"learn": learn_from_switches}, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/docs/stop")
def docs_stop():
    docs_job.stop_requested.set()
    docs_job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/docs/job")
def docs_status():
    return jsonify(docs_job.snapshot(int(request.args.get("since", 0))))


@app.get("/api/docs/runs")
def docs_runs():
    return jsonify({"runs": portdocs.list_runs(DOCS_DIR), "total": len(saved_items("docs"))})


@app.get("/port-docs/download/<run>/<kind>")
def docs_download(run, kind):
    """kind: 'all' (one CSV, every switch) or 'zip' (one CSV per switch)."""
    folder = portdocs.run_folder(DOCS_DIR, run)
    if not folder or kind not in ("all", "zip"):
        abort(404)
    if kind == "all":
        body, mimetype, name = (folder / "all-switches.csv").read_bytes(), "text/csv", f"port-docs-{run}.csv"
    else:
        body, mimetype, name = portdocs.zip_bytes(folder), "application/zip", f"port-docs-{run}-per-switch.zip"
    return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
# LLDP neighbors: what's plugged into each port (phones, access points, switches...), as a page and CSVs
# ---------------------------------------------------------------------------
@app.get("/lldp")
def lldp_page():
    return render_template("lldp.html")


@app.post("/api/lldp/run")
def lldp_run():
    global lldp_job
    payload = request.json or {}
    with lldp_lock:
        if lldp_job.state == "running":
            return jsonify({"ok": False, "problems": ["Neighbors are already being read."]}), 409
        problems = []
        chosen, label = pick_switches(str(payload.get("scope", "")), load_switches())
        if not chosen:
            problems.append("Add your switches on the Port Analyzer page first." if not load_switches()
                            else "No switches match that choice.")
        logins = logins_from(payload)
        used = {s["protocol"] for s in chosen}
        if "telnet" in used and not logins["telnet"].password:
            problems.append("Enter the password.")
        if "ssh" in used and not logins["ssh"].password:
            problems.append("Enter the SSH password." if payload.get("ssh_different") else "Enter the password.")
        problems = list(dict.fromkeys(problems))
        if problems:
            return jsonify({"ok": False, "problems": problems}), 400
        lldp_job = analyzer.Job()
        threading.Thread(target=lldp.run_job,
                         args=(lldp_job, chosen, logins, load_settings(), LLDP_DIR, DEBUG_DIR, label),
                         kwargs={"learn": learn_from_switches}, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/lldp/stop")
def lldp_stop():
    lldp_job.stop_requested.set()
    lldp_job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/lldp/job")
def lldp_job_status():
    return jsonify(lldp_job.snapshot(int(request.args.get("since", 0))))


@app.get("/api/lldp/runs")
def lldp_runs():
    return jsonify({"runs": lldp.list_runs(LLDP_DIR), "total": len(saved_items("lldp"))})


@app.get("/lldp/view/<run>")
def lldp_view(run):
    folder = lldp.run_folder(LLDP_DIR, run)
    if not folder:
        abort(404)
    return render_template("lldp_view.html", data=lldp.load_data(folder), run=run)


@app.get("/lldp/demo")
def lldp_demo():
    return render_template("lldp_view.html", data=lldp.run_demo(), run="")


@app.get("/lldp/download/<run>/<kind>")
def lldp_download(run, kind):
    """kind: 'devices' (a row per device), 'ports' (a row per port) or 'zip' (one CSV per switch)."""
    folder = lldp.run_folder(LLDP_DIR, run)
    if not folder or kind not in ("devices", "ports", "zip"):
        abort(404)
    if kind == "devices":
        body, mimetype, name = (folder / "devices.csv").read_bytes(), "text/csv", f"lldp-devices-{run}.csv"
    elif kind == "ports":
        body, mimetype, name = (folder / "every-port.csv").read_bytes(), "text/csv", f"lldp-every-port-{run}.csv"
    else:
        body, mimetype, name = lldp.zip_bytes(folder), "application/zip", f"lldp-{run}-per-switch.zip"
    return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
# Network map: every switch in the three-layer model, joined by CDP/LLDP links
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Device tracker: SNMP scans of every switch, compared with the last ones
# ---------------------------------------------------------------------------
def _typed_login(payload):
    """The SNMP login typed in the page. Only known values; empty secrets mean 'use the saved one'."""
    out = {}
    if payload.get("version") in (snmpwalk.V2C, snmpwalk.V3):
        out["version"] = payload["version"]
    if payload.get("auth_protocol") in snmpwalk.AUTH:
        out["auth_protocol"] = payload["auth_protocol"]
    if payload.get("priv_protocol") in snmpwalk.PRIV:
        out["priv_protocol"] = payload["priv_protocol"]
    for key in ("username", *tracker.SECRETS):
        value = payload.get(key)
        if isinstance(value, str) and value and len(value) <= 128:
            out[key] = value if key != "username" else value.strip()
    return out


def _tracker_login(payload, settings):
    """(creds, problems)"""
    try:
        creds = tracker.credentials(settings, _typed_login(payload))
    except Exception as exc:                                    # noqa: BLE001 - Credential Manager trouble
        return None, [f"Couldn't read the saved SNMP login: {exc}"]
    return creds, tracker.missing_login(creds)


def _saved_secrets():
    try:
        return tracker.saved_secrets(), ""
    except Exception as exc:                                    # noqa: BLE001
        return {name: False for name in tracker.SECRETS}, str(exc)


@app.get("/tracker")
def tracker_page():
    return render_template("tracker.html")


@app.get("/api/tracker/setup")
def tracker_setup():
    saved, problem = _saved_secrets()
    log = ""
    if TRACKER.last_log.exists():
        lines = TRACKER.last_log.read_text(encoding="utf-8", errors="replace").splitlines()
        log = {"when": datetime.fromtimestamp(TRACKER.last_log.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
               "lines": lines[-12:]}
    return jsonify({"settings": TRACKER.settings(), "saved": saved, "store_problem": problem,
                    "schedule": tracker.schedule_status(), "auth": list(snmpwalk.AUTH), "priv": list(snmpwalk.PRIV),
                    "switches": len(load_switches()), "last_log": log,
                    "history": len(TRACKER.history().get("devices", {}))})


def _int_setting(payload, key, low, high, problems, label):
    if key not in payload:
        return None
    try:
        value = int(payload[key])
        if not low <= value <= high:
            raise ValueError
        return value
    except (TypeError, ValueError):
        problems.append(f"{label} must be a whole number from {low} to {high}.")
        return None


@app.post("/api/tracker/setup")
def tracker_setup_save():
    """Save the SNMP settings; passwords and the community string go to Windows Credential Manager."""
    payload = request.json or {}
    settings, problems = TRACKER.settings(), []
    typed = _typed_login(payload)
    for key in ("version", "auth_protocol", "priv_protocol"):
        if key in typed:
            settings[key] = typed[key]
    if "username" in payload:
        settings["username"] = str(payload.get("username") or "").strip()[:64]
    for key, low, high, label in (("missing_after", 1, 30, "Missing after"), ("busy_limit", 2, 1000, "Busy port limit"),
                                  ("keep_days", 7, 3650, "Keep scans for"), ("forget_days", 30, 3650, "Forget after"),
                                  ("parallel", 1, 50, "Switches at once"), ("timeout", 1, 30, "Timeout")):
        value = _int_setting(payload, key, low, high, problems, label)
        if value is not None:
            settings[key] = value
    if "dhcp_servers" in payload:
        servers, bad = tracker.parse_servers(payload.get("dhcp_servers"))
        problems += bad
        settings["dhcp_servers"] = ", ".join(servers)
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    TRACKER.save_settings(settings)
    try:
        for name in tracker.SECRETS:
            if typed.get(name):
                tracker.save_secret(name, typed[name])
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"ok": False, "problems": [f"Settings saved, but the login couldn't be saved in Windows "
                                                  f"Credential Manager: {exc}"]}), 500
    saved, _ = _saved_secrets()
    return jsonify({"ok": True, "settings": settings, "saved": saved})


@app.post("/api/tracker/forget")
def tracker_forget():
    try:
        tracker.forget_secrets()
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"ok": False, "problems": [str(exc)]}), 500
    saved, _ = _saved_secrets()
    return jsonify({"ok": True, "saved": saved})


@app.post("/api/tracker/test")
def tracker_test():
    payload = request.json or {}
    chosen = [s for s in load_switches() if s["address"] == payload.get("address")]
    if not chosen:
        return jsonify({"ok": False, "problems": ["Pick a switch from your list."]}), 400
    settings = TRACKER.settings()
    creds, problems = _tracker_login(payload, settings)
    if problems:
        return jsonify({"ok": False, "problems": problems}), 400
    return jsonify(tracker.test_switch(chosen[0], creds, settings))


@app.post("/api/tracker/dhcp-test")
def tracker_dhcp_test():
    """Ask the DHCP servers typed in the page for their leases, as the Windows user running the app."""
    servers, problems = tracker.parse_servers((request.json or {}).get("servers"))
    if problems or not servers:
        return jsonify({"ok": False, "problems": problems or ["Enter a DHCP server name."]}), 400
    leases, problem = tracker.read_dhcp(servers)
    return jsonify({"ok": bool(leases) or not problem, "leases": len(leases), "problem": problem})


@app.post("/api/tracker/run")
def tracker_run():
    global tracker_job
    payload = request.json or {}
    with tracker_lock:
        if tracker_job.state == "running":
            return jsonify({"ok": False, "problems": ["A scan is already running."]}), 409
        switches = load_switches()
        if not switches:
            return jsonify({"ok": False, "problems": ["The switch list is empty. Add switches on the Port Analyzer "
                                                      "page first."]}), 400
        settings = TRACKER.settings()
        creds, problems = _tracker_login(payload, settings)
        if problems:
            return jsonify({"ok": False, "problems": problems}), 400
        if not TRACKER.lock():
            return jsonify({"ok": False, "problems": ["A scan is already running (the daily one, probably). "
                                                      "Wait for it to finish."]}), 409
        tracker_job = analyzer.Job()
        threading.Thread(target=_tracker_scan, args=(tracker_job, switches, creds, settings), daemon=True).start()
    return jsonify({"ok": True})


def _tracker_scan(job, switches, creds, settings):
    try:
        tracker.run_scan(job, TRACKER, switches, creds, settings, source="manual")
    finally:
        TRACKER.unlock()


@app.post("/api/tracker/stop")
def tracker_stop():
    tracker_job.stop_requested.set()
    tracker_job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/tracker/job")
def tracker_job_status():
    snap = tracker_job.snapshot(int(request.args.get("since", 0)))
    if snap["state"] != "running":
        other = TRACKER.progress()               # the daily scan runs in its own window: what it last wrote
        if other and other.get("state") == "running" and other.get("source") == "scheduled" \
                and time.time() - float(other.get("updated", 0)) < 30:
            snap["scheduled"] = {"progress": other.get("progress", {}), "started": other.get("started")}
    return jsonify(snap)


@app.get("/api/tracker/runs")
def tracker_runs():
    return jsonify({"runs": portdocs.list_runs(TRACKER.scans, 40), "total": len(saved_items("tracker"))})


@app.get("/tracker/view/<run>")
def tracker_view(run):
    folder = portdocs.run_folder(TRACKER.scans, run)
    if folder is None or not (folder / "scan.json").exists():
        abort(404)
    data = json.loads((folder / "scan.json").read_text(encoding="utf-8"))
    return render_template("tracker_view.html", data=data, run=run)


@app.get("/tracker/demo")
def tracker_demo_page():
    return render_template("tracker_view.html", data=tracker_demo.run_demo(), run="")


@app.get("/tracker/download/<run>/<kind>")
def tracker_download(run, kind):
    folder = portdocs.run_folder(TRACKER.scans, run)
    if folder is None or kind not in ("devices", "changes"):
        abort(404)
    path = folder / f"{kind}.csv"
    if not path.exists():
        abort(404)
    return Response(path.read_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="tracker-{kind}-{run}.csv"'})


@app.get("/api/tracker/schedule")
def tracker_schedule():
    return jsonify(tracker.schedule_status())


@app.post("/api/tracker/schedule")
def tracker_schedule_set():
    """{"on": true, "time": "10:00", "days": "daily"|"weekdays"} or {"on": false}."""
    payload = request.json or {}
    settings = TRACKER.settings()
    if payload.get("on"):
        when, days = str(payload.get("time", "")), payload.get("days")
        if not tracker._TIME.match(when) or days not in ("daily", "weekdays"):
            return jsonify({"ok": False, "problems": ["Pick a time like 10:00, and every day or weekdays."]}), 400
        _creds, missing = _tracker_login({}, settings)
        if missing:
            return jsonify({"ok": False, "problems": ["Save the SNMP login first: the daily scan uses the saved one."]
                            + missing}), 400
        settings.update({"time": when, "days": days})
        TRACKER.save_settings(settings)
        problem = tracker.schedule_on(when, days, APP_DIR)
    else:
        problem = tracker.schedule_off()
    if problem:
        return jsonify({"ok": False, "problems": [problem]}), 500
    return jsonify({"ok": True, "schedule": tracker.schedule_status()})


@app.post("/api/tracker/reset")
def tracker_reset():
    """Forget where every device was (the next scan starts over). Saved scans and CSVs stay."""
    if (request.json or {}).get("confirm") is not True:
        return jsonify({"ok": False, "problems": ["Say you're sure (confirm: true)."]}), 400
    if TRACKER.lock():
        try:
            TRACKER.history_file.unlink(missing_ok=True)
        finally:
            TRACKER.unlock()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "problems": ["A scan is running. Try again when it's done."]}), 409


@app.get("/network")
def network_form():
    return render_template("network_form.html")


@app.post("/api/network/run")
def network_run():
    global net_job
    payload = request.json or {}
    with net_lock:
        if net_job.state == "running":
            return jsonify({"ok": False, "problems": ["A map is already being drawn."]}), 409
        problems = []
        chosen, label = pick_switches(str(payload.get("scope", "")), load_switches())
        if not chosen:
            problems.append("Add your switches on the Port Analyzer page first." if not load_switches()
                            else "No switches match that choice.")
        logins = logins_from(payload)
        used = {s["protocol"] for s in chosen}
        if "telnet" in used and not logins["telnet"].password:
            problems.append("Enter the password.")
        if "ssh" in used and not logins["ssh"].password:
            problems.append("Enter the SSH password." if payload.get("ssh_different") else "Enter the password.")
        try:
            also_draw = network.parse_ranges(str(payload.get("also_draw", ""))[:500])
        except ValueError as exc:
            problems.append(str(exc))
        problems = list(dict.fromkeys(problems))
        if problems:
            return jsonify({"ok": False, "problems": problems}), 400
        net_job = analyzer.Job()
        threading.Thread(target=network.run_job,
                         args=(net_job, chosen, logins, load_settings(), NETWORK_DIR, DEBUG_DIR, label),
                         kwargs={"also_draw": also_draw, "whole_list": load_switches(), "learn": learn_from_switches},
                         daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/network/stop")
def network_stop():
    net_job.stop_requested.set()
    net_job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/network/job")
def network_status():
    return jsonify(net_job.snapshot(int(request.args.get("since", 0))))


@app.get("/api/network/pages")
def network_pages():
    return jsonify({"pages": network.list_pages(NETWORK_DIR), "total": len(saved_items("network"))})


@app.get("/network/page/<name>")
def network_page(name):
    pages = (NETWORK_DIR / "pages").resolve()
    path = (pages / name).resolve()
    if path.parent != pages or not path.is_file() or not re.fullmatch(r"network-\d{8}-\d{6}\.html", name):
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


# The latest map's ideas for the switch list: switches it found that aren't on it, and ones that seem to have moved
def _list_index(rows):
    return {r["address"].lower() for r in rows}, {r["name"].lower(): r for r in rows if r.get("name")}


def _annotated_suggestions():
    found = network.load_suggestions(NETWORK_DIR)
    if not found:
        return None
    addresses, names = _list_index(load_switches())
    for item in found.get("add", []):
        same_name = names.get(item["name"].lower())
        item["status"] = ("on_list" if item["address"].lower() in addresses
                          else "name_on_list" if same_name else "new")
        item["listed_as"] = same_name["address"] if same_name else ""
    for item in found.get("fix", []):
        item["status"] = ("done" if item["new"].lower() in addresses and item["old"].lower() not in addresses
                          else "taken" if item["new"].lower() in addresses
                          else "gone" if item["old"].lower() not in addresses else "ready")
    for item in found.get("gone", []):
        item["status"] = "on_list" if item["address"].lower() in addresses else "gone"
    return found


@app.get("/api/network/suggestions")
def network_suggestions():
    return jsonify({"suggestions": _annotated_suggestions(), "types": sw.TYPES})


@app.post("/api/network/suggestions/apply")
def network_suggestions_apply():
    """Add the found switches you ticked, and change the addresses you ticked. Only what the latest map suggested,
    and never something that would clash with the list: an address or a name that's already on it."""
    payload = request.json or {}
    found = network.load_suggestions(NETWORK_DIR) or {}
    offered_add = {i["address"]: i for i in found.get("add", [])}
    offered_fix = {(i["old"], i["new"]) for i in found.get("fix", [])}
    settings = load_settings()
    added, fixed, skipped = [], [], []
    with switches_lock:
        rows = load_switches()
        addresses, names = _list_index(rows)
        for want in payload.get("add") or []:
            item = offered_add.get(str((want or {}).get("address", "")))
            if not item:
                continue
            vtype = sw.clean_type(str(want.get("type", item["type"]) or ""))     # (you can change the type it guessed)
            if not vtype:
                skipped.append(f"{item['name']} ({item['address']}): pick its type.")
                continue
            if item["address"].lower() in addresses:
                skipped.append(f"{item['name']} ({item['address']}): that address is already on your list.")
                continue
            if item["name"].lower() in names:
                skipped.append(f"{item['name']} ({item['address']}): a switch called {item['name']} is already on your "
                               f"list, at {names[item['name'].lower()]['address']}.")
                continue
            same_type = [r["protocol"] for r in rows if r["type"] == vtype]       # how you reach this kind of switch
            protocol = max(set(same_type), key=same_type.count) if same_type else \
                sw.clean_protocol(settings.get("default_protocol")) or "telnet"
            building = " ".join(str(want.get("building", item.get("building", ""))).split())[:80]
            rows.append({"address": item["address"], "type": vtype, "protocol": protocol, "building": building,
                         "location": "", "name": item["name"][:80]})
            addresses.add(item["address"].lower())
            names[item["name"].lower()] = rows[-1]
            added.append(item["name"])
        for want in payload.get("fix") or []:
            old, new = str((want or {}).get("old", "")), str((want or {}).get("new", ""))
            if (old, new) not in offered_fix:
                continue
            row = next((r for r in rows if r["address"].lower() == old.lower()), None)
            if row is None:
                skipped.append(f"{old} isn't on your list any more.")
            elif new.lower() in addresses:
                skipped.append(f"{new} is already on your list, so {row['name'] or old} wasn't changed to it.")
            else:
                row["address"] = new
                addresses.discard(old.lower())
                addresses.add(new.lower())
                fixed.append(f"{row['name'] or new}: {old} -> {new}")
        if added or fixed:
            save_switches(rows)
    return jsonify({"ok": True, "added": added, "fixed": fixed, "skipped": skipped,
                    "suggestions": _annotated_suggestions()})


@app.get("/network/demo")
def network_demo():
    _, html, _ = network.run_demo()
    return Response(html, mimetype="text/html")


@app.get("/api/network/layers")
def network_layers():
    return jsonify({"ok": True, "layers": network.load_overrides(NETWORK_DIR)})


@app.post("/api/network/layer")
def network_set_layer():
    """Someone picked a switch's layer on the map (or put it back to automatic)."""
    payload = request.json or {}
    node_id, layer = str(payload.get("id", "")).strip(), str(payload.get("layer", "")).strip()
    if not node_id or len(node_id) > 200 or layer not in ("",) + network.LAYERS:
        return jsonify({"ok": False, "problems": ["That isn't a switch and a layer."]}), 400
    return jsonify({"ok": True, "layers": network.save_override(NETWORK_DIR, node_id, layer)})


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------
BROWSER_BLOCKED_PORTS = {5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080}


def free_port(start):
    for port in range(start, start + 20):
        if port in BROWSER_BLOCKED_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    sys.exit(f"No free port between {start} and {start + 19}.")


def main():
    global PORT
    PORT = free_port(PORT)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Port Analyzer is running at {url}")
    print("  Keep this window open while you use it. Close it to stop the app.\n")
    if not os.environ.get("PA_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    from waitress import serve
    try:
        serve(app, host="127.0.0.1", port=PORT, threads=8)
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
