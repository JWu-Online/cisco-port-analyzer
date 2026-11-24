"""
LLDP neighbors test: reading ports and neighbors, telling phones from access points, and a full run
through the web app.

    python tests/test_lldp.py

The full run starts a pretend switch for each switch of the made-up building in lldp_demo.py (a Catalyst
stack, a second Catalyst, a Dell, an Aruba, a Nexus over SSH) plus one that refuses the login, reads them
the way the page does and checks the page, the CSVs and the switch list.
"""

import csv
import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("PA_DATA_DIR", tempfile.mkdtemp(prefix="pa-lldp-test-"))

from mock_switch import MockCLI, SSHSwitch, TelnetSwitch  # noqa: E402
import samples as S  # noqa: E402
import lldp as L  # noqa: E402
import lldp_demo as LD  # noqa: E402
import app as webapp  # noqa: E402
from test_network import IOS_CDP, IOS_LLDP, DELL_LLDP, ARUBA_LLDP  # noqa: E402


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"   ({detail})" if detail and not condition else ""))
    return bool(condition)


def reading():
    ok = True
    print("\nPorts")
    ios = L.ports_from({"status": S.OUTPUTS["cisco_ios"]["show interfaces status"]}, "cisco_ios")
    ok &= check("Catalyst: every front port, its description, status and VLAN; no port-channel",
                [(p["port"], p["description"], p["status"], p["vlan"]) for p in ios] ==
                [("Gi1/0/1", "Printer 218", "connected", "15"), ("Gi1/0/2", "Room 218 desk", "notconnect", "15"),
                 ("Gi1/0/3", "", "notconnect", "131"), ("Gi1/0/4", "", "disabled", "1"),
                 ("Gi1/0/10", "", "notconnect", "126"), ("Te1/1/1", "Uplink to core", "connected", "trunk")],
                [(p["port"], p["description"], p["status"], p["vlan"]) for p in ios])
    nx = L.ports_from({"status": S.OUTPUTS["cisco_nxos"]["show interface status"]}, "cisco_nxos")
    ok &= check("Nexus: its shortened words written out, no mgmt0, '--' is no description",
                [(p["port"], p["description"], p["status"]) for p in nx] ==
                [("Eth1/1", "To C108 2960x", "connected"), ("Eth1/2", "spare", "notconnect"),
                 ("Eth1/3", "", "disabled"), ("Eth1/4", "", "xcvrAbsent")], nx)
    dell = L.ports_from({"status": S.OUTPUTS["dell_powerconnect"]["show interfaces status"],
                         "descriptions": S.OUTPUTS["dell_powerconnect"]["show interfaces description"]}, "dell_powerconnect")
    ok &= check("Dell: up/down said the Cisco way, descriptions from their own command",
                [(p["port"], p["description"], p["status"]) for p in dell] ==
                [("gi1/0/1", "Phone 218", "connected"), ("gi1/0/2", "Desk 2", "notconnect"), ("gi1/0/3", "", "notconnect"),
                 ("te1/0/1", "Uplink", "connected")], dell)
    aruba = L.ports_from({"interfaces": S.OUTPUTS["aruba_mas"]["show interface"]}, "aruba_mas")
    ok &= check("Aruba: shut down is 'disabled', the default description (its own name) is none",
                [(p["port"], p["description"], p["status"]) for p in aruba] ==
                [("GE0/0/0", "", "connected"), ("GE0/0/1", "Room M205 desk", "notconnect"),
                 ("GE0/0/2", "", "notconnect"), ("GE0/0/3", "", "disabled")], aruba)
    descs = L._descriptions("Interface                      Status         Protocol Description\n"
                            "Gi1/0/1                        up             up       Rm E-310 Desk 2 (long one, all of it)\n"
                            "Gi1/0/2                        admin down     down     \n")
    ok &= check("'show interfaces description' gives the whole description",
                descs == {("gi", "1/0/1"): "Rm E-310 Desk 2 (long one, all of it)", ("gi", "1/0/2"): ""}, descs)

    print("\nWhat each device is")
    cdp = L.neighbors_from({"cdp": IOS_CDP, "lldp": ""}, "cisco_ios")
    ok &= check("CDP: a switch, a phone, an access point", [nb["kind"] for nb in cdp] == ["network", "phone", "ap"], cdp)
    lldp = L.neighbors_from({"cdp": "", "lldp": IOS_LLDP}, "cisco_ios")
    ok &= check("LLDP: B,R is a switch, B,T a phone", [nb["kind"] for nb in lldp] == ["network", "phone"], lldp)
    ok &= check("Dell table: switches and a phone",
                [nb["kind"] for nb in L.neighbors_from({"lldp": DELL_LLDP}, "dell_powerconnect")]
                == ["network", "network", "phone"])
    ok &= check("Aruba table uses its own letters: A is an access point",
                [nb["kind"] for nb in L.neighbors_from({"lldp": ARUBA_LLDP}, "aruba_mas")] == ["network", "ap"])
    k = L.kind_of
    ok &= check("more: a WLC is gear not an AP, a station is 'other', an AP by its model, a SEP name is a phone",
                [k({"source": "CDP", "caps": "Router Switch Host", "platform": "cisco AIR-CT5520-K9"}),
                 k({"source": "LLDP", "caps": "S", "platform": "HP ETHERNET MULTI-ENVIRONMENT"}),
                 k({"source": "LLDP", "caps": "B,R", "platform": "Cisco AP Software, C9120AXI-B"}),
                 k({"source": "LLDP", "caps": "", "platform": "", "name": "SEP001122334455"}),
                 k({"source": "LLDP", "caps": "Bridge, WLAN access point", "platform": ""}),
                 k({"source": "LLDP", "caps": "P", "platform": ""}, "aruba_mas")]
                == ["network", "other", "ap", "phone", "ap", "phone"])

    print("\nWindows PCs (their built-in LLDP sends an id and a MAC, nothing else)")
    win = L.neighbors_from({"cdp": "", "lldp": LD._windows_block("Gi1/0/3", "LAB-PC-07", "9c7b.ef12.3456")
                            + LD._windows_block("Gi1/0/4", "9c7b.ef12.9999", "9c7b.ef12.9999")}, "cisco_ios")
    devs = [L._device({**nb, "sources": {nb["source"]}}) for nb in win]
    ok &= check("a computer, named by the computer name it sent, or by its MAC when that's all it sent",
                [(d["kind"], d["name"], d["mac"], d["model"], d["caps"]) for d in devs] ==
                [("pc", "LAB-PC-07", "9c7b.ef12.3456", "", ""), ("pc", "9c7b.ef12.9999", "9c7b.ef12.9999", "", "")], devs)
    nx_null = ("Chassis id: 9c7b.ef12.3456\nPort id: 9c7b.ef12.3456\nLocal Port id: Eth1/9\nPort Description: null\n"
               "System Name: null\nSystem Description: null\nTime remaining: 98 seconds\nSystem Capabilities: "
               "not advertised\nEnabled Capabilities: not advertised\nManagement Address: not advertised\n")
    nb = L.neighbors_from({"lldp": nx_null}, "cisco_nxos")[0]
    ok &= check("the same on a Nexus, which prints 'null' for what wasn't sent",
                nb["kind"] == "pc" and L._device({**nb, "sources": {"LLDP"}})["name"] == "9c7b.ef12.3456", nb)
    ok &= check("a Linux or Windows description with 'station' is a computer; a printer's is not",
                [L.kind_of({"source": "LLDP", "caps": "S", "platform": p}) for p in
                 ("Ubuntu 22.04.4 LTS Linux 5.15.0", "Windows Server 2022", "HP ETHERNET MULTI-ENVIRONMENT")]
                == ["pc", "pc", "other"])

    print("\nThe same device said two ways")
    phone_cdp = {"local": "Gi1/0/5", "name": "SEP001122334455", "ip": "10.0.5.5", "chassis": "", "port": "Port 1",
                 "platform": "Cisco IP Phone 8845", "caps": "Host Phone", "source": "CDP", "kind": "phone", "model": ""}
    phone_lldp = {"local": "Gi1/0/5", "name": "SEP001122334455", "ip": "10.0.5.5", "chassis": "0011.2233.4455",
                  "port": "0011.2233.4455", "platform": "Cisco IP Phone CP-8845, V3", "caps": "B,T", "source": "LLDP",
                  "kind": "phone", "model": "CP-8845"}
    merged = L.merge([phone_cdp, phone_lldp])
    ok &= check("CDP + LLDP of one phone -> one device, with both sources",
                len(merged) == 1 and merged[0]["sources"] == {"CDP", "LLDP"} and merged[0]["chassis"] == "0011.2233.4455")
    dev = L._device(merged[0])
    ok &= check("shown with its model (LLDP-MED) and MAC", (dev["name"], dev["model"], dev["mac"], dev["found_by"]) ==
                ("SEP001122334455", "CP-8845", "0011.2233.4455", "CDP + LLDP"), dev)
    other = {**phone_lldp, "name": "PC-42", "ip": "10.0.5.99", "chassis": "aaaa.bbbb.cccc", "kind": "other"}
    ok &= check("two different devices on a port stay two", len(L.merge([phone_cdp, phone_lldp, other])) == 2)
    pc = {"local": "Gi1/0/5", "name": "", "ip": "", "chassis": "PC-9", "port": "aaaa.bbbb.cccc", "platform": "",
          "caps": "", "source": "LLDP", "kind": "pc", "model": ""}
    ok &= check("a phone (CDP only) and the Windows PC behind it stay two",
                [m["kind"] for m in L.merge([phone_cdp, pc])] == ["phone", "pc"])
    ok &= check("MAC addresses in any of the usual forms", [L._mac(x) for x in
                ("00:11:22:33:00:01", "0011.2233.4455", "00-11-22-33-44-55", "eth0", "Gi0")] ==
                ["0011.2233.0001", "0011.2233.4455", "0011.2233.4455", "", ""])
    return ok


def demo():
    ok = True
    print("\nThe made-up building")
    data = L.run_demo()
    by = {s["name"]: s for s in data["switches"]}
    ok &= check("totals by type", data["counts"] == {"phone": 82, "ap": 7, "network": 11, "pc": 14, "other": 2}
                and data["read"] == 5 and data["failed"] == 1, data["counts"])
    sw1 = {p["port"]: p for p in by["E3-IDF-SW1"]["ports"]}
    ok &= check("a two-member stack: 48 + 4 uplinks each", len(sw1) == 104)
    ok &= check("the printer and the camera are 'other', the Polycom a phone named by its address",
                [(d["kind"], d["name"]) for d in sw1["Gi1/0/47"]["neighbors"] + sw1["Gi2/0/47"]["neighbors"]
                 + sw1["Gi2/0/46"]["neighbors"]] == [("other", "NPI0141B"), ("other", "axis-147f"), ("phone", "10.0.5.96")])
    ok &= check("err-disabled, shut down and the daisy-chained switch",
                sw1["Gi2/0/30"]["status"] == "err-disabled" and sw1["Gi1/0/44"]["status"] == "disabled"
                and [(d["name"], d["kind"], d["their_port"]) for d in sw1["Gi1/0/48"]["neighbors"]]
                == [("E3-IDF-SW2", "network", "GigabitEthernet1/0/24")])
    core = {p["port"]: p for p in by["CORE-1"]["ports"]}
    ok &= check("the core: closets by CDP and LLDP, the router, the wireless controller (gear, not an AP)",
                [(p, core[p]["neighbors"][0]["name"]) for p in ("Eth1/3", "Eth6/1", "Eth6/2")]
                == [("Eth1/3", "E2-IDF"), ("Eth6/1", "INET-RTR-1"), ("Eth6/2", "WLC-1")]
                and all(d["kind"] == "network" for p in core.values() for d in p["neighbors"])
                and core["Eth1/7"]["status"] == "xcvrAbsent", core["Eth6/2"])
    ok &= check("the switch that refused the login: no ports, its problem", not by["E4-IDF"]["ports"]
                and "login failed" in by["E4-IDF"]["problem"])
    ok &= check("location from SNMP when the list has none", by["M2-ARUBA"]["location"] == "M Building 2nd floor")
    rows = L.rows(by["E3-IDF-SW1"])
    devices = [r for r in rows if r.get("device")]
    ok &= check("a phone with a Windows PC behind it: both on the port",
                [(d["kind"], d["name"]) for d in sw1["Gi1/0/5"]["neighbors"]] ==
                [("phone", "SEP00BB000013F1"), ("pc", "E3-PC-105")], sw1["Gi1/0/5"]["neighbors"])
    ok &= check("CSV rows: one per port, plus one per extra device on a port", len(rows) == 105 and len(devices) == 68
                and devices[0]["kind"] == "Computer", (len(rows), len(devices)))
    return ok


def pretend_switches():
    outs = LD.demo_outputs()
    clis, servers, switch_list = {}, {}, []
    location_cmd = {"cisco_ios": "show running-config | include snmp-server location",
                    "cisco_nxos": "show running-config snmp", "aruba_mas": "show running-config | include location"}
    for switch, make in LD.SWITCHES:
        vendor = switch["type"]
        if make is None:
            servers[switch["name"]] = TelnetSwitch(MockCLI("E4-IDF", {}), "netops", "another-password")
        else:
            out = outs[switch["address"]]
            cmds = {L.COMMANDS[vendor][key][0]: out[key] for key in L.COMMANDS[vendor] if out.get(key)}
            if out.get("location") and vendor in location_cmd:
                cmds[location_cmd[vendor]] = ('syslocation "%s"' if vendor == "aruba_mas" else "snmp-server location %s") \
                    % out["location"]
            if vendor == "cisco_nxos":
                cli = MockCLI(out["prompt"], cmds)
                server = SSHSwitch(cli, "admin", "core-pass")
            elif vendor == "aruba_mas":
                cli = MockCLI(out["prompt"], cmds, start_enabled=False, enable_secret="en-secret", prompt_style="aruba")
                server = TelnetSwitch(cli, "netops", "sw-pass", user_prompt="User: ")
            elif vendor == "dell_powerconnect":
                cli = MockCLI(out["prompt"], cmds, start_enabled=False, enable_secret="en-secret")
                server = TelnetSwitch(cli, "netops", "sw-pass", user_prompt="User Name:")
            else:
                cli = MockCLI(out["prompt"], cmds, start_enabled=False, enable_secret="en-secret")
                server = TelnetSwitch(cli, "netops", "sw-pass")
            clis[out["prompt"]] = cli
            servers[out["prompt"]] = server
        key = switch["name"] or "E3-IDF-SW2"
        switch_list.append({**switch, "address": f"127.0.0.1:{servers[key].port}"})
    return clis, servers, switch_list


def full_run():
    ok = True
    print("\nReading neighbors through the web app")
    clis, servers, switch_list = pretend_switches()
    client = webapp.app.test_client()
    base = {"base_url": "http://127.0.0.1:5000"}
    client.post("/api/switches", json={"switches": switch_list}, **base)
    client.post("/api/settings", json={"default_protocol": "telnet", "parallel": 6, "timeout": 10}, **base)
    login = {"username": "netops", "password": "sw-pass", "secret": "en-secret", "ssh_different": True,
             "ssh_username": "admin", "ssh_password": "core-pass"}

    ok &= check("page loads, and the main page has a button for it", client.get("/lldp", **base).status_code == 200
                and b'href="/lldp"' in client.get("/", **base).data)
    ok &= check("the made-up building opens", client.get("/lldp/demo", **base).status_code == 200
                and b"made-up sample building" in client.get("/lldp/demo", **base).data)
    r = client.post("/api/lldp/run", json={"scope": "", "username": "netops"}, **base)
    ok &= check("no password -> asked for it", r.status_code == 400 and "password" in r.json["problems"][0].lower(), r.json)
    ok &= check("non-JSON posts refused", client.post("/api/lldp/run", data="{}", **base).status_code == 415)

    r = client.post("/api/lldp/run", json={"scope": "", **login}, **base)
    ok &= check("run starts", r.status_code == 200, r.json)
    started = time.time()
    while client.get("/api/lldp/job", **base).json["state"] == "running" and time.time() - started < 240:
        time.sleep(0.3)
    job = client.get("/api/lldp/job", **base).json
    log = [ln["msg"] for ln in job["lines"]]
    for m in log:
        print("    " + m)
    ok &= check("run finished", job["state"] == "done" and bool(job["report_file"]), log)
    ok &= check("the switch with the wrong password is reported",
                sum("Couldn't read" in m for m in log) == 1 and any("E4" in m or "login failed" in m for m in log), log)
    ok &= check("the log counts each switch's devices",
                any(m.startswith("E3-IDF-SW1 (") and "68 devices (48 phones, 4 access points, 3 switches or routers, "
                    "11 computers, 2 other devices)" in m for m in log), log)

    runs = client.get("/api/lldp/runs", **base).json["runs"]
    run = runs[0]
    ok &= check("run listed with its totals and the switch it couldn't read",
                run["run"] == job["report_file"] and run["switches"] == 5 and run["devices"] == 116
                and run["counts"] == {"phone": 82, "ap": 7, "network": 11, "pc": 14, "other": 2}
                and len(run["failed"]) == 1, run)
    page = client.get(f"/lldp/view/{run['run']}", **base)
    ok &= check("its page opens, with the CSV buttons", page.status_code == 200
                and f"/lldp/download/{run['run']}/devices".encode() in page.data and b"E3-IDF-SW1" in page.data)

    r = client.get(f"/lldp/download/{run['run']}/devices", **base)
    rows = list(csv.DictReader(io.StringIO(r.data.decode("utf-8-sig"))))
    ok &= check("Devices CSV: a BOM for Excel, the columns, a row per device",
                r.data.startswith(b"\xef\xbb\xbf") and list(rows[0]) == [c[0] for c in L.COLUMNS] and len(rows) == 116,
                (len(rows), list(rows[0]) if rows else None))
    ap = next(row for row in rows if row["Connected device"] == "AP-E3-112")
    ok &= check("an access point row", (ap["Switch"], ap["Port"], ap["Port description"], ap["Device type"],
                                        ap["Model / platform"], ap["Its port"], ap["Found by"]) ==
                ("E3-IDF-SW1", "Gi1/0/12", "AP E3112 ceiling", "Access point", "C9120AXI-B", "GigabitEthernet0",
                 "CDP + LLDP"), ap)
    r = client.get(f"/lldp/download/{run['run']}/ports", **base)
    every = list(csv.DictReader(io.StringIO(r.data.decode("utf-8-sig"))))
    ok &= check("Every port CSV: ports with nothing are there too", len(every) == 105 + 28 + 48 + 24 + 9
                and any(row["Status"] == "connected" and not row["Connected device"] for row in every)
                and any(row["Status"] == "notconnect" and not row["Connected device"] for row in every), len(every))
    pcs = [row for row in rows if row["Device type"] == "Computer"]
    ok &= check("Windows PCs in the Devices CSV, as Computer, with their MAC", len(pcs) == 14
                and pcs[0]["Device MAC"] and pcs[0]["Found by"] == "LLDP", pcs[:1])
    r = client.get(f"/lldp/download/{run['run']}/zip", **base)
    names = sorted(zipfile.ZipFile(io.BytesIO(r.data)).namelist()) if r.status_code == 200 else []
    ok &= check("one CSV per switch, named after it", names == ["CORE-1.csv", "E2-IDF.csv", "E3-IDF-SW1.csv",
                                                                "E3-IDF-SW2.csv", "M2-ARUBA.csv"], names)
    ok &= check("other runs and paths refused", client.get("/lldp/view/../../etc", **base).status_code == 404
                and client.get("/lldp/view/20990101-000000", **base).status_code == 404
                and client.get(f"/lldp/download/{run['run']}/data.json", **base).status_code == 404)
    saved = {s["address"]: (s["name"], s["location"]) for s in client.get("/api/state", **base).json["switches"]}
    by_name = {s["name"] or "E3-IDF-SW2": s["address"] for s in switch_list}
    ok &= check("an empty name and an empty location on the list filled in from the switches",
                saved[by_name["E3-IDF-SW2"]] == ("E3-IDF-SW2", "E3 IDF")
                and saved[by_name["M2-ARUBA"]] == ("M2-ARUBA", "M Building 2nd floor")
                and saved[by_name["E3-IDF-SW1"]] == ("E3-IDF-SW1", "E3 IDF"), saved)
    ok &= check("LLDP and CDP each read once per switch", clis["E3-IDF-SW1"].commands.count("show lldp neighbors detail") == 1
                and clis["E3-IDF-SW1"].commands.count("show cdp neighbors detail") == 1
                and "show lldp neighbor" in clis["M2-ARUBA"].commands)
    ok &= check("no passwords in what was saved", not any(b"sw-pass" in p.read_bytes() for p in
                                                          Path(webapp.LLDP_DIR).rglob("*") if p.is_file()))
    return ok


def main():
    ok = reading()
    ok &= demo()
    ok &= full_run()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
