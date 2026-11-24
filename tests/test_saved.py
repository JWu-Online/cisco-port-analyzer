"""
Deleting saved runs: one at a time, all of one kind (only when confirmed), and nothing else.

    python tests/test_saved.py

Makes pretend saved reports, Stack topology drawings, Port documentation runs, LLDP neighbor runs and
Network maps in a throwaway data folder, next to files that must never be touched (the Network map's
layer choices, Stack topology's memory of each switch, a stray file), and deletes through the web app.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PA_DATA_DIR"] = tempfile.mkdtemp(prefix="pa-saved-test-")

import app as webapp  # noqa: E402


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return bool(condition)


def make_files():
    d = webapp.DATA_DIR
    made = {"reports": [], "topology": [], "docs": [], "lldp": [], "network": []}
    (d / "reports").mkdir(parents=True)
    for i in range(3):
        name = f"report-2026112{i}-10000{i}.json"
        (d / "reports" / name).write_text(json.dumps({"created": f"2026-11-2{i} 10:00", "months": 6,
                                                      "scope": f'B Building "east" ({i + 4})', "switches": [],
                                                      "ports": []}), encoding="utf-8")
        made["reports"].append(name)
    (d / "topology" / "pages").mkdir(parents=True)
    (d / "topology" / "history").mkdir(parents=True)
    (d / "topology" / "history" / "10.0.5.1.json").write_text("{}", encoding="utf-8")       # never touched
    for i, host in enumerate(("E3-IDF-SW1", "B2.IDF")):
        name = f"{host}-2026112{i}-09000{i}.html"
        (d / "topology" / "pages" / name).write_text("<html></html>", encoding="utf-8")
        made["topology"].append(name)
    for kind in ("docs", "lldp"):
        for i in range(2):
            run = d / kind / f"2026112{i}-08000{i}"
            (run / "switches").mkdir(parents=True)
            (run / "run.json").write_text("{}", encoding="utf-8")
            (run / "switches" / "E3-IDF-SW1.csv").write_text("a,b\n", encoding="utf-8")
            made[kind].append(run.name)
        (d / kind / "20261101-000000").mkdir()        # a folder with no run.json isn't a run: never touched
    (d / "network" / "pages").mkdir(parents=True)
    (d / "network" / "layers.json").write_text("{}", encoding="utf-8")                      # never touched
    for i in range(2):
        name = f"network-2026112{i}-07000{i}.html"
        (d / "network" / "pages" / name).write_text("<html></html>", encoding="utf-8")
        made["network"].append(name)
    (d / "network" / "pages" / "notes.html").write_text("mine", encoding="utf-8")           # never touched
    return made


def main():
    ok = True
    made = make_files()
    d = webapp.DATA_DIR
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}

    print("\nListed, with how many there are")
    reports = client.get("/api/reports", **base).json
    ok &= check("saved reports, newest first, with what was checked (read from the start of each file)",
                reports["total"] == 3 and [r["file"] for r in reports["reports"]] == made["reports"][::-1]
                and reports["reports"][0]["scope"] == 'B Building "east" (6)' and reports["reports"][0]["months"] == "6",
                reports)
    ok &= check("totals on every other list",
                client.get("/api/topology/pages", **base).json["total"] == 2
                and client.get("/api/docs/runs", **base).json["total"] == 2
                and client.get("/api/lldp/runs", **base).json["total"] == 2
                and client.get("/api/network/pages", **base).json["total"] == 2)
    ok &= check("the main page loads its saved-reports list", b'id="savedReports"' in client.get("/", **base).data
                and b"/static/saved.js" in client.get("/", **base).data)

    print("\nOne at a time")
    for kind in made:
        name = made[kind][0]
        r = client.post("/api/saved/delete", json={"kind": kind, "name": name}, **base)
        path = (d / ("reports" if kind == "reports" else kind) / name) if kind in ("reports", "docs", "lldp") \
            else d / kind / "pages" / name
        ok &= check(f"{kind}: one deleted, the rest still there", r.status_code == 200 and not path.exists()
                    and r.json["left"] == len(made[kind]) - 1, (r.status_code, r.json))
    r = client.post("/api/saved/delete", json={"kind": "reports", "name": made["reports"][0]}, **base)
    ok &= check("deleting it again: already gone", r.status_code == 404 and "already gone" in r.json["problems"][0])
    refused = []
    for kind, name in (("network", "../layers.json"), ("network", "notes.html"), ("topology", "../history/10.0.5.1.json"),
                       ("docs", "20261101-000000"), ("docs", ".."), ("lldp", "../docs/20261121-080001"),
                       ("reports", "../network/layers.json"), ("nothing", "x"), ("reports", None),
                       ("reports", ["report-20261121-100001.json"])):
        r = client.post("/api/saved/delete", json={"kind": kind, "name": name}, **base)
        if r.status_code != 404:
            refused.append((kind, name, r.status_code))
    ok &= check("anything that isn't a saved run is refused, wherever it points", not refused, refused)
    ok &= check("non-JSON posts refused", client.post("/api/saved/delete", data="{}", **base).status_code == 415)

    print("\nAll of one kind")
    r = client.post("/api/saved/delete-all", json={"kind": "lldp"}, **base)
    ok &= check("not without confirm: true", r.status_code == 400 and len(webapp.saved_items("lldp")) == 1)
    r = client.post("/api/saved/delete-all", json={"kind": "lldp", "confirm": "yes"}, **base)
    ok &= check("only a real true counts", r.status_code == 400 and len(webapp.saved_items("lldp")) == 1)
    ok &= check("an unknown kind", client.post("/api/saved/delete-all", json={"kind": "data", "confirm": True},
                                                **base).status_code == 404)
    kinds = list(made)
    for i, kind in enumerate(kinds):
        r = client.post("/api/saved/delete-all", json={"kind": kind, "confirm": True}, **base)
        others = [len(webapp.saved_items(k)) for k in kinds[i + 1:]]
        ok &= check(f"{kind}: all deleted, the other kinds' runs still there",
                    r.status_code == 200 and r.json["deleted"] == len(made[kind]) - 1
                    and webapp.saved_items(kind) == [] and others == [len(made[k]) - 1 for k in kinds[i + 1:]],
                    (r.json, others))
    ok &= check("never touched: layer choices, Stack topology's memory, a folder that isn't a run, a stray file",
                (d / "network" / "layers.json").exists() and (d / "topology" / "history" / "10.0.5.1.json").exists()
                and (d / "docs" / "20261101-000000").is_dir() and (d / "lldp" / "20261101-000000").is_dir()
                and (d / "network" / "pages" / "notes.html").exists())
    r = client.post("/api/saved/delete-all", json={"kind": "reports", "confirm": True}, **base)
    ok &= check("delete all with nothing left: fine, 0", r.status_code == 200 and r.json["deleted"] == 0)

    print("\nA file that's open somewhere (Windows won't delete it)")
    run = d / "docs" / "20261124-120000"
    run.mkdir()
    (run / "run.json").write_text("{}", encoding="utf-8")
    real = shutil.rmtree

    def locked(path, *a, **k):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    shutil.rmtree = locked
    try:
        r = client.post("/api/saved/delete", json={"kind": "docs", "name": run.name}, **base)
        ok &= check("one: said why, and kept", r.status_code == 409 and "close it and try again" in r.json["problems"][0]
                    and run.exists(), r.json)
        r = client.post("/api/saved/delete-all", json={"kind": "docs", "confirm": True}, **base)
        ok &= check("all: said which couldn't be deleted", r.status_code == 409 and r.json["deleted"] == 0
                    and run.name in r.json["problems"][0], r.json)
    finally:
        shutil.rmtree = real

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
