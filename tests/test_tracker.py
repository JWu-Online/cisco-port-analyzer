"""
Device tracker: reading made-up switches' SNMP tables, deciding where each device is, comparing scans, saving
CSVs, the saved login staying out of the data folder, and the web pages.

    python tests/test_tracker.py

No switch is asked anything: the switches are made-up SNMP tables (tests/snmp_samples.py), and the saved login
goes to a pretend Credential Manager.
"""

import base64
import copy
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["PA_DATA_DIR"] = tempfile.mkdtemp(prefix="pa-tracker-test-")

import analyzer  # noqa: E402
import snmp_samples as S  # noqa: E402
import tracker as T  # noqa: E402

ok = True


def check(label, condition, detail=""):
    global ok
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    ok &= bool(condition)


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


KEYRING = FakeKeyring()
T._keyring = lambda: KEYRING


def read(sample, switch, **kw):
    return T.read_switch(S.walker(sample, **kw), switch)


def test_reading():
    print("Reading a switch's tables")
    c = read(S.cisco(), S.SWITCHES[0])
    check("name from sysName, without the domain", c["name"] == "C449_2960S", c["name"])
    by_port = {nb["local"]: nb for nb in c["neighbors"]}
    check("CDP phone, its port and kind", by_port["Gi1/0/1"]["name"] == "SEP001122334401"
          and by_port["Gi1/0/1"]["kind"] == "phone" and by_port["Gi1/0/1"]["ip"] == "10.0.40.11", by_port["Gi1/0/1"])
    check("LLDP access point: MAC chassis, address from the index, W capability",
          by_port["Gi1/0/3"]["chassis"] == S.AP_MAC and by_port["Gi1/0/3"]["ip"] == "10.0.50.3"
          and by_port["Gi1/0/3"]["kind"] == "ap", by_port["Gi1/0/3"])
    check("Windows PC: its computer name, its MAC as the port id", by_port["Gi1/0/2"]["chassis"] == "DESKTOP-7Q2"
          and by_port["Gi1/0/2"]["port"] == S.WIN_PC and by_port["Gi1/0/2"]["kind"] == "pc", by_port["Gi1/0/2"])
    check("CDP uplink is a switch", by_port["Gi1/1/1"]["kind"] == "network" and by_port["Gi1/1/1"]["name"] == "CORE-1")
    macs = {(m["mac"], m["port"], m["vlan"]) for m in c["macs"]}
    check("Cisco MAC table read one VLAN at a time", (S.PHONE_C, "Gi1/0/1", 40) in macs
          and (S.PRINTER, "Gi1/0/4", 10) in macs and "one per VLAN" in c["notes"][0], c["notes"])
    check("the switch's own MAC (status self, port 0) is left out", not any(m[0] == "0011.2233.9999" for m in macs))
    check("access VLAN per port, port descriptions, ARP", c["port_vlan"]["Gi1/0/4"] == 10
          and c["aliases"]["Gi1/0/4"] == "C449 printer" and c["arp"][S.PRINTER] == "10.0.10.24")

    d = read(S.dell(), S.SWITCHES[1])
    by_port = {nb["local"]: nb for nb in d["neighbors"]}
    check("Dell: LLDP ports by ifIndex when they're named by MAC", set(by_port) == {"Gi1/0/1", "Te1/0/1"}, set(by_port))
    check("LLDP-MED phone (bridge + telephone)", by_port["Gi1/0/1"]["kind"] == "phone", by_port["Gi1/0/1"])
    macs = {(m["mac"], m["port"], m["vlan"]) for m in d["macs"]}
    check("Q-BRIDGE MAC table, VLAN from the fdb id", (S.PHONE_D, "Gi1/0/1", 40) in macs
          and (S.CAMERA, "Gi1/0/2", 20) in macs, macs)
    check("untagged VLAN per port from Q-BRIDGE", d["port_vlan"].get("Gi1/0/2") == 20, d["port_vlan"])

    bad = read(S.cisco(), S.SWITCHES[0], fail_vlans=(40,))
    check("a per-VLAN table that fails: MAC table marked incomplete, neighbors still read",
          not bad["macs_ok"] and "VLAN 40" in bad["mac_problem"] and len(bad["neighbors"]) == 4, bad["mac_problem"])

    check("LLDP ids by type", T._lldp_id(4, bytes.fromhex("001122334455")) == "0011.2233.4455"
          and T._lldp_id(3, bytes.fromhex("001122334455"), port=True) == "0011.2233.4455"
          and T._lldp_id(5, bytes([1, 10, 0, 0, 9])) == "10.0.0.9" and T._lldp_id(7, b"Gi1/0/3") == "Gi1/0/3")
    check("CDP capabilities", T._cdp_caps(bytes([0, 0, 0, 0x29])) == "Router Switch IGMP"
          and T._cdp_caps(bytes([0, 0, 0, 0x90])) == "Host Phone")


def scan(cisco=None, dell=None, failed=()):
    results = []
    for sw, sample in ((S.SWITCHES[0], cisco or S.cisco()), (S.SWITCHES[1], dell or S.dell())):
        if sw["address"] in failed:
            results.append({"address": sw["address"],
                            "name": sw.get("name") or sw["address"], "type": sw["type"], "building": sw["building"],
                            "location": sw["location"], "neighbors": [], "macs": [], "arp": {}, "port_vlan": {},
                            "aliases": {}, "problem": "No answer.", "notes": [], "lldp_ok": False, "macs_ok": False,
                            "mac_problem": "", "ports": 0})
        else:
            results.append(read(sample, sw))
    return results


def test_where():
    print("Where each device is")
    devices = T.devices_from(scan(), S.SWITCHES, T.DEFAULT_SETTINGS)
    check("MACs through the uplink and behind the access point are left out",
          S.UPLINK_MAC not in devices and S.AP_CLIENT not in devices)
    check("our own switches aren't devices; an unlisted one is",
          not any(d["name"] == "CORE-1" for d in devices.values())
          and any(d["name"] == "CORE-2" for d in devices.values()))
    phone = devices[S.PHONE_C]
    check("phone: CDP and the MAC table, one device, its voice VLAN",
          phone["found_by"] == "CDP + MAC table" and phone["places"][0]["vlan"] == 40 and phone["kind"] == "phone",
          (phone["found_by"], phone["places"]))
    check("the PC behind the phone is its own device on the same port",
          devices[S.PC_BEHIND_PHONE]["places"][0]["port"] == "Gi1/0/1")
    check("Windows PC: LLDP and the MAC table, one device", devices[S.WIN_PC]["found_by"] == "LLDP + MAC table"
          and devices[S.WIN_PC]["name"] == "DESKTOP-7Q2", devices[S.WIN_PC])
    check("printer: MAC table only, its IP from ARP", devices[S.PRINTER]["ip"] == "10.0.10.24"
          and devices[S.PRINTER]["kind"] == "unknown")
    check("port description comes along", devices[S.PRINTER]["places"][0]["port_description"] == "C449 printer")
    busy = T.devices_from(scan(), S.SWITCHES, {**T.DEFAULT_SETTINGS, "busy_limit": 1})
    check("a port with more MACs than the busy limit is left out", S.PC_BEHIND_PHONE not in busy
          and S.PRINTER in busy)


def test_compare():
    print("Comparing scans")
    settings = dict(T.DEFAULT_SETTINGS)
    day = datetime(2026, 11, 20, 10, 0)
    results = scan()
    devices = T.devices_from(results, S.SWITCHES, settings)
    changes, history, baseline = T.compare(devices, results, {}, settings, day)
    check("first scan: the starting point, no changes", baseline and changes == [] and len(history["devices"]) == len(devices))

    # Day 2: the printer moves to Gi1/0/2 and the phone's PC gets a new VLAN; a new camera; the Dell's camera is gone
    c = S.cisco()
    pm = S._idx_mac(S.PRINTER)
    c[10][f"{T.BRIDGE_FDB}.2.{pm}"] = 2
    pc = S._idx_mac(S.PC_BEHIND_PHONE)
    for col in (1, 2, 3):
        c[40][f"{T.BRIDGE_FDB}.{col}.{pc}"] = c[10].pop(f"{T.BRIDGE_FDB}.{col}.{pc}")
    new = "accc.8e00.0999"
    c[10][f"{T.BRIDGE_FDB}.1.{S._idx_mac(new)}"] = S._mac(new)
    c[10][f"{T.BRIDGE_FDB}.2.{S._idx_mac(new)}"] = 4
    c[10][f"{T.BRIDGE_FDB}.3.{S._idx_mac(new)}"] = 3
    d = S.dell()
    cm = S._idx_mac(S.CAMERA)
    for col in (2, 3):
        d[None].pop(f"{T.QBRIDGE_FDB}.{col}.1.{cm}")
    results = scan(c, d)
    devices = T.devices_from(results, S.SWITCHES, settings)
    changes, history, _ = T.compare(devices, results, history, settings, day + timedelta(days=1))
    kinds = {(ch["change"], ch["mac"]) for ch in changes}
    check("moved", ("moved", S.PRINTER) in kinds, kinds)
    moved = next(ch for ch in changes if ch["change"] == "moved")
    check("moved: from and to", moved["from"]["port"] == "Gi1/0/4" and moved["to"]["port"] == "Gi1/0/2", moved)
    check("new", ("new", new) in kinds)
    check("VLAN changed (same port)", ("vlan", S.PC_BEHIND_PHONE) in kinds, kinds)
    check("gone one day isn't missing yet", not any(ch["change"] == "missing" for ch in changes))

    # Day 3: the Dell can't be read: its camera isn't counted
    results = scan(c, d, failed=(S.SWITCHES[1]["address"],))
    devices = T.devices_from(results, S.SWITCHES, settings)
    changes, history, _ = T.compare(devices, results, history, settings, day + timedelta(days=2))
    check("a switch that wasn't read doesn't count against its devices",
          history["devices"][S.CAMERA]["missed"] == 1 and not changes, (history["devices"][S.CAMERA], changes))
    check("and its other devices aren't missing either", history["devices"][S.PHONE_D]["missed"] == 0)

    # Days 4 and 5: read fine, camera still gone: missing on the third scan it's gone from a read switch
    for n in (3, 4):
        results = scan(c, d)
        devices = T.devices_from(results, S.SWITCHES, settings)
        changes, history, _ = T.compare(devices, results, history, settings, day + timedelta(days=n))
    check("missing after 3 scans in a row", [ch["change"] for ch in changes] == ["missing"]
          and changes[0]["mac"] == S.CAMERA and changes[0]["from"]["switch_name"] == "B2-IDF", changes)
    results = scan(c, d)
    devices = T.devices_from(results, S.SWITCHES, settings)
    changes, history, _ = T.compare(devices, results, history, settings, day + timedelta(days=5))
    check("missing is said once", changes == [], changes)

    # Day 7: the camera is back where it was
    results = scan(c, S.dell())
    devices = T.devices_from(results, S.SWITCHES, settings)
    changes, history, _ = T.compare(devices, results, history, settings, day + timedelta(days=6))
    check("back", [ch["change"] for ch in changes] == ["back"] and changes[0]["mac"] == S.CAMERA, changes)
    check("first seen is kept", history["devices"][S.CAMERA]["first_seen"] == "2026-11-20 10:00")

    # A long time later: a device not seen for months is forgotten
    old = copy.deepcopy(history)
    old["devices"]["dead.beef.0001"] = {"mac": "dead.beef.0001", "name": "", "ip": "", "kind": "unknown", "model": "",
                                        "places": [{"switch": "10.0.1.11", "switch_name": "C449_2960S", "port": "Gi1/0/9",
                                                    "vlan": 10}], "first_seen": "2026-01-01 10:00",
                                        "last_seen": "2026-01-02 10:00", "missed": 50, "missing": True}
    _changes, newer, _ = T.compare(devices, results, old, settings, day + timedelta(days=7))
    check("forgotten after forget_days", "dead.beef.0001" not in newer["devices"])


def test_saving():
    print("Saving a scan")
    store = T.Store(Path(os.environ["PA_DATA_DIR"]) / "save-test")
    results = scan()
    devices = T.devices_from(results, S.SWITCHES, T.DEFAULT_SETTINGS)
    devices[S.PRINTER]["name"] = "=HYPERLINK(\"x\")"
    now = datetime(2026, 11, 24, 10, 0)
    changes, history, baseline = T.compare(devices, results, {}, T.DEFAULT_SETTINGS, now)
    folder = T.save_scan(store.scans, results, devices, changes, history, baseline, "scheduled", now)
    raw = (folder / "devices.csv").read_bytes()
    check("CSV starts with a BOM (Excel opens it as UTF-8)", raw.startswith(b"\xef\xbb\xbf"))
    rows = list(csv.DictReader((folder / "devices.csv").read_text(encoding="utf-8-sig").splitlines()))
    printer = next(r for r in rows if r["MAC"] == S.PRINTER)
    check("formula-like text gets a ' in front", printer["Name"].startswith("'="), printer["Name"])
    check("columns", list(rows[0]) == [c[0] for c in T.DEVICE_COLUMNS])
    summary = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    check("summary", summary["source"] == "scheduled" and summary["baseline"] and summary["devices"] == len(rows))
    old = store.scans / "20250101-100000"
    old.mkdir(parents=True)
    (old / "run.json").write_text("{}", encoding="utf-8")
    check("old scans are deleted after keep_days", T.prune(store.scans, 90, now) == 1 and not old.exists()
          and folder.exists())


def test_lock_and_login():
    print("One scan at a time, and the saved login")
    store = T.Store(Path(os.environ["PA_DATA_DIR"]) / "lock-test")
    check("first lock", store.lock())
    check("second lock refused", not store.lock())
    old = time.time() - 4 * 3600
    os.utime(store.lock_file, (old, old))
    check("a lock left behind hours ago is replaced", store.lock())
    store.unlock()
    check("unlocked", not store.lock_file.exists())

    settings = dict(T.DEFAULT_SETTINGS, version="3", username="tracker", auth_protocol="SHA", priv_protocol="AES")
    creds = T.credentials(settings)
    check("nothing saved: says what's missing", len(T.missing_login(creds)) == 2, T.missing_login(creds))
    T.save_secret("auth_password", "a-secret-1")
    T.save_secret("priv_password", "p-secret-2")
    creds = T.credentials(settings)
    check("saved login used", creds["auth_password"] == "a-secret-1" and not T.missing_login(creds))
    creds = T.credentials(settings, {"auth_password": "typed-3"})
    check("typed beats saved", creds["auth_password"] == "typed-3" and creds["priv_password"] == "p-secret-2")
    T.forget_secrets()
    T.save_secret("auth_password", "a-secret-1")
    check("v3 with no encryption needs no privacy password",
          T.missing_login(T.credentials({**settings, "priv_protocol": "none"})) == [])
    T.forget_secrets()
    check("forgotten", not any(T.saved_secrets().values()))


def test_scan_job():
    print("A whole scan")
    store = T.Store(Path(os.environ["PA_DATA_DIR"]) / "job-test")
    samples = {S.SWITCHES[0]["address"]: S.cisco(), S.SWITCHES[1]["address"]: S.dell()}

    def fake(switch, creds, settings):
        if switch["address"] not in samples:
            return {"address": switch["address"], "name": switch["name"] or switch["address"], "type": "",
                    "building": "", "location": "", "neighbors": [], "macs": [], "arp": {}, "port_vlan": {},
                    "aliases": {}, "problem": "No answer.", "notes": [], "lldp_ok": False, "macs_ok": False,
                    "mac_problem": "", "ports": 0}
        return T.read_switch(S.walker(samples[switch["address"]]), switch)

    job = analyzer.Job()
    folder = T.run_scan(job, store, [S.SWITCHES[2]], {}, T.DEFAULT_SETTINGS, scan_one=fake)
    check("no switch read: nothing saved, history untouched", folder is None and job.state == "error"
          and not store.history_file.exists())
    job = analyzer.Job()
    folder = T.run_scan(job, store, S.SWITCHES, {}, T.DEFAULT_SETTINGS, scan_one=fake)
    check("saved", folder is not None and job.state == "done" and (folder / "scan.json").exists(), job.lines[-1:])
    check("history saved", len(store.history()["devices"]) > 5)
    check("progress written for the page", store.progress()["state"] == "done")


def test_schedule():
    print("The daily task")
    calls = []
    T.IS_WINDOWS = True
    real = T._schtasks
    T._schtasks = lambda args: (calls.append(args) or (0, "SUCCESS"))
    try:
        problem = T.schedule_on("09:45", "weekdays", Path(r"C:\Users\jwu\cisco port analyzer"))
        args = calls[-1]
        check("weekdays at 09:45", problem == "" and args[args.index("/ST") + 1] == "09:45"
              and args[args.index("/D") + 1] == "MON,TUE,WED,THU,FRI", args)
        tr = args[args.index("/TR") + 1]
        check("runs TrackDevices.bat, quoted for the space in the path", tr.startswith('"') and
              tr.endswith('TrackDevices.bat" --scheduled') and "cisco port analyzer" in tr, tr)
        check("a bad time is refused", T.schedule_on("25:00", "daily") != "")
        T.schedule_on("10:00", "daily")
        check("every day", calls[-1][calls[-1].index("/SC") + 1] == "DAILY")
    finally:
        T._schtasks = real
        T.IS_WINDOWS = os.name == "nt"


def test_app():
    print("The pages")
    import app as webapp
    webapp.TRACKER = T.Store(webapp.DATA_DIR)
    webapp.SAVED["tracker"]["dir"] = webapp.TRACKER.scans
    webapp.save_switches([{**s} for s in S.SWITCHES])
    samples = {S.SWITCHES[0]["address"]: S.cisco(), S.SWITCHES[1]["address"]: S.dell()}
    real_scan = T.scan_switch

    def fake(switch, creds, settings):
        if creds.get("community") != "right-one":
            return {**real_scan({"address": "192.0.2.1"}, {"version": "x"}, settings), "address": switch["address"]}
        sample = samples.get(switch["address"])
        if sample is None:
            return {**real_scan({"address": "192.0.2.1"}, {"version": "x"}, settings), "address": switch["address"]}
        return T.read_switch(S.walker(sample), switch)

    T.scan_switch = fake
    client = webapp.app.test_client()
    base = {"base_url": f"http://127.0.0.1:{webapp.PORT}"}
    try:
        check("page", client.get("/tracker", **base).status_code == 200)
        check("demo", client.get("/tracker/demo", **base).status_code == 200)
        r = client.post("/api/tracker/run", json={"version": "2c"}, **base)
        check("no login: says so", r.status_code == 400 and "community" in r.json["problems"][0], r.json)
        r = client.post("/api/tracker/setup", json={"version": "2c", "community": "right-one", "missing_after": 2}, **base)
        check("login saved", r.status_code == 200 and r.json["saved"]["community"], r.json)
        r = client.get("/api/tracker/setup", **base).json
        check("the page is told it's saved, never what it is", r["saved"]["community"]
              and "right-one" not in json.dumps(r) and r["settings"]["missing_after"] == 2)
        r = client.post("/api/tracker/setup", json={"missing_after": 99}, **base)
        check("bad option refused", r.status_code == 400)
        r = client.post("/api/tracker/test", json={"address": S.SWITCHES[1]["address"]}, **base)
        check("test one switch", r.status_code == 200 and r.json["ok"] and r.json["lldp"] == 2 and r.json["macs"] == 3,
              r.json)
        r = client.post("/api/tracker/test", json={"address": "10.9.9.9"}, **base)
        check("test: only switches on the list", r.status_code == 400)
        r = client.post("/api/tracker/run", json={}, **base)
        check("scan starts with the saved login", r.status_code == 200, r.json)
        for _ in range(100):
            snap = client.get("/api/tracker/job", **base).json
            if snap["state"] != "running":
                break
            time.sleep(0.05)
        check("scan done", snap["state"] == "done", snap["lines"][-2:])
        runs = client.get("/api/tracker/runs", **base).json
        run = runs["runs"][0]["run"]
        check("listed", runs["total"] == 1 and runs["runs"][0]["baseline"] and runs["runs"][0]["read"] == 2)
        check("its page", client.get(f"/tracker/view/{run}", **base).status_code == 200)
        r = client.get(f"/tracker/download/{run}/changes", **base)
        check("changes CSV", r.status_code == 200 and r.data.startswith(b"\xef\xbb\xbfChange,MAC"))
        check("devices CSV", client.get(f"/tracker/download/{run}/devices", **base).status_code == 200)
        check("no other file", client.get(f"/tracker/download/{run}/scan", **base).status_code == 404
              and client.get("/tracker/download/../x/devices", **base).status_code == 404)
        everything = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in webapp.DATA_DIR.rglob("*") if p.is_file())
        check("the community string is nowhere in the data folder", "right-one" not in everything)
        r = client.post("/api/tracker/reset", json={}, **base)
        check("start over wants confirm", r.status_code == 400 and webapp.TRACKER.history_file.exists())
        r = client.post("/api/tracker/reset", json={"confirm": True}, **base)
        check("start over", r.status_code == 200 and not webapp.TRACKER.history_file.exists()
              and (webapp.TRACKER.scans / run).exists())
        r = client.post("/api/saved/delete", json={"kind": "tracker", "name": run}, **base)
        check("delete a scan", r.status_code == 200 and not (webapp.TRACKER.scans / run).exists(), r.json)
        if os.name != "nt":
            r = client.post("/api/tracker/schedule", json={"on": True, "time": "10:00", "days": "daily"}, **base)
            check("daily task: only on Windows", r.status_code == 500 and "Windows" in r.json["problems"][0], r.json)
        r = client.post("/api/tracker/schedule", json={"on": True, "time": "nope", "days": "daily"}, **base)
        check("daily task: bad time refused", r.status_code == 400)
        r = client.post("/api/tracker/forget", json={}, **base)
        check("forget the login", r.status_code == 200 and not r.json["saved"]["community"])
        r = client.post("/api/tracker/schedule", json={"on": True, "time": "10:00", "days": "daily"}, **base)
        check("daily task needs a saved login", r.status_code == 400 and "Save the SNMP login" in r.json["problems"][0])
        r = client.post("/api/tracker/run", data="x", content_type="text/plain", **base)
        check("only JSON", r.status_code == 415)
    finally:
        T.scan_switch = real_scan


def test_demo():
    print("Demo")
    import tracker_demo
    page = tracker_demo.run_demo()
    check("every kind of change shows", all(page["counts"][k] for k in T.CHANGES), page["counts"])

def test_dhcp_lookup():
    print("The DHCP lookup")
    check("servers: commas or spaces, duplicates dropped", T.parse_servers("lgccdc1, lgccdc2 lgccdc1") == (["lgccdc1", "lgccdc2"], []),
          T.parse_servers("lgccdc1, lgccdc2 lgccdc1"))
    check("no servers", T.parse_servers("") == ([], []))
    servers, problems = T.parse_servers("dc1'; rm x")
    check("a name with a quote or ; is refused", problems and not any("'" in s or ";" in s for s in servers), (servers, problems))

    def row(mac, name, state="Active", expires="2026-11-26T10:00:00", ip="10.0.10.5", server="dc1"):
        return {"server": server, "scope": "10.0.10.0", "ip": ip, "name": name, "mac": mac, "state": state,
                "expires": expires}
    rows = [row("AA-BB-CC-00-00-01", "pc123.lgcc.cuny.edu"),
            row("AA-BB-CC-00-00-02", "old-name", state="Expired"),
            row("AA-BB-CC-00-00-03", "sooner", expires="2026-11-25T12:00:00", ip="10.0.10.31"),
            row("AA-BB-CC-00-00-03", "later", expires="2026-11-27T12:00:00", ip="10.0.10.32"),
            row("AA-BB-CC-00-00-04", "later", expires="2026-11-27T12:00:00"),
            row("AA-BB-CC-00-00-04", "reserved", state="ActiveReservation", expires=""),
            row("AA-BB-CC-00-00-05", ""),
            row("00-01-00-01-2A-3B-4C-5D-AA-BB-CC-00-00-06", "duid-pc")]
    seen = []

    def run(args, timeout):
        # The script goes to PowerShell base64-encoded (no quoting trouble on the command line): read it back
        seen.append(base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le"))
        return 0, json.dumps(rows), ""
    real_windows = T.IS_WINDOWS
    T.IS_WINDOWS = True
    try:
        leases, problem = T.read_dhcp(["dc1"], run=run)
        check("lookup worked", problem == "" and "'dc1'" in seen[-1], (problem, seen[-1][:200]))
        check("name shortened to its first part", leases["aabb.cc00.0001"]["name"] == "pc123", leases.get("aabb.cc00.0001"))
        check("expired leases left out", "aabb.cc00.0002" not in leases)
        check("the newest lease wins", leases["aabb.cc00.0003"]["name"] == "later"
              and leases["aabb.cc00.0003"]["ip"] == "10.0.10.32")
        check("a reservation beats an active lease", leases["aabb.cc00.0004"]["name"] == "reserved")
        check("an empty host name stays empty", leases["aabb.cc00.0005"]["name"] == "")
        check("a client id that isn't a MAC is left out", len(leases) == 4, sorted(leases))

        both = rows[:1] + [{"server": "dc2", "error": "The term 'Get-DhcpServerv4Scope' is not recognized as the name of a cmdlet"}]
        leases, problem = T.read_dhcp(["dc1", "dc2"], run=lambda a, t: (0, json.dumps(both), ""))
        check("one server failing keeps the other's leases", "aabb.cc00.0001" in leases)
        check("missing RSAT said plainly", "DHCP PowerShell tools" in problem, problem)
        leases, problem = T.read_dhcp(["dc1", "dc2"], run=lambda a, t: (0, json.dumps(
            rows[:1] + [{"server": "dc2", "error": "Failed to get ... Access is denied."}]), ""))
        check("access denied names the server", "dc2" in problem and "aabb.cc00.0001" in leases, problem)

        def slow(args, timeout):
            raise subprocess.TimeoutExpired(args, timeout)
        leases, problem = T.read_dhcp(["dc1"], run=slow)
        check("too slow: no leases, says so", leases == {} and "2 minutes" in problem, problem)
        leases, problem = T.read_dhcp(["dc1"], run=lambda a, t: (1, "", "powershell broke"))
        check("PowerShell failing: says so", leases == {} and problem, problem)
    finally:
        T.IS_WINDOWS = real_windows
    if not real_windows:
        leases, problem = T.read_dhcp(["dc1"], run=run)
        check("not Windows: skipped with a note", leases == {} and "Windows" in problem, problem)


def main():
    test_reading()
    test_where()
    test_compare()
    test_saving()
    test_lock_and_login()
    test_scan_job()
    test_schedule()
    test_dhcp_lookup()
    test_app()
    test_demo()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
