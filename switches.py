"""
switches.py - logging into the switch types we use, over SSH or telnet.

Each vendor speaks a slightly different "dialect": different login prompts,
different commands, different output layouts. Every switch in the list is
tagged with its type so the app knows which dialect to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TYPES = {
    # value: short name shown in the app
    "cisco_ios": "Cisco Catalyst (IOS / IOS-XE)",
    "cisco_nxos": "Cisco Nexus (NX-OS)",
    "dell_powerconnect": "Dell PowerConnect (5548P)",
    "aruba_mas": "Aruba Mobility Access (S3500)",
}

MODELS = {
    "cisco_ios": "Catalyst 2960S/X, 3560, 3650, 3750, 4506/4507, 4948, 9200L and most other Catalyst switches",
    "cisco_nxos": "Nexus 7000 and 9000 (N7k, N9k)",
    "dell_powerconnect": "Dell PowerConnect 5500 series: 5524, 5548, 5548P",
    "aruba_mas": "Aruba Mobility Access Switches running ArubaOS: S1500, S2500, S3500",
}

# Other names people write for the same thing (model names work too)
ALIASES = {
    "cisco": "cisco_ios", "ios": "cisco_ios", "cisco_xe": "cisco_ios", "catalyst": "cisco_ios",
    "nxos": "cisco_nxos", "nexus": "cisco_nxos", "n7k": "cisco_nxos", "n9k": "cisco_nxos",
    "dell": "dell_powerconnect", "powerconnect": "dell_powerconnect", "5548p": "dell_powerconnect",
    "5548": "dell_powerconnect", "5524": "dell_powerconnect",
    "s3500": "aruba_mas", "s2500": "aruba_mas", "s1500": "aruba_mas", "aruba_os": "aruba_mas",
    "mas": "aruba_mas",
}
for _model in ("2960", "2960s", "2960x", "3560", "3560g", "3560cg", "3650", "3750", "3750g",
               "4506", "4506e", "4507", "4948", "c4948", "9200", "9200l", "c9200l"):
    ALIASES[_model] = "cisco_ios"

# Netmiko driver for each type over SSH and over telnet. ArubaOS has no telnet
# driver, so it logs in over a generic telnet session and then switches to the
# ArubaOS driver, which works fine on a telnet session once logged in.
DRIVERS = {
    "cisco_ios": {"ssh": "cisco_ios", "telnet": "cisco_ios_telnet"},
    "cisco_nxos": {"ssh": "cisco_nxos", "telnet": "cisco_nxos_telnet"},
    "dell_powerconnect": {"ssh": "dell_powerconnect", "telnet": "dell_powerconnect_telnet"},
    "aruba_mas": {"ssh": "aruba_os", "telnet": "generic_telnet"},
}

ERROR_TEXT = re.compile(
    r"(?i)(invalid input|invalid command|unrecognized command|incomplete command|"
    r"syntax error|% error|command not found|ambiguous command|% unknown command|% parse error)"
)


def clean_type(value):
    """Return a known type for whatever the user wrote, or None."""
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    v = ALIASES.get(v, v)
    return v if v in TYPES else None


def clean_protocol(value):
    v = (value or "").strip().lower()
    return v if v in ("ssh", "telnet") else None


@dataclass
class Login:
    username: str = ""
    password: str = ""
    secret: str = ""          # enable password; blank = same as password


def split_address(address):
    """'10.0.0.1' -> ('10.0.0.1', None); '10.0.0.1:2323' -> ('10.0.0.1', 2323)."""
    address = address.strip()
    if address.count(":") == 1:
        host, port = address.split(":")
        if port.isdigit():
            return host.strip(), int(port)
    return address, None


def connect(address, vtype, protocol, login, timeout=20):
    """Log in and return a ready Netmiko connection (privileged if possible)."""
    from netmiko import ConnectHandler        # imported here so parsing works without it

    host, port = split_address(address)
    driver = DRIVERS[vtype][protocol]
    params = dict(
        device_type=driver, host=host,
        username=login.username, password=login.password,
        secret=login.secret or login.password,
        conn_timeout=timeout, auth_timeout=timeout, banner_timeout=timeout,
        fast_cli=False,
    )
    if port:
        params["port"] = port
    conn = ConnectHandler(**params)
    if driver == "generic_telnet":
        from netmiko import redispatch
        conn.std_login()
        redispatch(conn, device_type=DRIVERS[vtype]["ssh"])
    if conn.find_prompt().strip().endswith(">"):
        try:
            conn.enable()
        except Exception:
            pass                              # carry on with what this login can see
    return conn


def prompt_name(conn):
    """'C311-SW1#' -> 'C311-SW1'; '(S3500-M205) #' -> 'S3500-M205'."""
    return conn.find_prompt().strip().rstrip("#>").strip().strip("()").strip()


def run_first_working(conn, commands, read_timeout=180):
    """Send each command until one isn't rejected. Returns (command, output)."""
    for cmd in commands:
        out = conn.send_command(cmd, read_timeout=read_timeout)
        if not ERROR_TEXT.search(out[:400]):
            return cmd, out
    return None, ""


def friendly_error(exc):
    text = str(exc).strip() or exc.__class__.__name__
    first = text.splitlines()[0]
    lowered = text.lower()
    if "authentication" in lowered or "login failed" in lowered:
        return "login failed (check the username and password)"
    if "timed out" in lowered or "timeout" in lowered:
        return "no answer (wrong address, wrong SSH/telnet choice, or a firewall)"
    if "refused" in lowered:
        return "connection refused (is SSH/telnet turned on for that switch?)"
    return first[:200]
