"""
Network map test: reading Observium's tables, deciding layers, and drawing through the web app.

    python tests/test_network.py

Observium's database is played by an in-memory SQLite database with the same tables and
columns (devices, ports, neighbours), filled with the made-up campus in network_demo.py.
No MySQL or Observium needed.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("PA_DATA_DIR", tempfile.mkdtemp(prefix="pa-net-test-"))

import network as N  # noqa: E402
import network_demo as ND  # noqa: E402
import app as webapp  # noqa: E402


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return bool(condition)


# ---------------------------------------------------------------------------
# A pretend Observium database
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.db.queries.append(sql)
        cur = self.db.sqlite.execute(sql.replace("%s", "?"), tuple(params))
        self.rows = [dict(r) for r in cur.fetchall()]

    def fetchall(self):
        return self.rows


class FakeDB:
    """Looks like a PyMySQL connection with DictCursor, backed by SQLite."""

    def __init__(self, obs):
        self.queries, self.closed = [], False
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row
        self.sqlite.executescript("""
            CREATE TABLE devices (device_id INTEGER, hostname TEXT, sysName TEXT, ip TEXT, hardware TEXT, os TEXT,
              type TEXT, location TEXT, status INTEGER, disabled INTEGER, "ignore" INTEGER);
            CREATE TABLE ports (port_id INTEGER, device_id INTEGER, ifName TEXT, ifDescr TEXT, ifAlias TEXT,
              ifSpeed INTEGER, deleted INTEGER DEFAULT 0);
            CREATE TABLE neighbours (neighbour_id INTEGER PRIMARY KEY, device_id INTEGER, port_id INTEGER,
              protocol TEXT, remote_port_id INTEGER, remote_hostname TEXT, remote_port TEXT, remote_platform TEXT,
              remote_version TEXT, remote_address TEXT, autodiscovery_id INTEGER, active INTEGER);
        """)
        for table, rows in (("devices", obs["devices"]), ("ports", list(obs["ports"].values())),
                            ("neighbours", obs["neighbours"])):
            for row in rows:
                cols = ",".join(f'"{c}"' for c in row)
                self.sqlite.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                                    tuple(row.values()))

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeError(Exception):
    """Like pymysql's errors: (code, message)."""
    __module__ = "pymysql.err"


def parts():
    ok = True
    print("\nNames, gear and layers")
    ok &= check("short names", [N.short_name(x) for x in ("C4-DIST-1.lgcc.cuny.edu", "CORE-2(FOX123)", "10.0.0.1")]
                == ["C4-DIST-1", "CORE-2", "10.0.0.1"])
    ok &= check("neighbours Observium doesn't monitor: routers and switches kept, phones/APs/servers not",
                [N.looks_like_gear(h, p, v) for h, p, v in (
                    ("INET-RTR-1", "cisco ISR4451-X/K9", ""), ("B-SW", "cisco WS-C2960X-48FPD-L", ""),
                    ("SEP00112233AA01", "Cisco IP Phone 8845", ""), ("AP-1", "cisco AIR-AP2802I-B-K9", ""),
                    ("esx1", "VMware ESX", "Releasebuild"), ("laptop", "", "Windows 11"), ("x", "", ""),
                    ("00:11:22:33:44:55", "", ""))] == [True, True, False, False, False, False, False, False])
    ok &= check("models", [N.layer_hint(m)[0] for m in ("N7K-C7010", "WS-C4507R+E", "C9500-24Y4C", "C9300-48P",
                                                       "WS-C2960X-48FPD-L", "PowerConnect 5548P", "ArubaS3500-48P",
                                                       "WS-C6509-E", "SomethingElse")]
                == ["core", "distribution", "distribution", "access", "access", "access", "access", "core", None])
    ok &= check("firewalls and routers go with the core",
                N.layer_hint("ASA5525", "firewall")[0] == "core" and N.layer_hint("ISR4451", "router")[0] == "core")
    nodes = {k: {"hint": None, "hint_why": ""} for k in "ABCDEFG"}
    adj = {"A": {"B", "C", "G"}, "B": {"A", "D", "E"}, "C": {"A", "F"}, "D": {"B"}, "E": {"B"}, "F": {"C"}, "G": {"A"}}
    N.assign_layers(nodes, adj)
    ok &= check("no models known: the busiest switch in the middle is core, ones with switches below are distribution",
                {k: v["layer_auto"] for k, v in nodes.items()} ==
                {"A": "core", "B": "distribution", "C": "distribution", "D": "access", "E": "access", "F": "access",
                 "G": "access"}, {k: v["layer_auto"] for k, v in nodes.items()})
    ok &= check("port names from either end match", [N._port(x) for x in ("GigabitEthernet1/0/49", "Gi1/0/49",
                                                                          "Ethernet2/1", "Eth2/1", "GE0/0/47", "gi1/0/5")]
                == ["Gi1/0/49", "Gi1/0/49", "Eth2/1", "Eth2/1", "GE0/0/47", "Gi1/0/5"])

    print("\nPlain words for database problems")
    msgs = [N.friendly_db_error(FakeError(code, "x"), "obs", "observium", "portanalyzer", 3306)
            for code in (1045, 1049, 1142, 1130, 2003)]
    ok &= check("wrong password, wrong database, missing grant, not allowed from this PC, can't reach it",
                "refused the username or password" in msgs[0] and "doesn't exist" in msgs[1]
                and "SELECT on all three" in msgs[2] and "from this PC" in msgs[3] and "port 3306" in msgs[4], msgs)
    return ok


def demo_map():
    ok = True
    print("\nThe made-up campus, read from a pretend Observium database")
    db = FakeDB(ND.demo_observium())
    obs = N.read_observium(db)
    ok &= check("reads devices, neighbours and only the ports with a neighbour",
                len(obs["devices"]) == len(ND.demo_observium()["devices"])
                and len(obs["ports"]) == len(ND.demo_observium()["ports"])
                and not any("UPDATE" in q or "INSERT" in q or "DELETE" in q for q in db.queries), db.queries)
    data = N.build_map(obs, ND.demo_switch_list(), {}, "test")
    by = {n["name"]: n for n in data["nodes"]}
    layers = {name: n["layer"] for name, n in by.items()}
    ok &= check("cores, the firewall and the edge router on top; distribution; 26 closets",
                sorted(k for k, v in layers.items() if v == "core") == ["CORE-1", "CORE-2", "FW-1", "INET-RTR-1"]
                and sorted(k for k, v in layers.items() if v == "distribution") ==
                ["B-DIST", "C-DIST-1", "C-DIST-2", "E-DIST"]
                and len([v for v in layers.values() if v == "access"]) == 26, layers)
    ok &= check("left out: the VMware host Observium monitors, the phone and the access point it doesn't",
                "VMHOST-1" not in by and not any(re.match("SEP|AP-", n) for n in by)
                and data["summary"]["left_out"] == 1, sorted(by))
    ok &= check("the router Observium doesn't monitor is drawn, marked as such",
                not by["INET-RTR-1"]["listed"] and by["INET-RTR-1"]["model"] == "ISR4451-X/K9")
    names = {n["id"]: n["name"] for n in data["nodes"]}
    links = {tuple(sorted((names[l["a"]], names[l["b"]]))): l for l in data["links"]}
    core = links[("CORE-1", "CORE-2")]
    ok &= check("each link once, though Observium has it from both ends",
                len(data["links"]) == 47 and [(p["a_port"], p["b_port"], p["found_by"]) for p in core["pairs"]] ==
                [("Eth1/1", "Eth1/1", "CDP"), ("Eth1/2", "Eth1/2", "CDP")], len(data["links"]))
    dell = links.get(("B-DIST", "B2-IDF"))
    ok &= check("the Dell's link, known only from the Dell's end (LLDP), is kept with both ports",
                dell and {dell["pairs"][0]["a_port"], dell["pairs"][0]["b_port"]} == {"Gi3/2", "Gi1/0/49"}
                and dell["pairs"][0]["found_by"] == "LLDP", dell)
    ok &= check("dual-homed closets link to both distribution switches",
                ("C-DIST-1", "C1-IDF") in links and ("C-DIST-2", "C1-IDF") in links
                and ("C-DIST-2", "C4-IDF") not in links)
    ok &= check("a switch Observium shows as down is marked, with a warning",
                not by["B4-IDF"]["read"] and any("down: B4-IDF" in w for w in data["warnings"]))
    ok &= check("buildings and locations from the switch list; Observium's location when it's not on the list",
                by["E3-IDF"]["building"] == "E Building" and by["E3-IDF"]["location"] == "E3 IDF"
                and by["E3-IDF"]["obs_location"] == "LaGuardia, E3 IDF" and by["CORE-1"]["on_list"]
                and by["CORE-1"]["list_type"] == "cisco_nxos"
                and not by["FW-1"]["on_list"] and by["FW-1"]["location"] == "LaGuardia, Main MDF", (by["E3-IDF"], by["FW-1"]))

    moved = N.build_map(obs, ND.demo_switch_list(), {by["M1-IDF"]["id"]: "distribution"}, "test")
    m1 = next(n for n in moved["nodes"] if n["name"] == "M1-IDF")
    ok &= check("a layer you pick wins, and automatic is remembered",
                m1["layer"] == "distribution" and m1["layer_auto"] == "access", m1)

    print("\nOdd things in Observium's tables")
    odd = ND.demo_observium()
    odd["devices"][0]["disabled"] = 1                                   # CORE-1 disabled in Observium
    ids = {d["sysName"].split(".")[0]: d["device_id"] for d in odd["devices"]}
    for n in odd["neighbours"]:                                         # the B3 daisy chain gone stale
        if n["device_id"] in (ids["B3-IDF"], ids["B3-IDF-2"]) and n["remote_port_id"] and \
                odd["ports"][n["remote_port_id"]]["device_id"] in (ids["B3-IDF"], ids["B3-IDF-2"]):
            n["active"] = 0
    data = N.build_map(N.read_observium(FakeDB(odd)), [], {}, "test")
    by = {n["name"]: n for n in data["nodes"]}
    ok &= check("disabled devices and inactive neighbours are skipped",
                "CORE-1" not in by and by["B3-IDF-2"]["links"] == 0 and by["B3-IDF"]["links"] == 1
                and any("no links in Observium: B3-IDF-2" in w for w in data["warnings"]),
                (by.get("B3-IDF-2"), data["warnings"]))
    many = ND.demo_observium()
    extra = 1200
    first = max(many["ports"]) + 1
    for i in range(extra):                                              # a big switch: lots of neighbours
        pid = first + i
        many["ports"][pid] = {"port_id": pid, "device_id": 4, "ifName": f"Gi9/{i}", "ifDescr": "", "ifAlias": ""}
        many["neighbours"].append({"device_id": 4, "port_id": pid, "protocol": "cdp", "remote_port_id": None,
                                   "remote_hostname": f"SEP{i:012X}", "remote_port": "Port 1",
                                   "remote_platform": "Cisco IP Phone", "remote_version": "", "remote_address": "",
                                   "active": 1})
    db = FakeDB(many)
    obs = N.read_observium(db)
    ok &= check("ports are fetched in batches (over 1,000 neighbours)",
                len(obs["ports"]) == len(many["ports"])
                and sum("FROM ports" in q for q in db.queries) == -(-len(many["ports"]) // 500), len(obs["ports"]))
    html = N.render_html(N.build_map(obs, ND.demo_switch_list(), {}, "test"))
    ok &= check("page renders with the data and the filters",
                '"CORE-1"' in html and 'id="dist"' in html and 'id="collapse"' in html and "show-layer" in html)
    return ok


def through_the_app():
    ok = True
    print("\nThrough the web app")
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": ND.demo_switch_list()}, **base)
    seen = {}

    def connect(host, port, database, user, password, timeout=10):
        seen.update(host=host, port=port, database=database, user=user)
        if password != "read-only-pass":
            raise N.ObserviumError(f"The database refused the username or password for '{user}'.")
        return FakeDB(ND.demo_observium())
    webapp.OBSERVIUM_CONNECT = connect
    login = {"host": "observium.example.edu", "port": "3306", "database": "observium", "user": "portanalyzer",
             "password": "read-only-pass", "web": "https://observium.example.edu"}
    try:
        ok &= check("form page loads, the main page has the button, the sample draws",
                    client.get("/network", **base).status_code == 200
                    and b'href="/network"' in client.get("/", **base).data
                    and b"made-up sample" in client.get("/network/demo", **base).data)
        bad = [client.post("/api/network/draw", json={**login, **change}, **base) for change in
               ({"host": ""}, {"password": ""}, {"port": "abc"}, {"database": "x; DROP TABLE devices"},
                {"web": "observium.example.edu"})]
        ok &= check("missing or bad fields are refused before connecting",
                    all(r.status_code == 400 for r in bad) and not seen, [r.json for r in bad])
        r = client.post("/api/network/test", json={**login, "password": "wrong"}, **base)
        ok &= check("wrong password: said plainly", r.status_code == 502
                    and "refused the username or password" in r.json["problems"][0], r.json)
        r = client.post("/api/network/test", json=login, **base)
        ok &= check("test connection: counts what's there", r.status_code == 200 and r.json["devices"] == 34
                    and r.json["links"] == len(ND.demo_observium()["neighbours"]), r.json)
        r = client.post("/api/network/draw", json=login, **base)
        ok &= check("draw: saved, with a summary", r.status_code == 200 and r.json["file"].startswith("network-")
                    and r.json["summary"]["layers"]["distribution"] == 4, r.json)
        saved = client.get("/api/network/connection", **base).json["connection"]
        ok &= check("the fields are remembered, the password isn't",
                    saved == {"host": "observium.example.edu", "port": "3306", "database": "observium",
                              "user": "portanalyzer", "web": "https://observium.example.edu"}
                    and "read-only-pass" not in (webapp.NETWORK_DIR / "observium.json").read_text(), saved)
        pages = client.get("/api/network/pages", **base).json["pages"]
        page = client.get(f"/network/page/{r.json['file']}", **base)
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page.data.decode(),
                                    re.S).group(1))
        ok &= check("map listed and opens; no password in it; links to Observium pages",
                    pages[0]["file"] == r.json["file"] and b"read-only-pass" not in page.data
                    and data["web"] == "https://observium.example.edu"
                    and all(n["observium_id"] for n in data["nodes"] if n["listed"]), pages[:1])
        core1 = next(n["id"] for n in data["nodes"] if n["name"] == "CORE-1")
        r = client.post("/api/network/layer", json={"id": core1, "layer": "distribution"}, **base)
        ok &= check("pick a layer: saved", r.status_code == 200 and r.json["layers"] == {core1: "distribution"}
                    and client.get("/api/network/layers", **base).json["layers"] == {core1: "distribution"}, r.json)
        redraw = client.post("/api/network/draw", json=login, **base).json
        ok &= check("...and the next map uses it", redraw["summary"]["layers"]["core"] == 3, redraw["summary"])
        r = client.post("/api/network/layer", json={"id": core1, "layer": ""}, **base)
        ok &= check("back to automatic", r.json["layers"] == {})
        ok &= check("nonsense layers refused",
                    client.post("/api/network/layer", json={"id": core1, "layer": "galaxy"}, **base).status_code == 400)
        ok &= check("other files can't be opened as maps",
                    client.get("/network/page/..%2Flayers.json", **base).status_code == 404
                    and client.get("/network/page/observium.json", **base).status_code == 404)
        ok &= check("non-JSON posts refused", client.post("/api/network/draw", data="{}", **base).status_code == 415)
        ok &= check("Draw this stack opens Stack topology with the switch picked",
                    b'URLSearchParams(location.search).get("address")' in client.get("/topology", **base).data)
    finally:
        webapp.OBSERVIUM_CONNECT = None
    return ok


def main():
    ok = parts()
    ok &= demo_map()
    ok &= through_the_app()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
