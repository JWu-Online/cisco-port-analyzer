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
import time
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
    "modules": "show module",
    "power": "show power inline",
    "interfaces": "show interfaces",
}
DELL_COMMANDS = {
    "system": ("show system", "show version"),
    "stack": ("show stack", "show switch"),
    "status": "show interfaces status",
    "description": "show interfaces description",
    "counters": "show interfaces counters",
    "vlans": "show vlan",
    "power": "show power inline",
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
    """(type, member, slot, port) or None.

    Gi2/0/14 -> ('Gi', 2, 0, 14)   stack member 2
    Fa0/7    -> ('Fa', 1, 0, 7)    standalone switch, member 1
    Gi3/14   -> ('Gi', 3, 0, 14)   chassis (4500, 4948): line card in slot 3 is drawn like a member
    """
    m = _NAME_RE.match(short_name)
    if not m:
        return None
    prefix, nums = m.groups()
    parts = [int(n) for n in nums.split("/")]
    if len(parts) == 3:
        member, slot, port = parts
    else:
        first, port = parts
        member, slot = (first or 1), 0
    return prefix, member, slot, port


def is_chassis(names):
    """True for a chassis like a 4507: two-number port names on more than one slot (Gi2/1, Gi3/1)."""
    slots = set()
    for name in names:
        m = _NAME_RE.match(name)
        if not m:
            continue
        nums = m.group(2).split("/")
        if len(nums) == 3:
            return False
        if nums[0] != "0":
            slots.add(nums[0])
    return len(slots) > 1


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


def parse_modules(output):
    """'show module' on a chassis (4500) -> {slot: {"ports", "card", "model"}}, plus the chassis model."""
    mods = {}
    chassis = re.search(r"(?im)^\s*Chassis Type\s*:\s*(\S+)", output or "")
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d{1,2})\s+(\d{1,3})\s+(.+?)\s{2,}(\S+)\s+\S+\s*$", line)
        if m and int(m.group(1)) not in mods:
            mods[int(m.group(1))] = {"ports": int(m.group(2)), "card": m.group(3).strip(), "model": m.group(4)}
    return mods, (chassis.group(1) if chassis else "")


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
            "tagged": [], "untagged": [], "other": [], "network_policy": ""}
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
                # Dell: 'add 20 untagged' / 'add 40 tagged' (tagged if not said); a trunk's allowed VLANs are tagged
                prof["untagged" if low.endswith(" untagged") else "tagged"].append(m.group(1))
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
            m = re.fullmatch(r"network-policy (\d+)", low)   # a Cisco LLDP-MED profile (its voice VLAN)
            if m:
                prof["network_policy"] = m.group(1)
            prof["other"].append(line)                     # (still listed as not copied to a Dell)
    if prof["mode"] == "general":
        prof["general"] = True
        tagged, untagged = vlan_set(prof["tagged"]), vlan_set(prof["untagged"])
        pvid = prof["native_vlan"] or (next(iter(untagged)) if len(untagged) == 1 else "")
        voice = prof["voice_vlan"] or voice_id
        if pvid and untagged <= {pvid} and tagged <= ({voice} if voice else set()):
            # The usual Dell desk port: its PVID untagged, at most the voice VLAN tagged. That's an access port.
            prof.update(mode="access", access_vlan=pvid, native_vlan="", allowed=[],
                        voice_vlan=next(iter(tagged)) if tagged else prof["voice_vlan"])
        else:                                            # anything more is closest to a trunk
            prof["mode"] = "trunk"
    return prof


def vlan_set(parts):
    """['20', '30-32,40'] -> {'20', '30', '31', '32', '40'}."""
    out = set()
    for part in ",".join(parts).split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            out.update(str(v) for v in range(lo, min(hi, lo + 4094) + 1))
    return out


def dell_port_vlans(prof, in_config, untagged, tagged, voice_id):
    """How to label a Dell port: (label, pvid, tagged VLANs).

    The label is the PVID (the VLAN untagged traffic goes into), so a desk port with its voice VLAN
    tagged reads as the desk's VLAN, not "trunk". Only a port carrying two or more tagged VLANs
    besides the voice VLAN (an uplink, say) is labelled trunk. 'show vlan' wins over the config,
    because it also shows the voice VLAN a phone was put on by itself.
    """
    pvid = ""
    if prof:
        pvid = prof["native_vlan"] if prof["mode"] == "trunk" else prof["access_vlan"]
    if not pvid:
        pvid = next(iter(untagged)) if len(untagged) == 1 else ("1" if in_config else "")
    on_tag = set(tagged) or (vlan_set(prof["tagged"]) if prof else set())
    on_tag.discard(pvid)
    if prof and prof["voice_vlan"]:
        on_tag.add(prof["voice_vlan"])
    trunk_vlans = on_tag - {voice_id, prof["voice_vlan"] if prof else ""}
    label = "trunk" if len(trunk_vlans) >= 2 else (pvid or "?")
    return label, pvid, sorted(on_tag, key=int)


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
    elif prof["general"] and cisco:
        lines.append(" switchport mode access")
        lines.append(f" switchport access vlan {prof['access_vlan']}")
        notes.append(f"It was in Dell 'general' mode with PVID {prof['access_vlan']}"
                     + (f" and voice VLAN {prof['voice_vlan']} tagged" if prof["voice_vlan"] else "")
                     + ", copied as an access port" + (" with that voice VLAN" if prof["voice_vlan"] else ""))
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


def no_ports_message(names):
    """Say what was read when none of it could be put on a front panel."""
    sample = sorted((n for n in names if _NAME_RE.match(n)), key=analyzer.port_key)
    seen = f" ({', '.join(sample[:6])}{', ...' if len(sample) > 6 else ''})" if sample else ""
    return (f"Read {len(names)} ports{seen}, but couldn't place any of them as front-panel ports 1-48. "
            f"This switch names its ports in a way the topology page doesn't know yet. What it printed is "
            f"saved in the data\\debug folder; send that file to whoever maintains the app.")


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
    chassis = is_chassis(set(status) | set(intfs))
    if not chosen:
        raise ValueError(no_ports_message(set(status) | set(intfs)))
    modules, chassis_model = parse_modules(outputs.get("modules")) if chassis else ({}, "")
    power_ports, budgets = parse_power_inline(outputs.get("power"))
    errors = parse_ios_errors(outputs.get("interfaces"))
    issue_count = {"ports": 0, "power": 0, "slow": 0}
    member_numbers = sorted(set(chosen) | (set() if chassis else set(switch_rows)))
    if not switch_rows and not chassis and len(member_numbers) == 1:
        warnings.append("'show switch' returned nothing, so this is drawn as a single switch.")
    if 0 in budgets and not chassis and len(member_numbers) == 1:
        budgets[member_numbers[0]] = budgets.pop(0)       # a lone switch prints one 'Available:' line, no table
    port_history = _touch_history(history, now)

    members_out = []
    totals = {"active": 0, "inactive": 0, "unsure": 0, "up_now": 0, "total": 0}
    vlan_counts = {}
    for num in member_numbers:
        row = {} if chassis else switch_rows.get(num, {})
        vinfo = dict(version["members"].get(num, {})) if not chassis else {}
        if chassis and num in modules:
            vinfo["model"] = modules[num]["model"]
        uptime = vinfo.get("uptime") or version["uptime"]
        uptime_text = vinfo.get("uptime_text") or version["uptime_text"]
        covered_since = update_coverage(history, num, now, uptime)
        names = chosen.get(num, {})
        size = _port_count(max(names) if names else LAST_PORT, vinfo.get("model", ""))
        if chassis and num in modules:
            size = _port_count(max([min(modules[num]["ports"], LAST_PORT)] + list(names)))
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
                "speed": st.get("speed", ""), "duplex": st.get("duplex", ""), "ptype": st.get("type", ""),
                "activity": activity,
                "last_active": last_active.strftime("%Y-%m-%d") if last_active else "",
                "why": why, "up_now": connected,
                "admin_down": state == "disabled", "err": state.startswith("err-"),
            })
            budget = budgets.get(0) if chassis else budgets.get(num)
            _add_health(ports_out[-1], port_health(name, connected, st.get("speed", ""), st.get("duplex", ""),
                                                   st.get("type", ""), errors.get(name), power_ports.get(name),
                                                   budget, uptime), issue_count)
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
            "card": modules.get(num, {}).get("card", ""),
            "poe": None if chassis else budgets.get(num),
            "role": "" if chassis else row.get("role", "" if switch_rows else "Standalone"),
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

    _budget_warnings({0: budgets[0]} if chassis and 0 in budgets else
                     {n: b for n, b in budgets.items() if n in member_numbers}, "Slot" if chassis else "Switch", warnings)
    down = [f"Switch {m} port {i + 1}" for m, pair in sorted(stack_ports.items())
            for i, st in enumerate(pair) if st.upper() != "OK"]
    if down and len(member_numbers) > 1:
        warnings.insert(0, f"Stack ring is broken: {', '.join(down)} {'is' if len(down) == 1 else 'are'} "
                           f"DOWN. The stack still works as a half ring, but one more cable or switch "
                           f"failure will split it.")

    data = {
        "host": host, "where": where, "vendor": "cisco_ios", "vendor_name": VENDORS["cisco_ios"],
        "unit": "Slot" if chassis else "Switch", "units": "slots" if chassis else "members",
        "model": chassis_model, "poe": budgets.get(0) if chassis else None, "issues": issue_count,
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
    power_ports, budgets = parse_power_inline(outputs.get("power"), "dell_powerconnect")
    link_info = {}                       # port: (type, duplex, speed) from 'show interfaces status'
    for line in (outputs.get("status") or "").splitlines():
        words = line.split()
        if len(words) >= 4 and analyzer.DELL_PORT.match(line):
            link_info[normalize_intf(words[0])] = (words[1], words[2], words[3])
    issue_count = {"ports": 0, "power": 0, "slow": 0}
    tagged_seen = set()                  # VLANs ports carry tagged (the voice VLAN), so their names show too

    if not ports_by:
        raise ValueError("Read the switch, but no ports could be read. Is it a Dell PowerConnect 5500?")
    if not _front_panel(ports_by):
        raise ValueError(no_ports_message(ports_by))
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
            vlan, pvid, port_tagged = dell_port_vlans(prof, cfg is not None, untagged.get(name, set()),
                                                      tagged.get(name, set()), voice_id)
            tagged_seen.update(port_tagged)
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
            ptype, duplex, speed = link_info.get(name, ("", "", ""))
            ports_out.append({
                "num": port, "name": b["port"], "desc": b.get("description", ""),
                "status": state, "vlan": vlan, "ptype": ptype, "pvid": pvid, "tagged": port_tagged,
                "speed": speed if connected else "", "duplex": duplex if connected else "",
                "activity": activity,
                "last_active": last_active.strftime("%Y-%m-%d") if last_active else "",
                "why": why, "up_now": connected,
                "admin_down": state == "disabled", "err": False,
            })
            _add_health(ports_out[-1], port_health(name, connected, speed if connected else "", duplex if connected else "",
                                                   ptype, None, power_ports.get(name), budgets.get(num), uptime,
                                                   "dell_powerconnect"), issue_count)
            if cfg:
                ports_out[-1].update(ifname=cfg["ifname"], config=cfg["lines"], removed=cfg["removed"])
            prof = prof or basic_profile(ports_out[-1]["desc"], vlan)
            ports_out[-1].update(copy=port_copies("dell_powerconnect", cfg["lines"] if cfg else None, prof),
                                 basic=cfg is None)

        members_out.append({
            "number": num,
            "role": roles.get(num, "Standalone" if len(chosen) == 1 else ""),
            "state": "", "model": model, "poe": budgets.get(num),
            "uptime": human_span(uptime) if uptime else "",
            "port_count": size, "stack_ports": [], "counts": counts, "ports": ports_out,
        })
        for k in counts:
            summary[k] += counts[k]
        summary["total"] += counts["active"] + counts["inactive"] + counts["unsure"]

    _budget_warnings({n: b for n, b in budgets.items() if n in chosen}, "Switch", warnings)
    if counted:
        warnings.append(f"{counted} port(s) had traffic since the switch started {human_span(uptime) if uptime else ''} "
                        f"ago, but Dell switches don't record when, so they're Unsure. Each drawing remembers the "
                        f"counts: draw this stack again over the next weeks, and ports whose counts never move "
                        f"turn Inactive once that covers {months:g} months.")

    data = {
        "host": host, "where": where, "vendor": "dell_powerconnect", "vendor_name": VENDORS["dell_powerconnect"],
        "unit": "Switch", "units": "members", "model": "", "poe": None, "issues": issue_count,
        "hostname": system["hostname"] or outputs.get("prompt", "") or host,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "months": months, "summary": summary, "warnings": warnings,
        "vlans": _vlan_legend(vlan_counts), "members": members_out,
        "vlan_names": {v: vlan_names[v] for v in sorted(set(vlan_counts) | tagged_seen, key=lambda v: (len(v), v))
                       if v in vlan_names},
        "has_config": bool(configs), "links": [],
        "history_runs": history["runs"], "history_since": history["first_run"][:10],
    }
    return data, history


# ---------------------------------------------------------------------------
# Port health: power (PoE) problems and signs of a slow connection
# ---------------------------------------------------------------------------
ERRORS_MIN = 100        # a handful of damaged packets since the switch started is normal
ERRORS_WARN = 0.0001    # 0.01% of received packets damaged: worth a look
ERRORS_BAD = 0.001      # 0.1%: users will notice
DROPS_MIN, DROPS_WARN = 1000, 0.01
POE_LOW = 0.10          # less than 10% of a switch's PoE budget left
_POE_ADMIN = {"auto", "static", "never", "consumption"}


def parse_power_inline(output, vendor="cisco_ios"):
    """'show power inline' -> (ports, budgets).

    ports:   {port: {"state", "watts", "device", "cls"}}
    budgets: {member: {"available", "used", "remaining"}} in watts; member 0 = the whole chassis
    Cisco stacks print a Module table, a 4500 one 'Available: Used: Remaining:' line, a Dell 5548P a Unit table.
    """
    ports, budgets = {}, {}
    text = output or ""
    m = re.search(r"(?i)Available:\s*([\d.]+)\s*\(w\)\s*Used:\s*([\d.]+)\s*\(w\)\s*Remaining:\s*(-?[\d.]+)", text)
    if m:
        budgets[0] = {"available": float(m.group(1)), "used": float(m.group(2)), "remaining": float(m.group(3))}
    for line in text.splitlines():
        b = re.match(r"^\s*(\d{1,2})\s+([\d.]+)\s+([\d.]+)\s+(-?[\d.]+)\s*$", line)
        if b:                                               # Cisco: Module Available Used Remaining
            budgets[int(b.group(1))] = {"available": float(b.group(2)), "used": float(b.group(3)),
                                        "remaining": float(b.group(4))}
            continue
        d = re.match(r"(?i)^\s*(\d{1,2})\s+(?:on|off)\s+(\d+)\s*watts?\s+(\d+)\s*watts?", line)
        if d:                                               # Dell: Unit Power Nominal Consumed
            avail, used = float(d.group(2)), float(d.group(3))
            budgets[int(d.group(1))] = {"available": avail, "used": used, "remaining": avail - used}
            continue
        words = line.split()
        if len(words) < 3 or not _NAME_RE.match(words[0]):
            continue
        low = [w.lower() for w in words]
        admin = next((i for i in range(1, len(low)) if low[i] in _POE_ADMIN), None)
        if admin is None or admin + 1 >= len(words):
            continue
        state = low[admin + 1]
        if vendor == "dell_powerconnect":                   # port, powered device, state, status, priority, class
            device = " ".join(words[1:admin])
            rest = words[admin + 2:]
            cls = next((w for w in rest if re.fullmatch(r"(?i)class\d", w)), "")
            watts = None
        else:                                               # port, admin, oper, watts..., device, class[, max]
            rest = words[admin + 2:]
            nums = []
            while rest and re.fullmatch(r"\d+(?:\.\d+)?", rest[0]):
                nums.append(float(rest.pop(0)))
            if rest and re.fullmatch(r"\d+\.\d+", rest[-1]):
                rest.pop()                                  # Max watts (stacks print it last)
            cls = rest.pop() if rest and re.fullmatch(r"(?i)\d|n/a", rest[-1]) else ""
            device = " ".join(rest)
            watts = nums[-1] if nums else None              # a 4500 prints "from PS" then "to device"
        ports[normalize_intf(words[0])] = {"state": state, "watts": watts,
                                           "device": "" if device.lower() in ("n/a", "") else device,
                                           "cls": "" if cls.lower() == "n/a" else cls.lower().replace("class", "")}
    return ports, budgets


_ERR_FIELDS = [("in_packets", r"(\d+) packets input"), ("out_packets", r"(\d+) packets output"),
               ("in_errors", r"(\d+) input errors"), ("crc", r"(\d+) CRC"), ("late", r"(\d+) late collision"),
               ("drops", r"Total output drops:\s*(\d+)")]


def parse_ios_errors(output):
    """'show interfaces' -> {port: counters} for slow-connection signs, plus when counters were cleared."""
    ports, cur = {}, None
    for line in (output or "").splitlines():
        h = re.match(r"^(\S+) is (?:up|down|administratively down)", line)
        if h:
            cur = ports.setdefault(normalize_intf(h.group(1)), {})
            continue
        if cur is None:
            continue
        for key, rx in _ERR_FIELDS:
            m = re.search(rx, line)
            if m:
                cur[key] = int(m.group(1))
        m = re.search(r'Last clearing of "show interface" counters (\S+)', line)
        if m:
            cur["cleared"] = m.group(1)
    return ports


def _speed_mb(speed):
    m = re.search(r"(\d+)\s*([GM]?)", speed or "")
    if not m:
        return None
    return int(m.group(1)) * (1000 if m.group(2) == "G" else 1)


def _pct(x):
    return f"{x * 100:.2f}%" if x < 0.01 else f"{x * 100:.1f}%"


def port_health(name, connected, speed, duplex, ptype, err, poe, budget, uptime, vendor="cisco_ios"):
    """Problems worth a look on one port, in plain words: (issues, notes).

    issues: [{"level": "bad" | "warn", "kind": "power" | "slow", "title", "text"}]
    notes:  plain facts for the port panel (what it powers, its speed)
    """
    issues, notes = [], []

    def add(level, kind, title, text):
        issues.append({"level": level, "kind": kind, "title": title, "text": text})

    # ---- power ----
    if poe:
        state, budget_text = poe["state"], ""
        if budget:
            budget_text = f" This switch has {budget['remaining']:.0f} W left of {budget['available']:.0f} W."
        if state in ("faulty", "fault") or "fault" in state or "short" in state or "overload" in state:
            add("bad", "power", "PoE fault", "The switch saw a problem powering this device and stopped: often a damaged "
                "cable or wall jack, a short, or a faulty device. Try the cable test.")
        elif "deny" in state or "denied" in state:
            add("bad", "power", "PoE denied", "The switch refused to power this device because it doesn't have enough "
                "PoE power left." + budget_text)
        elif "err" in state:
            add("bad", "power", "PoE shut off", "The switch shut this port's power off after a power problem, such as "
                "the device drawing more than allowed.")
        elif state == "on":
            what = poe["device"] or "a device"
            bits = [f"{poe['watts']:g} W" if poe.get("watts") is not None else "", f"class {poe['cls']}" if poe.get("cls") else ""]
            notes.append("Powering " + what + "".join(" · " + b for b in bits if b))

    # ---- slow connection ----
    if err:
        if err.get("cleared", "never") == "never":
            since = f"since the switch started {human_span(uptime)} ago" if uptime else "since the switch started"
        else:
            ago, _ = parse_cisco_duration(err["cleared"])
            since = f"in the {human_span(ago)} since the counters were cleared" if ago else "since the counters were cleared"
        received = err.get("in_packets", 0)
        damaged = max(err.get("crc", 0), err.get("in_errors", 0))
        if damaged >= ERRORS_MIN and received and damaged / received >= ERRORS_WARN:
            rate = damaged / received
            add("bad" if rate >= ERRORS_BAD else "warn", "slow", "Receive errors",
                f"{damaged:,} damaged packets received ({_pct(rate)} of traffic) {since}. Usually a damaged cable, "
                f"a bad patch cord or wall jack, or a failing device. Try the cable test.")
        if err.get("late", 0) > 0:
            add("bad", "slow", "Late collisions",
                f"{err['late']:,} late collisions {since}: almost always a duplex mismatch, one end set to full "
                f"duplex and the other to auto. Set both ends the same way.")
        sent, dropped = err.get("out_packets", 0), err.get("drops", 0)
        if dropped >= DROPS_MIN and sent and dropped / (sent + dropped) >= DROPS_WARN:
            add("warn", "slow", "Output drops",
                f"{dropped:,} outgoing packets dropped ({_pct(dropped / (sent + dropped))}) {since}: more traffic "
                f"than the port or device can take, common on a device linked at 100 Mb or 10 Mb.")
    mb = _speed_mb(speed)
    if connected:
        if "half" in (duplex or "").lower():
            add("bad", "slow", "Half duplex", "Running at half duplex: slow, and usually a duplex mismatch or a bad "
                "cable. Devices should normally be full duplex.")
        if mb == 10:
            add("warn", "slow", "10 Mb link", "Linked at only 10 Mb: very slow. Usually a bad cable or a device stuck "
                "at 10 Mb. Try the cable test.")
        elif mb == 100 and (re.search(r"1000|1G", ptype or "") or name[:2] in ("Gi", "Tw", "Te")):
            notes.append("Linked at 100 Mb. Fine for older phones and printers; if this device should be gigabit, a "
                         "damaged wire pair often causes this. Try the cable test.")
        elif mb:
            notes.append(f"Linked at {mb // 1000 if mb >= 1000 else mb} {'Gb' if mb >= 1000 else 'Mb'}"
                         + (", full duplex" if "full" in (duplex or "").lower() else ""))
    return issues, notes


def _add_health(port_out, health, issue_count):
    issues, notes = health
    port_out["issues"], port_out["notes"] = issues, notes
    if issues:
        issue_count["ports"] += 1
        for i in issues:
            issue_count[i["kind"]] += 1


def _budget_warnings(budgets, unit, warnings):
    for num, b in sorted(budgets.items()):
        if b["available"] and b["remaining"] < POE_LOW * b["available"]:
            where = "This chassis" if num == 0 else f"{unit} {num}"
            warnings.append(f"{where} has only {max(b['remaining'], 0):.0f} W of PoE power left (of {b['available']:.0f} W). "
                            f"New phones or access points may be refused power.")


# ---------------------------------------------------------------------------
# Cable test (TDR) on one port, only when someone asks for it
# ---------------------------------------------------------------------------
TDR_WAIT = {"cisco_ios": 8, "dell_powerconnect": 3}     # seconds before the first look at the result
_pause = time.sleep                                     # tests swap this out
_PORT_ARG = re.compile(r"^[A-Za-z][A-Za-z-]*\d+(?:/\d+){1,2}$")
_STILL_RUNNING = re.compile(r"(?i)in progress|not completed|still running")
_TDR_STATUS = re.compile(
    r"(?i)\b(normal|ok|good|terminated|open(?: cable)?|short(?:ed| cable)?|impedance mis\w*|impedmis|"
    r"crosstalk|fault|fail(?:ed)?|not completed|not supported|no cable|unknown(?: test result)?|busy)\b")
_TDR_GOOD = {"normal", "ok", "good", "terminated"}


def parse_tdr(output):
    """'show cable-diagnostics tdr' (Catalyst stack, Catalyst 4500 or Dell) -> [{"pair", "length", "status"}].

    Length is in metres (None if not given). Dell gives one result for the whole cable, with no pair.
    """
    pairs = []
    for line in (output or "").splitlines():
        found = list(_TDR_STATUS.finditer(line))
        if not found or re.search(r"(?i)pair status|distance to fault|^\s*(?:interface|port)\s+(?:speed|result)", line):
            continue                                                     # nothing, or a column header
        clean = re.sub(r"\b\d+\s*[MG](?:bps)?\b", " ", line)          # drop speeds like 1000M / 1Gbps
        # The local pair is whichever comes first: "Pair A" (stacks) or "1-2" (4500); later ones are the far end
        candidates = [m for m in (re.search(r"Pair\s+([A-D])\b", clean), re.search(r"\b(1-2|3-6|4-5|7-8)\b", clean)) if m]
        pair = min(candidates, key=lambda m: m.start()) if candidates else None
        length = re.search(r"(?i)(\d+)\s*(?:\+/?-\s*\d+\s*)?(?:meters|metres|m)\b", clean)
        pairs.append({"pair": pair.group(1) if pair else "",
                      "length": int(length.group(1)) if length else None,
                      "status": found[-1].group(1).lower()})
    return pairs


def tdr_verdict(pairs, link_up):
    """("good" | "problem" | "unknown", what it means in plain words)."""
    if not pairs:
        return "unknown", "The switch didn't give a result. Try again, or run the test from the CLI."
    bad = [p for p in pairs if p["status"] not in _TDR_GOOD]
    lengths = [p["length"] for p in pairs if p["length"] is not None]
    if not bad:
        return "good", "Cable looks good" + (f": about {max(lengths)} m long." if lengths else ".")
    groups = {}
    for p in bad:
        kind = ("open" if p["status"].startswith("open") else "short" if p["status"].startswith("short") else
                "impedance" if "imped" in p["status"] else "unfinished" if p["status"] in ("not completed", "busy") else
                "unsupported" if p["status"] == "not supported" else "nocable" if p["status"] == "no cable" else "other")
        groups.setdefault((kind, p["length"], p["status"]), []).append(p["pair"])
    parts = []
    for (kind, length, status), names in groups.items():
        named = [n for n in names if n]
        who = ("All pairs are" if len(named) >= 4 else ("Pairs " + ", ".join(named) + " are") if len(named) > 1
               else f"Pair {named[0]} is" if named else "The cable is")
        where = f" at about {length} m" if length is not None else ""
        if kind == "open":
            far = (f" If the run is about {length} m long, nothing is plugged in at the far end (wall jack or device)."
                   if not link_up and length is not None else "")
            parts.append(f"{who} open{where}: a break or loose connection there.{far}")
        elif kind == "short":
            parts.append(f"{who} shorted{where}: wires touching. Re-terminate or replace the cable.")
        elif kind == "impedance":
            parts.append(f"{who} mismatched (impedance){where}: damaged or poor-quality cable, or a bad patch cord or "
                         f"jack there. Causes errors and slow speeds.")
        elif kind == "unfinished":
            parts.append("The test didn't finish" + (f" on {', '.join(named)}" if named else "") + ". Try again.")
        elif kind == "unsupported":
            parts.append("This port doesn't support the cable test (fiber and most 10G ports don't).")
        elif kind == "nocable":
            parts.append("No cable detected: nothing is plugged into this port.")
        else:
            parts.append(f"{who} reported '{status}'{where}.")
    return "problem", " ".join(parts)


def _dell_error_counters(output):
    """Per-port 'show interfaces counters <port>' on a Dell -> the error counters that aren't zero."""
    found = []
    for name, value in re.findall(r"([A-Za-z][A-Za-z /-]+?)\s*:\s*(\d+)", output or ""):
        if int(value) and re.search(r"(?i)error|fcs|align|collision|oversize|undersize|fragment|jabber|discard", name):
            found.append(f"{name.strip()}: {int(value):,}")
    return found


def run_cable_test(address, vendor, protocol, login, port, timeout=20, slow=False):
    """Run the cable test (TDR) on one port and read the result, plus that port's errors and power now.

    The link drops for a few seconds (about a minute on a 4500 line card), and a powered device restarts.
    """
    port = (port or "").strip()
    if not _PORT_ARG.fullmatch(port):
        raise ValueError("That isn't a port name.")
    cisco = vendor != "dell_powerconnect"
    started = datetime.now()
    with sw.connect(address, vendor, protocol, login, timeout) as conn:
        out = conn.send_command_timing(f"test cable-diagnostics tdr interface {port}", read_timeout=60)
        if re.search(r"(?i)yes/no", out):
            out += "\n" + conn.send_command_timing("yes", read_timeout=120)
        elif re.search(r"(?i)\[confirm\]", out):
            out += "\n" + conn.send_command_timing("\n", read_timeout=120)
        elif re.search(r"(?i)\(y/n\)|\[y/n\]|are you sure|continue\?", out):
            out += "\n" + conn.send_command_timing("y", read_timeout=120)
        # A '%' line is the switch saying no ("% TDR test is not supported..."); log messages like
        # "%LINK-3-UPDOWN:" are just the link dropping for the test.
        said_no = [ln.strip(" %") for ln in out.splitlines()
                   if ln.strip().startswith("%") and not re.match(r"\s*%[A-Z0-9_]+-\d-[A-Z0-9_]+:", ln)]
        if said_no or sw.ERROR_TEXT.search(out) or re.search(r"(?i)not supported|administratively down", out):
            return {"ok": False, "raw": out,
                    "problem": said_no[0] if said_no else "The switch wouldn't run the cable test on this port."}
        _pause(TDR_WAIT[vendor] * (6 if slow else 1))
        deadline = time.monotonic() + (90 if slow else 30)
        while True:
            result = conn.send_command(f"show cable-diagnostics tdr interface {port}", read_timeout=60)
            if not _STILL_RUNNING.search(result) or time.monotonic() > deadline:
                break
            _pause(4)
        counters = conn.send_command(f"show interfaces {port}" if cisco else f"show interfaces counters {port}",
                                     read_timeout=60)
        power = conn.send_command(f"show power inline {port}", read_timeout=60)

    pairs = parse_tdr(result) or parse_tdr(out)
    link_up = bool(re.search(r"(?im)\bis up, line protocol is up", counters)) if cisco else None
    verdict, summary = tdr_verdict(pairs, link_up)
    now_issues, now_notes = [], []
    if cisco:
        err = next(iter(parse_ios_errors(counters).values()), None)
        speed = re.search(r"(Full|Half)-duplex,\s*(\d+)\s*([MG])b/s", counters)
        now_issues, now_notes = port_health(normalize_intf(port), bool(link_up),
                                            f"{speed.group(2)}{speed.group(3)}" if speed else "",
                                            speed.group(1) if speed else "", "", err, None, None, None)
    else:
        found = _dell_error_counters(counters)
        if found:
            now_notes.append("Error counters: " + ", ".join(found))
    poe = next(iter(parse_power_inline(power, vendor)[0].values()), None)
    if poe:
        p_issues, p_notes = port_health(normalize_intf(port), False, "", "", "", None, poe, None, None)
        now_issues += p_issues
        now_notes += p_notes
    for issue in now_issues:                 # they've just run it
        issue["text"] = issue["text"].replace(" Try the cable test.", "")
    return {"ok": True, "verdict": verdict, "summary": summary, "pairs": pairs,
            "checked": started.strftime("%Y-%m-%d %H:%M"), "issues": now_issues, "notes": now_notes,
            "raw": "\n\n".join(x.strip() for x in (out, result) if x and x.strip())}


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
def collect_outputs(address, protocol, login, timeout=20, progress=print, vendor="cisco_ios", extra=None):
    """Log in once and run every command. A command the switch doesn't know gives ''.

    `extra` adds more {key: command, (commands to try) or function(conn) -> text} for other tools
    (Port documentation).
    """
    outputs = {}
    with sw.connect(address, vendor, protocol, login, timeout) as conn:
        outputs["prompt"] = sw.prompt_name(conn)
        for key, command in {**COMMANDS_FOR[vendor], **(extra or {})}.items():
            if callable(command):
                outputs[key] = command(conn)
                continue
            tries = list(command) if isinstance(command, tuple) else [command]
            progress(f"  {tries[0]}")
            try:
                _, outputs[key] = sw.run_first_working(conn, tries,
                                                       read_timeout=300 if key in ("interfaces", "config", "counters", "macs")
                                                       else 180)
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


def save_debug(debug_dir, address, vendor, outputs):
    """Keep what the switch printed, so a layout the page doesn't understand can be fixed.

    The running-config is cut down to the port sections, with keys and passwords
    already removed, like the page itself.
    """
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{re.sub(r'[^\w.-]', '_', address)}-topology-{vendor}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for key, text in outputs.items():
            if key == "config":
                text = "\n".join(f"interface {c['ifname']}\n" + "\n".join(c["lines"])
                                 for c in parse_interface_configs(text).values())
            f.write(f"===== {key} =====\n{text}\n\n")
    return path


def run_topology(address, protocol, login, months=6, timeout=20, data_dir=None, where="", progress=print,
                 vendor="cisco_ios"):
    """Read the stack, decide each port, remember what we saw, save the page.
    Returns (data, html, saved_path)."""
    data_dir = Path(data_dir or DEFAULT_DIR)
    outputs = collect_outputs(address, protocol, login, timeout, progress, vendor)
    try:
        save_debug(data_dir.parent / "debug", address, vendor, outputs)
    except OSError:
        pass                                # a full or read-only disk shouldn't stop the drawing
    data, history = build_stack(outputs, address, months, load_history(data_dir, address), where=where,
                                vendor=vendor)
    data["protocol"] = protocol
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
