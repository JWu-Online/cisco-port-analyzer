"""
topology.py - draw a Cisco switch stack: every member, ports 1-48, VLANs and use.

Each stack member is drawn as a front panel. Every port shows its VLAN and is
colored by whether it was used within the months you pick (6 by default), the
same Active / Inactive / Unsure answer as the rest of the app. The page also
shows the stack cables between members and warns when the stack ring is broken.

How a port counts as Active:
  1. Its link is up right now.
  2. 'show interfaces link' says the link went down less than N months ago
     (a port that has been down since the switch booted doesn't count).
  3. 'show interfaces' Last input / Last output is less than N months ago.
  4. An earlier run of this tool saw it up (kept in data/topology/history).
If none of those, the port is Inactive only when there's N months of evidence
(the member's uptime plus earlier runs). Otherwise it's Unsure.

Run it from the web app (Stack topology) or the command line:
    python topology.py 10.99.4.11 -u admin -p telnet -m 6
    python topology.py --demo
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import analyzer
import switches as sw

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
DEFAULT_DIR = Path(os.environ.get("PA_DATA_DIR", APP_DIR / "data")) / "topology"

DAYS_PER_MONTH = analyzer.DAYS_PER_MONTH
FIRST_PORT, LAST_PORT = 1, 48           # only front-panel ports 1-48 are drawn
EXCLUDED_TYPES = {"Ap"}                  # AppGigabitEthernet (app-hosting port on Catalyst 9k)
GAP_TOLERANCE = timedelta(days=3)        # runs at least every 3 days carry history across a reboot

COMMANDS = {
    "switch": "show switch",
    "neighbors": "show switch neighbors",
    "stack_ports": "show switch stack-ports",
    "version": "show version",
    "status": "show interfaces status",
    "vlans": "show vlan brief",
    # Only the interface sections if the switch can filter; older IOS sends the whole config
    "config": ("show running-config | section ^interface", "show running-config"),
    "link": "show interfaces link",
    "interfaces": "show interfaces",
}
LABEL = {"active": "Active", "inactive": "Inactive", "unsure": "Unsure"}


# ---------------------------------------------------------------------------
# Port names
# ---------------------------------------------------------------------------
_TYPE_SHORT = {
    "gigabitethernet": "Gi", "gi": "Gi", "fastethernet": "Fa", "fa": "Fa",
    "tengigabitethernet": "Te", "te": "Te", "twogigabitethernet": "Tw", "tw": "Tw",
    "fivegigabitethernet": "Fi", "fi": "Fi",
    "twentyfivegige": "Twe", "twentyfivegigabitethernet": "Twe", "twe": "Twe",
    "fortygigabitethernet": "Fo", "fo": "Fo", "hundredgige": "Hu", "hu": "Hu",
    "appgigabitethernet": "Ap", "ap": "Ap",
}
_NAME_RE = re.compile(r"^([A-Za-z-]+)\s*(\d+(?:/\d+){1,2})$")


def normalize_intf(name):
    """'GigabitEthernet1/0/5' and 'Gi1/0/5' both become 'Gi1/0/5'."""
    m = _NAME_RE.match(name.strip())
    if not m:
        return name.strip()
    prefix, nums = m.groups()
    return _TYPE_SHORT.get(prefix.lower(), prefix[:2].capitalize()) + nums


def port_position(short_name):
    """(type, member, slot, port) or None.  Gi2/0/14 -> ('Gi', 2, 0, 14); Fa0/7 -> ('Fa', 1, 0, 7)."""
    m = _NAME_RE.match(short_name)
    if not m:
        return None
    prefix, nums = m.groups()
    parts = [int(n) for n in nums.split("/")]
    if len(parts) == 3:
        member, slot, port = parts
    else:
        member, (slot, port) = 1, parts       # standalone switch: Fa0/7 is member 1
    return prefix, member, slot, port


# ---------------------------------------------------------------------------
# Durations. Cisco prints "00:12:44", "3d04h", "26w3d", "1y2w" or "never".
# ---------------------------------------------------------------------------
_UNIT = {"y": timedelta(days=365), "w": timedelta(weeks=1), "d": timedelta(days=1),
         "h": timedelta(hours=1), "m": timedelta(minutes=1)}
_DURATION_TOKEN = re.compile(r"^(\d+:\d{2}:\d{2}|(?:\d+[ywdhm])+)$")


def parse_cisco_duration(text):
    """(timedelta, granularity) or (None, None).

    Granularity is the smallest unit shown: "26w3d" really means "26w3d to 26w4d",
    so it's one day. We need it to tell "down since the switch booted" apart from
    "went down later".
    """
    text = (text or "").strip()
    if not _DURATION_TOKEN.match(text):
        return None, None
    if ":" in text:
        h, m, s = (int(x) for x in text.split(":"))
        return timedelta(hours=h, minutes=m, seconds=s), timedelta(seconds=1)
    total, smallest = timedelta(), None
    for num, unit in re.findall(r"(\d+)([ywdhm])", text):
        total += int(num) * _UNIT[unit]
        smallest = _UNIT[unit]
    return total, smallest


def human_span(td):
    return analyzer.describe_days(td.total_seconds() / 86400)


# ---------------------------------------------------------------------------
# Readers, one per command
# ---------------------------------------------------------------------------
_SWITCH_ROW = re.compile(
    r"^\s*(\*)?\s*(\d+)\s+(Active|Standby|Member|Master)\s+"
    r"([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+(\d+)\s+(\S+)\s+(.+?)\s*$")


def parse_show_switch(output):
    """'show switch' -> {member: {role, mac, priority, state}}."""
    members = {}
    for line in (output or "").splitlines():
        m = _SWITCH_ROW.match(line)
        if m:
            members[int(m.group(2))] = {
                "role": "Active" if m.group(3) == "Master" else m.group(3),
                "mac": m.group(4), "priority": int(m.group(5)), "state": m.group(7).strip(),
            }
    return members


def parse_two_port_table(output):
    """'show switch neighbors' / 'show switch stack-ports' -> {member: (port 1, port 2)}."""
    rows = {}
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s*$", line)
        if m:
            rows[int(m.group(1))] = (m.group(2), m.group(3))
    return rows


def parse_version(output):
    """'show version' -> hostname, stack uptime, and each member's model and uptime."""
    output = output or ""
    info = {"hostname": "", "uptime": None, "uptime_text": "", "members": {}}
    m = re.search(r"^(\S+) uptime is (.+?)\s*$", output, re.M)
    if m:
        info["hostname"], info["uptime_text"] = m.group(1), m.group(2)
        days = analyzer.parse_duration(m.group(2))
        info["uptime"] = timedelta(days=days) if days is not None else None

    # The table near the bottom:  "*    1 56    C9300-48P   17.06.05   CAT9K_IOSXE   INSTALL"
    in_table = False
    for line in output.splitlines():
        if re.match(r"^\s*Switch\s+Ports\s+Model", line):
            in_table = True
            continue
        if in_table:
            row = re.match(r"^\s*\*?\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)", line)
            if row:
                info["members"].setdefault(int(row.group(1)), {})["model"] = row.group(3)
            elif not line.strip() and info["members"]:
                in_table = False

    # A block for each other member:  "Switch 02" / "---------" / "Switch uptime : 3 weeks..."
    blocks = re.split(r"^Switch 0*(\d+)\s*\n\s*-{4,}\s*$", output, flags=re.M)
    for i in range(1, len(blocks) - 1, 2):
        entry = info["members"].setdefault(int(blocks[i]), {})
        up = re.search(r"Switch [Uu]ptime\s*:\s*(.+)", blocks[i + 1])
        if up:
            days = analyzer.parse_duration(up.group(1))
            entry["uptime_text"] = up.group(1).strip()
            entry["uptime"] = timedelta(days=days) if days is not None else None
        model = re.search(r"Model [Nn]umber\s*:\s*(\S+)", blocks[i + 1])
        if model:
            entry.setdefault("model", model.group(1))
    return info


_STATUS_WORDS = {"connected", "notconnect", "disabled", "err-disabled", "err-disable", "inactive",
                 "suspended", "xcvr-inval", "monitoring", "faulty", "sfpAbsent", "notpresent", "down"}


def parse_status(output):
    """'show interfaces status' -> {port: {desc, status, vlan, duplex, speed, type}}.

    The Name column can contain spaces, so find the status word from the right
    and read the columns after it.
    """
    ports = {}
    for line in (output or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or not _NAME_RE.match(parts[0]):
            continue
        for i in range(len(parts) - 1, 0, -1):
            if parts[i] in _STATUS_WORDS:
                break
        else:
            continue
        rest = parts[i + 1:]
        ports[normalize_intf(parts[0])] = {
            "desc": " ".join(parts[1:i]), "status": parts[i],
            "vlan": rest[0] if rest else "", "duplex": rest[1] if len(rest) > 1 else "",
            "speed": rest[2] if len(rest) > 2 else "", "type": " ".join(rest[3:]),
        }
    return ports


_VLAN_ROW = re.compile(r"^(\d{1,4})\s+(.+?)\s+(active|suspended|act/lshut|sus/lshut|act/ishut|sus/ishut|act/unsup)\b")


def parse_vlan_names(output):
    """'show vlan brief' -> {"20": "STAFF-DATA", ...}.

    VLANs nobody named (IOS calls them VLAN0020) are left out, since the name
    would just repeat the number.
    """
    names = {}
    for line in (output or "").splitlines():
        m = _VLAN_ROW.match(line)
        if m and not re.fullmatch(r"VLAN0*" + m.group(1), m.group(2)):
            names[m.group(1)] = m.group(2)
    return names


_SECRET_WORDS = re.compile(r"\b(password|secret|key|key-string|authentication-key|message-digest-key|community)\b", re.I)


def parse_interface_configs(output):
    """'show running-config' -> {port: {"ifname", "lines", "removed"}}.

    Only interface sections are kept; the rest of the config (users, SNMP,
    TACACS keys) is ignored. A port line that carries a key or password (rare on
    access ports) is swapped for a '!' comment, so saved pages never hold secrets.
    """
    configs, current = {}, None
    for raw in (output or "").splitlines():
        line = raw.rstrip()
        if line.startswith("interface "):
            ifname = line.split(None, 1)[1].strip()
            current = {"ifname": ifname, "lines": [], "removed": 0}
            configs[normalize_intf(ifname)] = current
        elif current is not None and line.startswith(" "):
            body = line.strip()
            secret = None if body.startswith("description ") else _SECRET_WORDS.search(body)
            if secret:
                current["lines"].append(f" ! left out: a line with a key or password ({body[:secret.end()]} ...)")
                current["removed"] += 1
            else:
                current["lines"].append(line)
        else:
            current = None          # "!" or the next section ends the interface
    return configs


def parse_link(output):
    """'show interfaces link' -> {port: down time text}.

    Header:  Port   Name   Down Time   [Up Time]
    The header's column positions keep a description from being read as a time,
    and the optional Up Time column is ignored.
    """
    result, down_col, up_col = {}, None, -1
    for line in (output or "").splitlines():
        if "Down Time" in line and line.lstrip().startswith("Port"):
            down_col, up_col = line.index("Down Time"), line.find("Up Time")
            continue
        parts = line.split()
        if not parts or not _NAME_RE.match(parts[0]):
            continue
        times = [(t.start(), t.group()) for t in re.finditer(r"\S+", line)][1:]
        times = [(pos, tok) for pos, tok in times if _DURATION_TOKEN.match(tok)]
        if down_col is not None:
            times = [(pos, tok) for pos, tok in times
                     if pos >= down_col - 3 and (up_col < 0 or pos < up_col - 1)]
        if times:
            result[normalize_intf(parts[0])] = times[0][1]
    return result


# ---------------------------------------------------------------------------
# History: what earlier runs saw, so a reboot doesn't wipe out what we know
# ---------------------------------------------------------------------------
def empty_history(host):
    return {"host": host, "first_run": None, "last_run": None, "runs": 0, "members": {}, "ports": {}}


def _history_path(data_dir, host):
    return Path(data_dir) / "history" / (re.sub(r"[^\w.-]", "_", host) + ".json")


def load_history(data_dir, host):
    path = _history_path(data_dir, host)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return empty_history(host)


def save_history(data_dir, host, history):
    path = _history_path(data_dir, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _merge(intervals, slack=GAP_TOLERANCE):
    """Merge [start, end] pairs. A gap shorter than `slack` (the hours between last
    night's run and a reboot) counts as touching."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + slack:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def update_coverage(history, member, now, uptime):
    """Record that this member's counters cover [boot, now]. Return how far back the
    evidence is unbroken, or None.

    Example: a run 2 months ago saw 1 year of uptime, then the switch rebooted 1 week
    ago with no run in between. That gap is a blind spot, so evidence only goes back
    1 week.
    """
    entry = history.setdefault("members", {}).setdefault(str(member), {"covered": []})
    spans = [[datetime.fromisoformat(s), datetime.fromisoformat(e)] for s, e in entry["covered"]]
    if uptime is not None:
        spans.append([now - uptime, now])
    merged = _merge(spans)
    entry["covered"] = [[s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds")] for s, e in merged]
    if merged and merged[-1][1] >= now - timedelta(hours=2):
        return merged[-1][0]
    return None


# ---------------------------------------------------------------------------
# Deciding Active / Inactive / Unsure for one port
# ---------------------------------------------------------------------------
def evaluate_port(now, window, months, connected, down_text, uptime, last_used_days, how,
                  history_up, covered_since):
    """Return (activity, last_active datetime or None, why)."""
    evidence = []   # (when the port was last known in use, why we think so)
    if connected:
        evidence.append((now, "Link is up now"))

    down, gran = parse_cisco_duration(down_text)
    if down is not None and uptime is not None and not connected:
        # Down since boot shows a down time about equal to the uptime: not evidence of use.
        if down + gran + timedelta(minutes=30) < uptime:
            evidence.append((now - down, f"Link went down {down_text} ago"))

    if last_used_days is not None:
        evidence.append((now - timedelta(days=last_used_days), how or "Last input/output"))

    if history_up:
        evidence.append((history_up, f"Seen up on {history_up:%Y-%m-%d} by an earlier run"))

    last_active, why = max(evidence, key=lambda e: e[0]) if evidence else (None, "")
    if last_active is not None and now - last_active <= window:
        return "active", last_active, why

    if covered_since is not None and now - covered_since >= window:
        if last_active is not None:
            return "inactive", last_active, f"Unused for {human_span(now - last_active)}"
        return "inactive", None, f"Never used in the {human_span(now - covered_since)} we can see back"

    if covered_since is None:
        return "unsure", last_active, "The switch's uptime couldn't be read and there are no earlier runs"
    return "unsure", last_active, (f"Not used since the switch restarted {human_span(now - covered_since)} "
                                   f"ago, less than {months:g} months")


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------
def _port_count(max_port, model=""):
    """Ports to draw: from the model when it says (C9300-48P -> 48), else round up."""
    m = re.search(r"-(8|12|16|24|48)[A-Z]", model or "")
    if m:
        return max(int(m.group(1)), max_port)
    return next((size for size in (8, 12, 16, 24, 48) if max_port <= size), LAST_PORT)


def build_stack(outputs, host, months=6, history=None, now=None, where=""):
    """Turn what the switch printed into the data the page draws. Returns (data, history).

    No network here, so it's easy to test.
    """
    now = (now or datetime.now()).replace(microsecond=0)
    window = timedelta(days=months * DAYS_PER_MONTH)
    history = history or empty_history(host)
    warnings = []

    version = parse_version(outputs.get("version"))
    switch_rows = parse_show_switch(outputs.get("switch"))
    neighbors = parse_two_port_table(outputs.get("neighbors"))
    stack_ports = parse_two_port_table(outputs.get("stack_ports"))
    status = parse_status(outputs.get("status"))
    link = parse_link(outputs.get("link"))
    vlan_names = parse_vlan_names(outputs.get("vlans"))
    configs = parse_interface_configs(outputs.get("config"))
    stack_days = version["uptime"].total_seconds() / 86400 if version["uptime"] else None
    intfs = {normalize_intf(p["port"]): p
             for p in analyzer.parse_ios_interfaces(outputs.get("interfaces"), stack_days)}

    if not status and not intfs:
        raise ValueError("Read the switch, but no ports could be read. Is it a Cisco Catalyst?")
    if not outputs.get("link"):
        warnings.append("This switch didn't answer 'show interfaces link', so link down-times weren't "
                        "used. Results rely on Last input/output and earlier runs.")

    # Front-panel ports 1-48 for each member
    by_member = {}
    for name in set(status) | set(intfs):
        pos = port_position(name)
        if not pos:
            continue
        ptype, member, slot, port = pos
        if slot != 0 or ptype in EXCLUDED_TYPES or not FIRST_PORT <= port <= LAST_PORT:
            continue
        by_member.setdefault(member, {}).setdefault(port, []).append((ptype, name))
    # Two names on one number (2960-X: Gi1/0/1 and uplink Te1/0/1)? Keep the type
    # with the most ports on that member: that's the row of access ports.
    chosen = {}
    for member, ports in by_member.items():
        count = {}
        for cands in ports.values():
            for ptype, _ in cands:
                count[ptype] = count.get(ptype, 0) + 1
        chosen[member] = {p: max(cands, key=lambda c: count[c[0]])[1] for p, cands in ports.items()}

    member_numbers = sorted(set(chosen) | set(switch_rows))
    if not switch_rows:
        warnings.append("'show switch' returned nothing, so this is drawn as a single switch.")

    if not history.get("first_run"):
        history["first_run"] = now.isoformat(timespec="seconds")
    history["last_run"] = now.isoformat(timespec="seconds")
    history["runs"] = history.get("runs", 0) + 1
    port_history = history.setdefault("ports", {})

    members_out = []
    totals = {"active": 0, "inactive": 0, "unsure": 0, "up_now": 0, "total": 0}
    vlan_counts = {}
    for num in member_numbers:
        row = switch_rows.get(num, {})
        vinfo = version["members"].get(num, {})
        uptime = vinfo.get("uptime") or version["uptime"]
        uptime_text = vinfo.get("uptime_text") or version["uptime_text"]
        covered_since = update_coverage(history, num, now, uptime)
        names = chosen.get(num, {})
        size = _port_count(max(names) if names else LAST_PORT, vinfo.get("model", ""))
        counts = {"active": 0, "inactive": 0, "unsure": 0, "up_now": 0}
        ports_out = []

        for port in range(1, size + 1):
            name = names.get(port)
            if not name:
                ports_out.append({"num": port, "absent": True})
                continue
            st, it = status.get(name, {}), intfs.get(name, {})
            state = st.get("status") or {"up": "connected", "disabled": "disabled"}.get(it.get("status"), "notconnect")
            connected = state == "connected" or it.get("status") == "up"
            seen = port_history.get(name, {}).get("last_seen_up")
            history_up = datetime.fromisoformat(seen) if seen else None

            activity, last_active, why = evaluate_port(
                now, window, months, connected, link.get(name), uptime,
                it.get("last_used_days"), it.get("how", ""), history_up, covered_since)
            if last_active and (history_up is None or last_active > history_up):
                port_history[name] = {"last_seen_up": last_active.isoformat(timespec="seconds")}

            vlan = st.get("vlan") or "?"
            vlan_counts[vlan] = vlan_counts.get(vlan, 0) + 1
            counts[activity] += 1
            counts["up_now"] += int(connected)
            ports_out.append({
                "num": port, "name": name,
                "desc": it.get("description") or st.get("desc", ""),
                "status": state, "vlan": vlan,
                "speed": st.get("speed", ""), "duplex": st.get("duplex", ""),
                "activity": activity,
                "last_active": last_active.strftime("%Y-%m-%d") if last_active else "",
                "why": why, "up_now": connected,
                "admin_down": state == "disabled", "err": state.startswith("err-"),
            })
            if name in configs:
                ports_out[-1].update(ifname=configs[name]["ifname"], config=configs[name]["lines"],
                                     removed=configs[name]["removed"])

        if counts["unsure"]:
            warnings.append(
                f"Switch {num} has only been up {human_span(uptime) if uptime else 'an unknown time'}, "
                f"so {counts['unsure']} idle port(s) can't be called inactive yet and are marked Unsure. "
                f"Running this regularly fills in the gap.")
        sp, nb = stack_ports.get(num, ("", "")), neighbors.get(num, ("", ""))
        members_out.append({
            "number": num,
            "role": row.get("role", "" if switch_rows else "Standalone"),
            "state": row.get("state", ""),
            "model": vinfo.get("model", ""),
            "uptime": ", ".join(uptime_text.split(", ")[:2]) if uptime_text else "",
            "port_count": size,
            "stack_ports": [{"port": i + 1, "status": sp[i], "neighbor": nb[i]} for i in range(2)],
            "counts": counts,
            "ports": ports_out,
        })
        for k in counts:
            totals[k] += counts[k]
        totals["total"] += counts["active"] + counts["inactive"] + counts["unsure"]

    down = [f"Switch {m} port {i + 1}" for m, pair in sorted(stack_ports.items())
            for i, st in enumerate(pair) if st.upper() != "OK"]
    if down and len(member_numbers) > 1:
        warnings.insert(0, f"Stack ring is broken: {', '.join(down)} {'is' if len(down) == 1 else 'are'} "
                           f"DOWN. The stack still works as a half ring, but one more cable or switch "
                           f"failure will split it.")

    data = {
        "host": host, "where": where,
        "hostname": version["hostname"] or outputs.get("prompt", "") or host,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "months": months, "summary": totals, "warnings": warnings,
        "vlans": _vlan_legend(vlan_counts), "members": members_out,
        "vlan_names": {v: vlan_names[v] for v in sorted(vlan_counts, key=lambda v: (len(v), v)) if v in vlan_names},
        "has_config": bool(configs),
        "links": _stack_links(neighbors, stack_ports),
        "history_runs": history["runs"], "history_since": history["first_run"][:10],
    }
    return data, history


def _vlan_legend(vlan_counts):
    """The 8 most-used VLANs get the 8 colors, in a fixed order; the rest share 'other'."""
    special = {"trunk": "trunk", "routed": "routed"}
    numbered = sorted((v for v in vlan_counts if v not in special),
                      key=lambda v: (-vlan_counts[v], int(v) if v.isdigit() else 99999, v))
    legend = [{"vlan": v, "count": vlan_counts[v], "slot": i + 1 if i < 8 else "other"}
              for i, v in enumerate(numbered)]
    legend += [{"vlan": v, "count": vlan_counts[v], "slot": s} for v, s in special.items() if v in vlan_counts]
    return legend


def _stack_links(neighbors, stack_ports):
    """Stack cables as [{a, pa, b, pb, ok}], each cable once."""
    links, seen = [], set()
    for a, pair in neighbors.items():
        for pa, nb in ((1, pair[0]), (2, pair[1])):
            if not nb.isdigit():
                continue
            b = int(nb)
            back = neighbors.get(b, ("", ""))
            options = [i + 1 for i in range(2) if back[i] == str(a)]
            pb = (3 - pa) if (3 - pa) in options else (options[0] if options else 0)
            key = tuple(sorted([(a, pa), (b, pb)]))
            if key in seen:
                continue
            seen.add(key)
            ok = all(stack_ports.get(m, ("OK", "OK"))[p - 1].upper() == "OK" for m, p in ((a, pa), (b, pb)) if p)
            links.append({"a": a, "pa": pa, "b": b, "pb": pb, "ok": ok})
    return links


# ---------------------------------------------------------------------------
# Talking to the switch, saving the page
# ---------------------------------------------------------------------------
def collect_outputs(address, protocol, login, timeout=20, progress=print):
    """Log in once and run every command. A command the switch doesn't know gives ''."""
    outputs = {}
    with sw.connect(address, "cisco_ios", protocol, login, timeout) as conn:
        outputs["prompt"] = sw.prompt_name(conn)
        for key, command in COMMANDS.items():
            tries = list(command) if isinstance(command, tuple) else [command]
            progress(f"  {tries[0]}")
            try:
                _, outputs[key] = sw.run_first_working(conn, tries,
                                                       read_timeout=300 if key in ("interfaces", "config") else 180)
            except Exception as exc:                 # one bad command shouldn't stop the rest
                progress(f"    skipped: {sw.friendly_error(exc)}")
                outputs[key] = ""
    return outputs


def render_html(data):
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
    # Escape <, >, & so a port description can't break out of the <script> tag
    data_json = json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return env.get_template("topology.html").render(data=data, data_json=data_json, label=LABEL)


def save_page(data_dir, data, html):
    pages = Path(data_dir) / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", data["hostname"] or data["host"])
    path = pages / f"{safe}-{datetime.now():%Y%m%d-%H%M%S}.html"
    path.write_text(html, encoding="utf-8")
    return path


def list_pages(data_dir, limit=15):
    pages = Path(data_dir) / "pages"
    files = sorted(pages.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True) if pages.exists() else []
    out = []
    for p in files[:limit]:
        m = re.match(r"(.+)-(\d{8})-(\d{6})$", p.stem)
        name, when = (m.group(1), datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")) if m else (p.stem, None)
        out.append({"file": p.name, "name": name, "created": when.strftime("%Y-%m-%d %H:%M") if when else ""})
    return out


def run_topology(address, protocol, login, months=6, timeout=20, data_dir=None, where="", progress=print):
    """Read the stack, decide each port, remember what we saw, save the page.
    Returns (data, html, saved_path)."""
    data_dir = Path(data_dir or DEFAULT_DIR)
    outputs = collect_outputs(address, protocol, login, timeout, progress)
    data, history = build_stack(outputs, address, months, load_history(data_dir, address), where=where)
    save_history(data_dir, address, history)
    html = render_html(data)
    return data, html, save_page(data_dir, data, html)


def run_demo(months=6, data_dir=None, save=True):
    """A made-up 3-member stack, so you can see the page without a switch."""
    from topology_demo import demo_outputs
    data, _ = build_stack(demo_outputs(), "demo-stack", months, where="Made-up sample stack")
    html = render_html(data)
    return data, html, save_page(Path(data_dir or DEFAULT_DIR), data, html) if save else None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Draw a Cisco switch stack: ports 1-48, VLANs and use.")
    ap.add_argument("hosts", nargs="*", help="Stack address(es)")
    ap.add_argument("-u", "--username")
    ap.add_argument("-p", "--protocol", choices=["telnet", "ssh"], default="telnet")
    ap.add_argument("-m", "--months", type=float, default=6)
    ap.add_argument("--timeout", type=int, default=20, help="Login timeout in seconds")
    ap.add_argument("--demo", action="store_true", help="Draw a made-up sample stack")
    ap.add_argument("--no-open", action="store_true", help="Don't open the page in a browser")
    args = ap.parse_args()

    if args.demo:
        _, _, path = run_demo(args.months)
        print(f"Saved {path}")
        if not args.no_open:
            webbrowser.open(path.as_uri())
        return
    if not args.hosts or not args.username:
        ap.error("give at least one stack address and -u USERNAME (or use --demo)")

    # PA_PASSWORD / PA_SECRET let a scheduled task run without prompting
    password = os.environ.get("PA_PASSWORD") or getpass.getpass("Password: ")
    secret = os.environ.get("PA_SECRET")
    if secret is None:
        secret = getpass.getpass("Enable password (Enter if not needed): ")
    login = sw.Login(args.username, password, secret)

    failed = 0
    for host in args.hosts:
        print(f"Reading {host} over {args.protocol}...")
        try:
            data, _, path = run_topology(host, args.protocol, login, args.months, args.timeout)
        except Exception as exc:
            print(f"  Couldn't read it: {sw.friendly_error(exc)}")
            failed += 1
            continue
        s = data["summary"]
        print(f"  {len(data['members'])} member(s): {s['active']} active, {s['inactive']} inactive, "
              f"{s['unsure']} unsure. Saved {path}")
        if not args.no_open:
            webbrowser.open(path.as_uri())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
