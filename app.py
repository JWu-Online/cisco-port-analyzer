"""
Port Analyzer - local web app.

Run it with RunAnalyzer.bat (Windows) or start.sh (Mac/Linux), or:  python app.py
It only listens on this computer (127.0.0.1); nobody else on the network can reach it.
Passwords are kept in memory for the run and never written to disk.
"""

import csv
import json
import os
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request

import analyzer
import report
import switches as sw

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PA_DATA_DIR", APP_DIR / "data"))
REPORTS_DIR = DATA_DIR / "reports"
DEBUG_DIR = DATA_DIR / "debug"
SWITCHES_FILE = DATA_DIR / "switches.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {"months": 6, "default_protocol": "telnet", "parallel": 10, "timeout": 20}
SWITCH_FIELDS = ["address", "type", "protocol", "building", "location"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
PORT = int(os.environ.get("PA_PORT", "5000"))
job = analyzer.Job()
job_lock = threading.Lock()


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
                    "location": str(row.get("location", "")).strip()})
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
        where = chosen[0]["location"] if chosen else ""
        return chosen, f"{where} ({a})" if where else a
    return all_switches, f"every switch ({len(all_switches)})"


def report_path(name=None):
    if name:
        path = REPORTS_DIR / name
        return path if path.parent == REPORTS_DIR and path.exists() and path.suffix == ".json" else None
    files = sorted(REPORTS_DIR.glob("report-*.json")) if REPORTS_DIR.exists() else []
    return files[-1] if files else None


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
                         daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    job.stop_requested.set()
    job.log("Stopping after the switches already in progress finish...", "warn")
    return jsonify({"ok": True})


@app.get("/api/job")
def job_status():
    return jsonify(job.snapshot(int(request.args.get("since", 0))))


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
                    "seconds": seconds})


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
