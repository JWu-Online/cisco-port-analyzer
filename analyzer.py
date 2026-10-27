"""
analyzer.py - find switch ports that haven't been used in a while.

How each switch type tells us when a port was last used:

  Cisco Catalyst (IOS)   "show interfaces": Last input / Last output. A port that's
                         down stops sending, so the newer of the two is when it was
                         last in use.
  Cisco Nexus (NX-OS)    "show interface": Last link flapped (when a down port went down).
  Aruba S3500 (ArubaOS)  "show interface": link status last changed.
  Dell 5548P             No timestamp at all. A down port with zero packets since the
                         switch started has not been used since then.

"Never" only reaches back to when the switch last restarted (or its counters were
cleared), so a port is only called Inactive when that reaches back far enough.
Otherwise it's Unsure, with the reason.

Run it from the command line too:
    python analyzer.py 10.99.4.11 -u admin -t cisco_ios -p telnet -m 6
    python analyzer.py --list data/switches.csv -u admin -m 6
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import switches as sw

DAYS_PER_MONTH = 30.44
RESULT_ORDER = ["INACTIVE", "UNSURE", "ACTIVE"]
RESULT_LABEL = {"INACTIVE": "Inactive", "UNSURE": "Unsure", "ACTIVE": "Active"}


# ---------------------------------------------------------------------------
# Durations: "1y2w", "2w3d", "1d02h", "00:12:44", "12week(s) 3day(s)",
# "1 year, 23 weeks, 4 days", "0 day 0 hr 2 min 32 sec", "105,03:24:11"
# ---------------------------------------------------------------------------
UNIT_DAYS = [("mo", DAYS_PER_MONTH), ("y", 365.25), ("w", 7.0), ("d", 1.0), ("h", 1 / 24),
             ("m", 1 / 1440), ("s", 1 / 86400)]


def parse_duration(text):
    """Days as a float, or None for "never" / nothing recognisable."""
    t = (text or "").strip().lower()
    if not t or t.startswith("never") or t in ("-", "n/a", "unknown"):
        return None
    m = re.fullmatch(r"(\d+)\s*,\s*(\d+):(\d+):(\d+)", t)            # Dell: days,hh:mm:ss
    if m:
        d, hh, mm, ss = map(int, m.groups())
        return d + hh / 24 + mm / 1440 + ss / 86400
    days, found = 0.0, False
    clock = re.search(r"\b(\d+):(\d{2}):(\d{2})\b", t)                # hh:mm:ss
    if clock:
        hh, mm, ss = map(int, clock.groups())
        days += hh / 24 + mm / 1440 + ss / 86400
        found = True
        t = t.replace(clock.group(0), " ")
    for num, unit in re.findall(r"(\d+)\s*([a-z]+)", t):
        for prefix, size in UNIT_DAYS:
            if unit.startswith(prefix) and not unit.startswith("ms"):
                days += int(num) * size
                found = True
                break
    return days if found else None


def describe_days(days):
    if days is None:
        return "unknown"
    if days < 1:
        return "today"
    if days < 14:
        n = int(days)
        return f"{n} day{'s' if n != 1 else ''}"
    if days < 61:
        return f"{int(days // 7)} weeks"
    if days < 730:
        return f"{int(days // DAYS_PER_MONTH)} months"
    return f"{days / 365.25:.1f} years"


# ---------------------------------------------------------------------------
# Port names
# ---------------------------------------------------------------------------
SHORT_NAMES = [("TwentyFiveGigE", "Twe"), ("TwoGigabitEthernet", "Tw"), ("TenGigabitEthernet", "Te"),
               ("FortyGigabitEthernet", "Fo"), ("HundredGigE", "Hu"), ("GigabitEthernet", "Gi"),
               ("FastEthernet", "Fa"), ("Ethernet", "Eth")]
SKIP_PORTS = re.compile(r"(?i)^(vlan|loopback|port-channel|po\d|tunnel|null|mgmt|appgigabit|bluetooth|"
                        r"async|embedded|group-async|nve|stack|cpu|ch\d|lag)")


def short_port(name):
    for long, short in SHORT_NAMES:
        if name.startswith(long):
            return short + name[len(long):]
    return name


def port_key(name):
    """Sort Gi1/0/2 before Gi1/0/10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def match_key(name):
    """Gi1/0/1, GigabitEthernet1/0/1 and gi1/0/1 all give ('gi', '1/0/1')."""
    m = re.match(r"([A-Za-z]+)[\s-]?([\d/.:]+)$", short_port(name.strip()))
    return (m.group(1)[:2].lower(), m.group(2)) if m else (name.lower(), "")


def uptime_from(text):
    """Switch uptime in days from 'show version' / 'show system' output."""
    m = re.search(r"(?i)up\s*time\s*\(days,hour:min:sec\)\s*:\s*([\d,:]+)", text or "")   # Dell
    if m:
        return parse_duration(m.group(1))
    m = re.search(r"(?i)uptime is (.+)", text or "")
    if m:
        return parse_duration(m.group(1))
    return None


def column_table(output, columns):
    """
    Read a fixed-width table (like 'show interfaces status') using the header row's
    column positions. Returns {first column value: {column: value}}.
    """
    lines = (output or "").splitlines()
    header_i = next((i for i, l in enumerate(lines)
                     if all(re.search(rf"(?i)\b{c}\b", l) for c in columns)), None)
    if header_i is None:
        return {}
    header = lines[header_i]
    starts = []
    for c in columns:
        m = re.search(rf"(?i)\b{c}\b", header)
        starts.append((c, m.start()))
    starts.sort(key=lambda x: x[1])
    rows = {}
    for line in lines[header_i + 1:]:
        if not line.strip() or set(line.strip()) <= set("- "):
            continue
        values = {}
        for n, (c, start) in enumerate(starts):
            end = starts[n + 1][1] if n + 1 < len(starts) else None
            values[c.lower()] = line[start:end].strip() if len(line) > start else ""
        first = line.split()[0] if line.split() else ""
        if first:
            rows[first] = values
    return rows


def expand_ports(text):
    """'gi1/0/1-4,te1/0/1,Po1-2' -> ['gi1/0/1', ..., 'gi1/0/4', 'te1/0/1', 'Po1', 'Po2']."""
    ports = []
    for part in re.split(r"[,\s]+", text or ""):
        m = re.fullmatch(r"([A-Za-z]+[\d/]*?)(\d+)-(\d+)", part)
        if m and int(m.group(3)) >= int(m.group(2)) and int(m.group(3)) - int(m.group(2)) < 200:
            ports += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        elif re.fullmatch(r"[A-Za-z]+[\d/]+", part or ""):
            ports.append(part)
    return ports


def vlans_from_vlan_table(output):
    """Best effort for 'show vlan' on Dell / ArubaOS: port -> VLAN (or 'trunk' if in several)."""
    seen = {}
    current = None
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d{1,4})\s+(.*)$", line)
        if m and int(m.group(1)) <= 4094:
            current = m.group(1)
            rest = m.group(2)
        elif current and line.startswith(" " * 6):
            rest = line                          # continuation of the ports list
        else:
            current = None if not line.strip() else current
            continue
        for p in expand_ports(" ".join(re.findall(r"[A-Za-z]{2,}[\d/]+(?:-\d+)?", rest))):
            seen.setdefault(match_key(p), set()).add(current)
    return {k: (next(iter(v)) if len(v) == 1 else "trunk") for k, v in seen.items()}


def new_port(name):
    return {"port": short_port(name), "description": "", "status": "down", "vlan": "",
            "last_used_days": None, "never": False, "window_days": None, "how": ""}


# ---------------------------------------------------------------------------
# Readers, one per switch type. Each returns (ports, uptime_days, raw_outputs).
# ---------------------------------------------------------------------------
def read_cisco_ios(conn):
    raw = {}
    _, raw["show version"] = sw.run_first_working(conn, ["show version"])
    _, raw["show interfaces"] = sw.run_first_working(conn, ["show interfaces"], read_timeout=300)
    _, raw["show interfaces status"] = sw.run_first_working(conn, ["show interfaces status"])
    uptime = uptime_from(raw["show version"])
    ports = parse_ios_interfaces(raw["show interfaces"], uptime)
    status = column_table(raw["show interfaces status"], ["Port", "Status", "Vlan", "Duplex"])
    vlans = {match_key(k): v["vlan"] for k, v in status.items()}
    for p in ports:
        p["vlan"] = vlans.get(match_key(p["port"]), "")
    return ports, uptime, raw


IOS_HEAD = re.compile(r"^(\S+) is (up|down|administratively down), line protocol is (\S+)")


def parse_ios_interfaces(output, uptime):
    ports, cur = [], None
    for line in (output or "").splitlines():
        m = IOS_HEAD.match(line)
        if m:
            name = m.group(1)
            cur = None
            if SKIP_PORTS.match(name) or "/" not in name or name in ("GigabitEthernet0/0",):
                continue
            cur = new_port(name)
            admin, proto = m.group(2), m.group(3).rstrip(",")
            cur["status"] = "disabled" if admin == "administratively down" else (
                "up" if admin == "up" and proto.startswith("up") else "down")
            cur["_in"] = cur["_out"] = None
            ports.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Description:"):
            cur["description"] = s.split(":", 1)[1].strip()
        elif s.startswith("Last input"):
            m = re.match(r"Last input ([^,]+), output ([^,]+)", s)
            if m:
                cur["_in"], cur["_out"] = m.group(1).strip(), m.group(2).strip()
        elif s.startswith("Last clearing of"):
            m = re.search(r"counters (.+)$", s)
            cur["_clear"] = parse_duration(m.group(1)) if m else None
    for p in ports:
        i, o = p.pop("_in"), p.pop("_out")
        clear = p.pop("_clear", None)
        known = [d for d in (parse_duration(i), parse_duration(o)) if d is not None]
        if known:
            p["last_used_days"] = min(known)
        elif i is not None and (i or "").lower().startswith("never") and (o or "").lower().startswith("never"):
            p["never"] = True
            p["window_days"] = clear if clear is not None else uptime
        p["how"] = f"Last input {i}, output {o}" if i is not None else ""
    return ports


def read_cisco_nxos(conn):
    raw = {}
    _, raw["show version"] = sw.run_first_working(conn, ["show version"])
    _, raw["show interface"] = sw.run_first_working(conn, ["show interface"], read_timeout=300)
    _, raw["show interface status"] = sw.run_first_working(conn, ["show interface status"])
    uptime = uptime_from(raw["show version"])
    ports = parse_nxos_interfaces(raw["show interface"], uptime)
    status = column_table(raw["show interface status"], ["Port", "Status", "Vlan", "Duplex"])
    vlans = {match_key(k): v["vlan"] for k, v in status.items()}
    for p in ports:
        p["vlan"] = vlans.get(match_key(p["port"]), "")
    return ports, uptime, raw


NXOS_HEAD = re.compile(r"^(Ethernet\S+) is (up|down)(?:\s*\((.+?)\))?")


def parse_nxos_interfaces(output, uptime):
    ports, cur = [], None
    for line in (output or "").splitlines():
        m = NXOS_HEAD.match(line)
        if m:
            cur = new_port(m.group(1))
            reason = (m.group(3) or "").lower()
            cur["status"] = "up" if m.group(2) == "up" else (
                "disabled" if "administratively" in reason else "down")
            ports.append(cur)
            continue
        if not line.startswith(" ") and line.strip() and not line.lower().startswith("admin state"):
            cur = None                      # another kind of interface (mgmt0, Vlan, port-channel)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.lower().startswith("admin state is down"):
            cur["status"] = "disabled"
        elif s.startswith("Description:"):
            cur["description"] = s.split(":", 1)[1].strip()
        elif s.startswith("Last link flapped"):
            when = s[len("Last link flapped"):].strip()
            cur["how"] = f"Last link flapped {when}"
            d = parse_duration(when)
            if d is not None:
                cur["last_used_days"] = d
            elif when.lower().startswith("never"):
                cur["never"] = True
                cur["window_days"] = uptime
    return ports


def read_dell_powerconnect(conn):
    raw = {}
    _, raw["show system"] = sw.run_first_working(conn, ["show system", "show version"])
    _, raw["show interfaces status"] = sw.run_first_working(conn, ["show interfaces status"])
    _, raw["show interfaces description"] = sw.run_first_working(conn, ["show interfaces description"])
    _, raw["show interfaces counters"] = sw.run_first_working(conn, ["show interfaces counters"], read_timeout=240)
    _, raw["show vlan"] = sw.run_first_working(conn, ["show vlan"])
    uptime = uptime_from(raw["show system"])
    ports = parse_dell(raw, uptime)
    return ports, uptime, raw


DELL_PORT = re.compile(r"^\s*((?:gi|te|fa|xg)\d[\d/]*)\s", re.I)


def parse_dell(raw, uptime):
    ports, by_key = [], {}
    for line in raw.get("show interfaces status", "").splitlines():
        m = DELL_PORT.match(line)
        if not m:
            continue
        tokens = line.split()
        state = next((t for t in tokens[1:] if t.lower() in ("up", "down")), None)
        if state is None:
            continue
        p = new_port(m.group(1))
        p["status"] = "up" if state.lower() == "up" else "down"
        ports.append(p)
        by_key[match_key(p["port"])] = p
    for line in raw.get("show interfaces description", "").splitlines():
        m = DELL_PORT.match(line)
        if m and match_key(m.group(1)) in by_key:
            by_key[match_key(m.group(1))]["description"] = line[m.end():].strip()
    totals = {}
    for line in raw.get("show interfaces counters", "").splitlines():
        m = DELL_PORT.match(line)
        if not m:
            continue
        numbers = [int(t) for t in line.split()[1:] if t.isdigit()]
        if numbers:
            key = match_key(m.group(1))
            totals[key] = totals.get(key, 0) + sum(numbers)
    vlans = vlans_from_vlan_table(raw.get("show vlan", ""))
    for p in ports:
        key = match_key(p["port"])
        p["vlan"] = vlans.get(key, "")
        total = totals.get(key)
        if p["status"] == "up":
            p["how"] = "Link is up"
        elif total == 0:
            p["never"] = True
            p["window_days"] = uptime
            p["how"] = "Down, 0 packets since the switch started"
        elif total is not None:
            p["how"] = f"Down, {total:,} packets since the switch started, no timestamp"
        else:
            p["how"] = "Down, no counters read"
    return ports


def read_aruba_mas(conn):
    raw = {}
    _, raw["show version"] = sw.run_first_working(conn, ["show version"])
    cmd, out = sw.run_first_working(conn, ["show interface gigabitethernet", "show interface"],
                                    read_timeout=300)
    raw[cmd or "show interface"] = out
    _, raw["show vlan"] = sw.run_first_working(conn, ["show vlan"])
    uptime = uptime_from(raw["show version"])
    ports = parse_aruba_interfaces(out, uptime)
    vlans = vlans_from_vlan_table(raw["show vlan"])
    for p in ports:
        p["vlan"] = vlans.get(match_key(p["port"]), "")
    return ports, uptime, raw


ARUBA_HEAD = re.compile(r"(?i)^(GE\d+/\d+/\d+) is (administratively )?(up|down)\b.*?link is (up|down)")


def parse_aruba_interfaces(output, uptime):
    ports, cur = [], None
    for line in (output or "").splitlines():
        m = ARUBA_HEAD.match(line.strip())
        if m:
            cur = new_port(m.group(1).upper())
            admin, link = m.group(3).lower(), m.group(4).lower()
            cur["status"] = "disabled" if admin == "down" else ("up" if link == "up" else "down")
            cur.update(_changed=None, _clear=None, _pkts=0, _got_pkts=False)
            ports.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        low = s.lower()
        if low.startswith("description:"):
            desc = s.split(":", 1)[1].strip()
            cur["description"] = "" if desc.upper() == cur["port"].upper() else desc   # default = port name
        elif low.startswith("link status last changed"):
            cur["_changed"] = s[len("link status last changed"):].strip()
        elif low.startswith("last clearing of counters"):
            cur["_clear"] = parse_duration(s[len("last clearing of counters"):])
        else:
            for n in re.findall(r"(\d+) packets (?:input|output)", low):
                cur["_pkts"] += int(n)
                cur["_got_pkts"] = True
    for p in ports:
        changed, clear, pkts, got = p.pop("_changed"), p.pop("_clear"), p.pop("_pkts"), p.pop("_got_pkts")
        if p["status"] == "up":
            p["how"] = "Link is up"
            continue
        d = parse_duration(changed) if changed else None
        if d is not None and not (d < 1 / 86400 and got and pkts == 0):
            p["last_used_days"] = d
            p["how"] = f"Link status last changed {changed}"
        elif got and pkts == 0:
            p["never"] = True
            p["window_days"] = clear if clear else uptime
            p["how"] = "Down, 0 packets since the counters started"
        else:
            p["how"] = "Down, no link-change time"
    return ports


READERS = {
    "cisco_ios": read_cisco_ios,
    "cisco_nxos": read_cisco_nxos,
    "dell_powerconnect": read_dell_powerconnect,
    "aruba_mas": read_aruba_mas,
}


# ---------------------------------------------------------------------------
# Deciding: Inactive, Unsure or Active
# ---------------------------------------------------------------------------
def classify(p, threshold_days, months):
    """Adds result, last_used (text), days_unused (number or blank) and why."""
    window = p["window_days"]
    if p["status"] == "up":
        p.update(result="ACTIVE", last_used="Now", days_unused=0, why="Link is up")
    elif p["last_used_days"] is not None:
        d = p["last_used_days"]
        p["days_unused"] = round(d, 1)
        p["last_used"] = describe_days(d) + " ago" if d >= 1 else "Today"
        if d >= threshold_days:
            p.update(result="INACTIVE", why=f"Unused for {describe_days(d)}")
        else:
            p.update(result="ACTIVE", why=f"Used {describe_days(d)} ago" if d >= 1 else "Used today")
    elif p["never"]:
        p["days_unused"] = round(window, 1) if window is not None else ""
        if window is None:
            p.update(result="UNSURE", last_used="Not since the switch started",
                     why="Never used since the switch started, but its uptime couldn't be read")
        elif window >= threshold_days:
            p.update(result="INACTIVE", last_used=f"Never in {describe_days(window)}",
                     why=f"Never used in the {describe_days(window)} the switch has been running")
        else:
            p.update(result="UNSURE", last_used=f"Never in {describe_days(window)}",
                     why=f"Never used, but the switch restarted only {describe_days(window)} ago, "
                         f"less than {months:g} months")
    else:
        p.update(result="UNSURE", last_used="Unknown", days_unused="",
                 why="Down, but this switch doesn't record when it was last used")
    return p


# ---------------------------------------------------------------------------
# Checking switches
# ---------------------------------------------------------------------------
def check_switch(switch, login, months, timeout=20, debug_dir=None):
    """Log into one switch, read and classify its ports. Never raises."""
    threshold = months * DAYS_PER_MONTH
    info = {"name": switch["address"], "address": switch["address"], "type": switch["type"],
            "protocol": switch["protocol"], "building": switch.get("building", ""),
            "location": switch.get("location", ""), "uptime_days": None, "ports": [],
            "problem": "", "debug_file": ""}
    raw = {}
    try:
        with sw.connect(switch["address"], switch["type"], switch["protocol"], login, timeout) as conn:
            info["name"] = sw.prompt_name(conn) or switch["address"]
            ports, uptime, raw = READERS[switch["type"]](conn)
    except Exception as exc:
        info["problem"] = sw.friendly_error(exc)
        return info
    info["uptime_days"] = round(uptime, 1) if uptime is not None else None
    for p in sorted(ports, key=lambda p: port_key(p["port"])):
        info["ports"].append(classify(p, threshold, months))
    worry = []
    if not ports:
        worry.append("no ports could be read")
    if uptime is None:
        worry.append("its uptime couldn't be read")
    if worry:
        info["problem"] = "Read the switch, but " + " and ".join(worry) + ". Is the type right?"
        if debug_dir:
            info["debug_file"] = save_raw(debug_dir, switch, raw)
    return info


def save_raw(debug_dir, switch, raw):
    """Keep what the switch printed, so a parsing problem can be checked later."""
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", switch["address"])
    path = debug_dir / f"{safe}-{switch['type']}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for cmd, out in raw.items():
            f.write(f"===== {cmd} =====\n{out}\n\n")
    return str(path)


def summarize(info):
    counts = Counter(p["result"] for p in info["ports"])
    return {k: info[k] for k in ("name", "address", "type", "protocol", "building", "location",
                                 "uptime_days", "problem", "debug_file")} | {
        "ports": len(info["ports"]), "inactive": counts["INACTIVE"],
        "unsure": counts["UNSURE"], "active": counts["ACTIVE"]}


PORT_FIELDS = ["switch", "address", "building", "location", "port", "description", "status", "vlan",
               "result", "last_used", "days_unused", "why", "how"]


def flatten(infos):
    rows = []
    for info in infos:
        for p in info["ports"]:
            rows.append({"switch": info["name"], "address": info["address"],
                         "building": info["building"], "location": info["location"],
                         **{k: p.get(k, "") for k in PORT_FIELDS if k not in
                            ("switch", "address", "building", "location")}})
    return rows


class Job:
    def __init__(self):
        self.state = "idle"
        self.lines = []
        self.progress = {"done": 0, "total": 0}
        self.report_file = None
        self.stop_requested = threading.Event()
        self._lock = threading.Lock()

    def log(self, msg, level="info"):
        with self._lock:
            self.lines.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})

    def snapshot(self, since=0):
        with self._lock:
            return {"state": self.state, "lines": self.lines[since:], "next": len(self.lines),
                    "progress": dict(self.progress),
                    "report_file": self.report_file.name if self.report_file else None}


def run_job(job, switches, logins, months, settings, reports_dir, debug_dir, scope_label):
    """Check every switch in `switches`, then save a report. Updates `job` as it goes."""
    job.state = "running"
    try:
        job.progress = {"done": 0, "total": len(switches)}
        job.log(f"Checking {scope_label} for ports unused for {months:g}+ months...")
        infos = []
        with ThreadPoolExecutor(max_workers=int(settings.get("parallel", 10))) as pool:
            futures = {}
            for s in switches:
                if job.stop_requested.is_set():
                    break
                futures[pool.submit(_check_unless_stopped, job, s, logins[s["protocol"]], months,
                                    int(settings.get("timeout", 20)), debug_dir)] = s
            for fut in as_completed(futures):
                info = fut.result()
                job.progress["done"] += 1
                if info is None:
                    continue
                infos.append(info)
                c = Counter(p["result"] for p in info["ports"])
                where = info["address"] if info["name"] == info["address"] else f"{info['name']} ({info['address']})"
                if info["problem"] and not info["ports"]:
                    job.log(f"Couldn't check {where}: {info['problem']}", "error")
                else:
                    msg = (f"{where}: {len(info['ports'])} ports, {c['INACTIVE']} inactive"
                           + (f", {c['UNSURE']} unsure" if c["UNSURE"] else ""))
                    if info["problem"]:
                        msg += f". {info['problem']}"
                        if info["debug_file"]:
                            msg += f" What it printed is saved in {info['debug_file']}"
                    job.log(msg, "warn" if info["problem"] else "info")
        if job.stop_requested.is_set():
            job.state = "stopped"
            job.log("Stopped. No report was saved.", "warn")
            return
        infos.sort(key=lambda i: (i["building"], i["location"], port_key(i["address"])))
        report = {
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "months": months, "scope": scope_label,
            "switches": [summarize(i) for i in infos],
            "ports": flatten(infos),
        }
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        job.report_file = path
        total = Counter(r["result"] for r in report["ports"])
        failed = sum(1 for s in report["switches"] if s["problem"] and not s["ports"])
        job.log(f"Done. {total['INACTIVE']} inactive, {total['UNSURE']} unsure and {total['ACTIVE']} active "
                f"ports on {len(infos) - failed} switches"
                + (f". {failed} switch(es) couldn't be checked." if failed else "."), "done")
        job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.log(f"The run stopped with an error: {sw.friendly_error(exc)}", "error")


def _check_unless_stopped(job, switch, login, months, timeout, debug_dir):
    if job.stop_requested.is_set():
        return None
    return check_switch(switch, login, months, timeout, debug_dir)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main():
    import report
    ap = argparse.ArgumentParser(description="Find switch ports unused for a number of months.")
    ap.add_argument("host", nargs="?", help="Switch IP or hostname (or use --list)")
    ap.add_argument("--list", help="CSV switch list: address,type,protocol,building,location")
    ap.add_argument("-u", "--username", required=True)
    ap.add_argument("-t", "--type", default="cisco_ios", help="Switch type: " + ", ".join(sw.TYPES))
    ap.add_argument("-p", "--protocol", choices=["telnet", "ssh"], default="telnet")
    ap.add_argument("-m", "--months", type=float, default=6)
    ap.add_argument("-q", "--query", choices=["inactive", "active", "all"], default="inactive",
                    help="Which ports go in the spreadsheet (default: inactive)")
    ap.add_argument("-o", "--output", help="Excel file name")
    args = ap.parse_args()

    if args.list:
        with open(args.list, newline="", encoding="utf-8-sig") as f:
            targets = [{"address": r["address"].strip(), "type": sw.clean_type(r.get("type")) or "cisco_ios",
                        "protocol": sw.clean_protocol(r.get("protocol")) or "telnet",
                        "building": r.get("building", ""), "location": r.get("location", "")}
                       for r in csv.DictReader(f) if r.get("address", "").strip()]
    elif args.host:
        vtype = sw.clean_type(args.type)
        if not vtype:
            ap.error(f"unknown type {args.type!r}")
        targets = [{"address": args.host, "type": vtype, "protocol": args.protocol,
                    "building": "", "location": ""}]
    else:
        ap.error("give a switch address or --list")

    password = getpass.getpass("Password: ")
    secret = getpass.getpass("Enable password (Enter if not needed): ")
    login = sw.Login(args.username, password, secret)
    infos = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for info in pool.map(lambda s: check_switch(s, login, args.months), targets):
            c = Counter(p["result"] for p in info["ports"])
            print(f"{info['name']}: " + (info["problem"] if not info["ports"] else
                                          f"{len(info['ports'])} ports, {c['INACTIVE']} inactive, "
                                          f"{c['UNSURE']} unsure"))
            infos.append(info)
    rows = flatten(infos)
    if args.query != "all":
        rows = [r for r in rows if r["result"] == args.query.upper()]
    out = args.output or f"{args.query}-ports-{args.months:g}mo.xlsx"
    with open(out, "wb") as f:
        f.write(report.excel_bytes([summarize(i) for i in infos], rows, args.months))
    print(f"Saved {len(rows)} ports to {out}")


if __name__ == "__main__":
    main()
