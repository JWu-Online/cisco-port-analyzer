"""
topology.py - draw a switch stack (Cisco Catalyst or Dell PowerConnect 5500):
every member, ports 1-48, VLANs and use.

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

Dell PowerConnect 5500 switches don't record when a port was last used, only
packet counts since they started. So there: link up now, or traffic since a
switch that started less than N months ago, or counts that grew since an earlier
run -> Active. No traffic since a start more than N months ago, or counts that
haven't moved for N months of runs -> Inactive. Anything else is Unsure.

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
DELL_COMMANDS = {
    "system": ("show system", "show version"),
    "stack": ("show stack", "show switch"),
    "status": "show interfaces status",
    "description": "show interfaces description",
    "counters": "show interfaces counters",
    "vlans": "show vlan",
    "config": "show running-config",
}
VENDORS = {"cisco_ios": "Cisco Catalyst", "dell_powerconnect": "Dell PowerConnect"}
COMMANDS_FOR = {"cisco_ios": COMMANDS, "dell_powerconnect": DELL_COMMANDS}
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

    Works for Cisco (indented lines, ending at '!') and Dell (lines that may not
    be indented, ending at 'exit', and 'interface range ...' blocks).
    Only interface sections are kept; the rest of the config (users, SNMP,
    TACACS keys) is ignored. A port line that carries a key or password (rare on
    access ports) is swapped for a '!' comment, so saved pages never hold secrets.
    """
    configs, current, indented = {}, [], None
    for raw in (output or "").splitlines():
        line = raw.rstrip()
        body = line.strip()
        m = re.match(r"^interface\s+(?:range\s+)?(.+)$", line, re.I)
        if m:
            current, indented = [], None
            for ifname in analyzer.expand_ports(m.group(1).strip()):
                entry = configs.setdefault(normalize_intf(ifname), {"ifname": ifname, "lines": [], "removed": 0})
                current.append(entry)
            continue
        if not current or not body:
            continue
        if body.lower() in ("exit", "!", "end"):
            current = []
            continue
        if indented is None:
            indented = line.startswith(" ")
        if indented and not line.startswith(" "):
            current = []                # the next top-level command ends an indented block
            continue
        lead = line[:len(line) - len(line.lstrip())]
        secret = None if body.lower().startswith("description ") else _SECRET_WORDS.search(body)
        for entry in current:
            if secret:
                entry["lines"].append(f"{lead}! left out: a line with a key or password ({body[:secret.end()]} ...)")
                entry["removed"] += 1
            else:
                entry["lines"].append(line)
    return configs


# ---------------------------------------------------------------------------
# Port settings that can move between a Cisco and a Dell
# ---------------------------------------------------------------------------
_IGNORED = {"switchport", "no shutdown", "switchport trunk encapsulation dot1q"}


def parse_profile(lines, voice_id=""):
    """A port's config lines (Cisco or Dell) -> the settings both brands understand.

    Anything else ends up in "other", so the page can say it wasn't carried over.
    """
    prof = {"description": "", "mode": "", "access_vlan": "", "voice_vlan": "", "native_vlan": "",
            "allowed": [], "shutdown": False, "portfast": False, "bpduguard": False, "general": False,
            "other": []}
    for raw in lines or []:
        line = raw.strip()
        low = re.sub(r"\s+", " ", line.lower())
        if not line or low in _IGNORED:
            continue
        if line.startswith("!"):                       # left out because it had a key or password
            prof["other"].append(line.lstrip("! ").strip())
            continue
        m = re.match(r"(?i)description\s+(.*)$", line)
        if m:
            prof["description"] = m.group(1).strip().strip('"').strip("'")
            continue
        m = re.fullmatch(r"switchport mode (access|trunk|general)", low)
        if m:
            prof["mode"] = m.group(1)
            continue
        m = (re.fullmatch(r"switchport access vlan (\d+)", low) or
             re.fullmatch(r"switchport voice vlan (\d+)", low) or
             re.fullmatch(r"switchport (?:trunk native vlan|general pvid) (\d+)", low) or
             re.fullmatch(r"switchport (?:trunk|general) allowed vlan (?:add )?([\d,\-]+)(?: (?:tagged|untagged))?", low))
        if m:
            if low.startswith("switchport access"):
                prof["access_vlan"] = m.group(1)
            elif low.startswith("switchport voice"):
                prof["voice_vlan"] = m.group(1)
            elif "allowed" in low:
                prof["allowed"].append(m.group(1))
            else:
                prof["native_vlan"] = m.group(1)
            continue
        if low == "voice vlan enable" and voice_id:      # Dell: the voice VLAN number is switch-wide
            prof["voice_vlan"] = voice_id
        elif low == "shutdown":
            prof["shutdown"] = True
        elif low.startswith("spanning-tree portfast"):
            prof["portfast"] = True
        elif low in ("spanning-tree bpduguard enable", "spanning-tree bpduguard"):
            prof["bpduguard"] = True
        else:
            prof["other"].append(line)
    if prof["mode"] == "general":                        # Dell's general mode is closest to a trunk
        prof["mode"], prof["general"] = "trunk", True
    return prof


def basic_profile(desc, vlan):
    """No config was read: rebuild what we know from the port's description and VLAN."""
    prof = parse_profile([])
    prof["description"] = desc or ""
    if (vlan or "").isdigit():
        prof["mode"], prof["access_vlan"] = "access", vlan
    elif vlan == "trunk":
        prof["mode"] = "trunk"
        prof["other"].append("the trunk's VLANs (its config wasn't read)")
    return prof


def render_profile(prof, vendor):
    """Lines to paste under 'interface X' on a `vendor` switch, and notes on what didn't carry over."""
    cisco = vendor == "cisco_ios"
    pre = " " if cisco else ""
    lines, notes = [], []
    desc = prof["description"]
    if desc:
        if not cisco:
            if len(desc) > 64:
                notes.append("Description cut to 64 characters, the Dell limit")
                desc = desc[:64]
            desc = desc.replace('"', "'")
            desc = f'"{desc}"' if " " in desc else desc
        lines.append(f"{pre}description {desc}")
    if prof["mode"] == "trunk":
        lines.append(f"{pre}switchport mode trunk")
        if prof["native_vlan"]:
            lines.append(f"{pre}switchport trunk native vlan {prof['native_vlan']}")
        if prof["allowed"]:
            vlans = ",".join(prof["allowed"])
            lines.append(f" switchport trunk allowed vlan {vlans}" if cisco else f"switchport trunk allowed vlan add {vlans}")
        if prof["general"] and cisco:
            notes.append("It was in Dell 'general' mode, copied as a trunk: check its VLANs")
    elif prof["mode"] == "access" or prof["access_vlan"]:
        lines.append(f"{pre}switchport mode access")
        if prof["access_vlan"]:
            lines.append(f"{pre}switchport access vlan {prof['access_vlan']}")
    if prof["voice_vlan"]:
        if cisco:
            lines.append(f" switchport voice vlan {prof['voice_vlan']}")
        else:
            notes.append(f"Voice VLAN {prof['voice_vlan']} not copied: on a Dell it's set for the whole switch "
                         f"('voice vlan id {prof['voice_vlan']}'), then 'voice vlan enable' on the port")
    if prof["portfast"]:
        lines.append(f"{pre}spanning-tree portfast")
    if prof["bpduguard"]:
        lines.append(f"{pre}spanning-tree bpduguard enable")
    if prof["shutdown"]:
        lines.append(f"{pre}shutdown")
    if prof["other"]:
        notes.append("Not copied, add by hand if needed: " + "; ".join(prof["other"]))
    return lines, notes


def port_copies(vendor, config_lines, prof):
    """What to paste for this port onto a Cisco and onto a Dell.

    Same brand as the old switch: the old config lines exactly. Other brand:
    the settings both understand, written the new switch's way.
    """
    out = {}
    for target in VENDORS:
        if target == vendor and config_lines is not None:
            if target == "cisco_ios":
                lines, notes = list(config_lines), []
            else:
                lines = [ln.strip() for ln in config_lines if ln.strip() and not ln.strip().startswith("!")]
                left = len(config_lines) - len([ln for ln in config_lines if not ln.strip().startswith("!")])
                notes = [f"{left} line(s) with a key or password left out"] if left else []
        else:
            lines, notes = render_profile(prof, target)
        out[target] = {"lines": lines, "notes": notes}
    return out


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
    m = re.search(r"-(8|12|16|24|48)[A-Z]", model or "") or re.search(r"\b55(24|48)", model or "")
    if m:
        return max(int(m.group(1)), max_port)
    return next((size for size in (8, 12, 16, 24, 48) if max_port <= size), LAST_PORT)


def _front_panel(names):
    """{member: {port number: name}} for front-panel ports 1-48.

    Two names on one number (2960-X: Gi1/0/1 and uplink Te1/0/1)? Keep the type
    with the most ports on that member: that's the row of access ports.
    """
    by_member = {}
    for name in names:
        pos = port_position(name)
        if not pos:
            continue
        ptype, member, slot, port = pos
        if slot != 0 or ptype in EXCLUDED_TYPES or not FIRST_PORT <= port <= LAST_PORT:
            continue
        by_member.setdefault(member, {}).setdefault(port, []).append((ptype, name))
    chosen = {}
    for member, ports in by_member.items():
        count = {}
        for cands in ports.values():
            for ptype, _ in cands:
                count[ptype] = count.get(ptype, 0) + 1
        chosen[member] = {p: max(cands, key=lambda c: count[c[0]])[1] for p, cands in ports.items()}
    return chosen


def _touch_history(history, now):
    if not history.get("first_run"):
        history["first_run"] = now.isoformat(timespec="seconds")
    history["last_run"] = now.isoformat(timespec="seconds")
    history["runs"] = history.get("runs", 0) + 1
    return history.setdefault("ports", {})


def build_stack(outputs, host, months=6, history=None, now=None, where="", vendor="cisco_ios"):
    """Turn what the switch printed into the data the page draws. Returns (data, history).

    No network here, so it's easy to test.
    """
    if vendor == "dell_powerconnect":
        return build_dell_stack(outputs, host, months, history, now, where)
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

    chosen = _front_panel(set(status) | set(intfs))
    member_numbers = sorted(set(chosen) | set(switch_rows))
    if not switch_rows:
        warnings.append("'show switch' returned nothing, so this is drawn as a single switch.")
    port_history = _touch_history(history, now)

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
            cfg = configs.get(name)
            if cfg:
                ports_out[-1].update(ifname=cfg["ifname"], config=cfg["lines"], removed=cfg["removed"])
            prof = parse_profile(cfg["lines"]) if cfg else basic_profile(ports_out[-1]["desc"], vlan)
            ports_out[-1].update(copy=port_copies("cisco_ios", cfg["lines"] if cfg else None, prof), basic=cfg is None)

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
        "host": host, "where": where, "vendor": "cisco_ios", "vendor_name": VENDORS["cisco_ios"],
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


# ---------------------------------------------------------------------------
# Dell PowerConnect 5500 (5524, 5548, 5548P)
# ---------------------------------------------------------------------------
def parse_dell_system(output):
    """'show system' -> hostname, uptime, model, and a model per stack unit when it lists them."""
    output = output or ""
    info = {"hostname": "", "uptime": None, "model": "", "units": {}}
    m = re.search(r"(?im)^\s*System Name:\s*(\S+)", output)
    if m:
        info["hostname"] = m.group(1)
    days = analyzer.uptime_from(output)
    info["uptime"] = timedelta(days=days) if days is not None else None
    m = re.search(r"(?i)PowerConnect\s*(\w+)", output)
    if m:
        info["model"] = "PowerConnect " + m.group(1).upper()
    for line in output.splitlines():
        u = re.match(r"(?i)^\s*(\d{1,2})\s+(?:Dell\s+)?(?:PowerConnect\s+)?(55\d\dP?)\b", line)
        if u:
            info["units"][int(u.group(1))] = "PowerConnect " + u.group(2).upper()
    return info


def parse_dell_stack(output):
    """'show stack' -> {unit: role}. Best effort: the layout changes between firmware versions,
    so this only looks for a unit number and the last master/backup/slave/member word on the line."""
    roles = {}
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d{1,2})\s+(.*)$", line)
        if not m or int(m.group(1)) > 8:
            continue
        words = re.findall(r"(?i)\b(master|backup|slave|member)\b", m.group(2))
        if words:
            roles[int(m.group(1))] = {"master": "Active", "backup": "Standby"}.get(words[-1].lower(), "Member")
    return roles


def parse_dell_vlans(output):
    """'show vlan' -> (names, untagged, tagged) as {vlan: name}, {port: {vlans}}, {port: {vlans}}.

    Header:  Vlan  Name  Tagged Ports  UnTagged Ports  Created by
    Port lists can wrap onto the next lines; each list is put in whichever of the
    two columns it starts closest to.
    """
    names, untagged, tagged = {}, {}, {}
    lines = (output or "").splitlines()
    head = next((i for i, ln in enumerate(lines)
                 if re.search(r"(?i)\buntagged\b", ln) and re.search(r"(?i)\bvlan\b", ln)), None)
    if head is None:
        return names, untagged, tagged
    h = lines[head].lower()
    name_col, untag_col, created = h.find("name"), h.find("untagged"), h.find("created")
    tag = re.search(r"(?<!un)tagged", h)
    tag_col = tag.start() if tag else untag_col
    port_list = re.compile(r"(?<![\w-])(?:gi|te|fa|xg|po|ch)\d[\w/,\-]*\d(?![\w/])", re.I)
    current = None
    for line in lines[head + 1:]:
        if not line.strip() or set(line.strip()) <= set("- "):
            continue
        lists = [t for t in port_list.finditer(line) if t.start() >= name_col + 1]
        m = re.match(r"^\s*(\d{1,4})\b", line)
        if m:
            current = m.group(1)
            # The name runs from the VLAN number to the first port list (or the Created-by code)
            end = lists[0].start() if lists else (created if created > m.end() else len(line))
            name = re.sub(r"\s+(?:[DSGRV]{1,3})\s*$", "", line[m.end():end]).strip()
            if name and name != current:
                names[current] = name
        if current is None:
            continue
        for tok in lists:
            bucket = tagged if abs(tok.start() - tag_col) < abs(tok.start() - untag_col) else untagged
            for port in analyzer.expand_ports(tok.group().replace(",", " ")):
                bucket.setdefault(normalize_intf(port), set()).add(current)
    return names, untagged, tagged


def dell_packet_totals(output):
    """'show interfaces counters' -> {port: packets in + out since the switch started}."""
    totals, columns = {}, None
    for line in (output or "").splitlines():
        words = line.split()
        if words and words[0].lower() == "port" and len(words) > 1:
            columns = [w.lower() for w in words[1:]]          # InUcastPkts InMcastPkts ... InOctets
            continue
        m = analyzer.DELL_PORT.match(line)
        if not m:
            continue
        values = words[1:]
        if columns and len(columns) == len(values):
            numbers = [int(v) for c, v in zip(columns, values) if "pkts" in c and v.isdigit()]
        else:
            numbers = [int(v) for v in values if v.isdigit()]
        if numbers:
            key = normalize_intf(m.group(1))
            totals[key] = totals.get(key, 0) + sum(numbers)
    return totals


def evaluate_counter_port(now, window, months, connected, total, uptime, hist, same_boot):
    """Dell: decide from packet counts since the switch started, plus earlier runs.

    Returns (activity, last_active, why) like evaluate_port.
    """
    evidence = []
    if connected:
        evidence.append((now, "Link is up now"))
    if hist.get("last_seen_up"):
        seen = datetime.fromisoformat(hist["last_seen_up"])
        evidence.append((seen, f"In use on {seen:%Y-%m-%d} (seen by an earlier run)"))
    checked = datetime.fromisoformat(hist["checked"]) if hist.get("checked") else None
    grew = bool(total is not None and same_boot and checked and hist.get("total") is not None and total > hist["total"])
    if grew:
        evidence.append((checked, f"Traffic since the run on {checked:%Y-%m-%d}"))
    if total and uptime is not None and uptime <= window:
        evidence.append((now - uptime, f"{total:,} packets since the switch started {human_span(uptime)} ago"))

    last_active, why = max(evidence, key=lambda e: e[0]) if evidence else (None, "")
    if last_active is not None and now - last_active <= window:
        return "active", last_active, why

    since = datetime.fromisoformat(hist["total_since"]) if hist.get("total_since") and same_boot else None
    if total == 0 and uptime is not None and uptime >= window:
        return "inactive", last_active, f"No traffic in the {human_span(uptime)} since the switch started"
    if total and since and total == hist.get("total") and now - since >= window:
        return "inactive", last_active, f"No new traffic since {since:%Y-%m-%d}"
    if total is None:
        return "unsure", last_active, "No packet counts could be read"
    if total == 0:
        return "unsure", last_active, (f"No traffic, but the switch started only {human_span(uptime)} ago"
                                       if uptime else "No traffic, but the switch's uptime couldn't be read")
    if grew:
        return "unsure", last_active, (f"Its count went up since the drawing on {checked:%Y-%m-%d}, but that's more "
                                       f"than {months:g} months ago, so it can't tell when. Draw it more often")
    started = f" {human_span(uptime)} ago" if uptime else ""
    return "unsure", last_active, (f"{total:,} packets since the switch started{started}, but a Dell doesn't "
                                   f"record when. If the count stays the same in later drawings, it turns Inactive")


def build_dell_stack(outputs, host, months=6, history=None, now=None, where=""):
    """Same page data as build_stack, from a Dell PowerConnect 5500 stack."""
    now = (now or datetime.now()).replace(microsecond=0)
    window = timedelta(days=months * DAYS_PER_MONTH)
    history = history or empty_history(host)
    warnings = []

    system = parse_dell_system(outputs.get("system"))
    roles = parse_dell_stack(outputs.get("stack"))
    base = analyzer.parse_dell({"show interfaces status": outputs.get("status") or "",
                                "show interfaces description": outputs.get("description") or ""}, None)
    ports_by = {normalize_intf(p["port"]): p for p in base}
    totals = dell_packet_totals(outputs.get("counters"))
    vlan_names, untagged, tagged = parse_dell_vlans(outputs.get("vlans"))
    configs = parse_interface_configs(outputs.get("config"))
    voice = re.search(r"(?im)^\s*voice vlan id (\d+)", outputs.get("config") or "")
    voice_id = voice.group(1) if voice else ""

    if not ports_by:
        raise ValueError("Read the switch, but no ports could be read. Is it a Dell PowerConnect 5500?")
    if not outputs.get("counters"):
        warnings.append("This switch didn't answer 'show interfaces counters', so only ports that are up "
                        "now can be called Active.")

    uptime = system["uptime"]
    boot = now - uptime if uptime else None
    prev = history.get("boot")
    same_boot = bool(boot and prev and abs(datetime.fromisoformat(prev) - boot) < timedelta(hours=1))
    if boot:
        history["boot"] = boot.isoformat(timespec="seconds")
    port_history = _touch_history(history, now)
    chosen = _front_panel(ports_by)

    members_out = []
    summary = {"active": 0, "inactive": 0, "unsure": 0, "up_now": 0, "total": 0}
    vlan_counts = {}
    counted = 0               # Unsure ports that had traffic at some unknown time
    for num in sorted(chosen):
        model = system["units"].get(num) or system["model"]
        names = chosen[num]
        size = _port_count(max(names), model)
        counts = {"active": 0, "inactive": 0, "unsure": 0, "up_now": 0}
        ports_out = []
        for port in range(1, size + 1):
            name = names.get(port)
            if not name:
                ports_out.append({"num": port, "absent": True})
                continue
            b = ports_by[name]
            connected = b["status"] == "up"
            cfg = configs.get(name)
            if cfg is None and configs:        # a Dell only lists ports that have something set
                long_name = re.sub(r"(?i)^(gi|te|fa)", lambda x: {"gi": "gigabitethernet", "te": "tengigabitethernet",
                                                               "fa": "fastethernet"}[x.group(1).lower()], b["port"])
                cfg = {"ifname": long_name, "lines": [], "removed": 0}
            prof = parse_profile(cfg["lines"], voice_id) if cfg else None
            state = "connected" if connected else ("disabled" if prof and prof["shutdown"] else "notconnect")
            if prof and prof["mode"] == "trunk":
                vlan = "trunk"
            elif prof and prof["access_vlan"]:
                vlan = prof["access_vlan"]
            elif tagged.get(name):
                vlan = "trunk"
            elif len(untagged.get(name, ())) == 1:
                vlan = next(iter(untagged[name]))
            else:
                vlan = "1" if cfg is not None else "?"
            total = totals.get(name)
            hist = port_history.get(name, {})
            activity, last_active, why = evaluate_counter_port(now, window, months, connected, total,
                                                               uptime, hist, same_boot)
            if activity == "unsure" and total:
                counted += 1

            new = dict(hist)
            if last_active and (not hist.get("last_seen_up") or last_active > datetime.fromisoformat(hist["last_seen_up"])):
                new["last_seen_up"] = last_active.isoformat(timespec="seconds")
            if total is not None:
                if not same_boot or hist.get("total") != total or not hist.get("total_since"):
                    new["total_since"] = now.isoformat(timespec="seconds")
                new["total"] = total
            new["checked"] = now.isoformat(timespec="seconds")
            port_history[name] = new

            vlan_counts[vlan] = vlan_counts.get(vlan, 0) + 1
            counts[activity] += 1
            counts["up_now"] += int(connected)
            ports_out.append({
                "num": port, "name": b["port"], "desc": b.get("description", ""),
                "status": state, "vlan": vlan, "speed": "", "duplex": "",
                "activity": activity,
                "last_active": last_active.strftime("%Y-%m-%d") if last_active else "",
                "why": why, "up_now": connected,
                "admin_down": state == "disabled", "err": False,
            })
            if cfg:
                ports_out[-1].update(ifname=cfg["ifname"], config=cfg["lines"], removed=cfg["removed"])
            prof = prof or basic_profile(ports_out[-1]["desc"], vlan)
            ports_out[-1].update(copy=port_copies("dell_powerconnect", cfg["lines"] if cfg else None, prof),
                                 basic=cfg is None)

        members_out.append({
            "number": num,
            "role": roles.get(num, "Standalone" if len(chosen) == 1 else ""),
            "state": "", "model": model,
            "uptime": human_span(uptime) if uptime else "",
            "port_count": size, "stack_ports": [], "counts": counts, "ports": ports_out,
        })
        for k in counts:
            summary[k] += counts[k]
        summary["total"] += counts["active"] + counts["inactive"] + counts["unsure"]

    if counted:
        warnings.append(f"{counted} port(s) had traffic since the switch started {human_span(uptime) if uptime else ''} "
                        f"ago, but Dell switches don't record when, so they're Unsure. Each drawing remembers the "
                        f"counts: draw this stack again over the next weeks, and ports whose counts never move "
                        f"turn Inactive once that covers {months:g} months.")

    data = {
        "host": host, "where": where, "vendor": "dell_powerconnect", "vendor_name": VENDORS["dell_powerconnect"],
        "hostname": system["hostname"] or outputs.get("prompt", "") or host,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "months": months, "summary": summary, "warnings": warnings,
        "vlans": _vlan_legend(vlan_counts), "members": members_out,
        "vlan_names": {v: vlan_names[v] for v in sorted(vlan_counts, key=lambda v: (len(v), v)) if v in vlan_names},
        "has_config": bool(configs), "links": [],
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
def collect_outputs(address, protocol, login, timeout=20, progress=print, vendor="cisco_ios"):
    """Log in once and run every command. A command the switch doesn't know gives ''."""
    outputs = {}
    with sw.connect(address, vendor, protocol, login, timeout) as conn:
        outputs["prompt"] = sw.prompt_name(conn)
        for key, command in COMMANDS_FOR[vendor].items():
            tries = list(command) if isinstance(command, tuple) else [command]
            progress(f"  {tries[0]}")
            try:
                _, outputs[key] = sw.run_first_working(conn, tries,
                                                       read_timeout=300 if key in ("interfaces", "config", "counters") else 180)
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


def run_topology(address, protocol, login, months=6, timeout=20, data_dir=None, where="", progress=print,
                 vendor="cisco_ios"):
    """Read the stack, decide each port, remember what we saw, save the page.
    Returns (data, html, saved_path)."""
    data_dir = Path(data_dir or DEFAULT_DIR)
    outputs = collect_outputs(address, protocol, login, timeout, progress, vendor)
    data, history = build_stack(outputs, address, months, load_history(data_dir, address), where=where,
                                vendor=vendor)
    save_history(data_dir, address, history)
    html = render_html(data)
    return data, html, save_page(data_dir, data, html)


def run_demo(months=6, data_dir=None, save=True, vendor="cisco_ios"):
    """A made-up stack (Cisco or Dell), so you can see the page without a switch."""
    from topology_demo import demo_outputs, dell_demo_outputs
    if vendor == "dell_powerconnect":
        data, _ = build_stack(dell_demo_outputs(), "demo-dell-stack", months, where="Made-up sample stack",
                              vendor=vendor)
    else:
        data, _ = build_stack(demo_outputs(), "demo-stack", months, where="Made-up sample stack")
    html = render_html(data)
    return data, html, save_page(Path(data_dir or DEFAULT_DIR), data, html) if save else None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Draw a Cisco Catalyst or Dell PowerConnect stack: ports 1-48, "
                                             "VLANs and use.")
    ap.add_argument("hosts", nargs="*", help="Stack address(es)")
    ap.add_argument("-t", "--type", default="cisco", help="cisco (Catalyst, the default) or dell (PowerConnect 5500)")
    ap.add_argument("-u", "--username")
    ap.add_argument("-p", "--protocol", choices=["telnet", "ssh"], default="telnet")
    ap.add_argument("-m", "--months", type=float, default=6)
    ap.add_argument("--timeout", type=int, default=20, help="Login timeout in seconds")
    ap.add_argument("--demo", action="store_true", help="Draw a made-up sample stack")
    ap.add_argument("--no-open", action="store_true", help="Don't open the page in a browser")
    args = ap.parse_args()
    vendor = sw.clean_type(args.type)
    if vendor not in VENDORS:
        ap.error("--type must be cisco or dell")

    if args.demo:
        _, _, path = run_demo(args.months, vendor=vendor)
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
            data, _, path = run_topology(host, args.protocol, login, args.months, args.timeout, vendor=vendor)
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
