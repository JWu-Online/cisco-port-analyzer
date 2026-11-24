"""
Made-up switches as SNMP tables, for the Device tracker tests.

Each switch is {context: {oid: value}}: context None is the normal one, a number is Cisco's per-VLAN MAC table
(community@10 on v2c, context vlan-10 on v3). Values: int, bytes, or ("ip", "1.2.3.4").

walker(switch) gives a walk() like snmpwalk.Session's; snmprec(table) writes a file for the snmpsim simulator.
"""

import tracker as T


def _mac(text):
    return bytes.fromhex(text.replace(".", "").replace(":", ""))


def _idx_mac(text):
    return ".".join(str(b) for b in _mac(text))


def _octets(text):
    return f"{len(text)}." + ".".join(str(b) for b in text.encode())


# ---- addresses used below ----
PHONE_C = "0011.2233.4401"      # Cisco phone on C449 Gi1/0/1 (CDP), PC behind it
PC_BEHIND_PHONE = "a4bb.6d00.0001"
WIN_PC = "a4bb.6d00.0002"        # Windows PC on Gi1/0/2: LLDP with its computer name, and its MAC
AP_MAC = "7069.5a00.0003"        # access point on Gi1/0/3, wireless clients behind it
AP_CLIENT = "f0d5.bf00.0099"
PRINTER = "0017.c800.0004"       # says nothing, MAC table only, Gi1/0/4
UPLINK_MAC = "00de.fb00.0100"    # seen through the uplink Gi1/1/1
PHONE_D = "0004.f200.0011"       # Polycom on the Dell, LLDP-MED
CAMERA = "accc.8e00.0012"        # MAC only on the Dell


def cisco():
    d, v10, v40 = {}, {}, {}
    d[T.SYS_NAME + ".0"] = b"C449_2960S.lgcc.cuny.edu"
    ports = {1: "Gi1/0/1", 2: "Gi1/0/2", 3: "Gi1/0/3", 4: "Gi1/0/4", 5: "Gi1/1/1", 6: "Vl10"}
    for i, n in ports.items():
        d[f"{T.IF_NAME}.{i}"] = n.encode()
    d[f"{T.IF_ALIAS}.1"] = b"C449-12 phone"
    d[f"{T.IF_ALIAS}.4"] = b"C449 printer"
    d[f"{T.IF_ALIAS}.5"] = b"uplink to core"
    # LLDP: local ports named, the AP and the Windows PC
    for i in range(1, 6):
        d[f"{T.LLDP_LOC_PORT}.2.{i}"] = 5
        d[f"{T.LLDP_LOC_PORT}.3.{i}"] = ports[i].encode()
    rem = T.LLDP_REM
    d[f"{rem}.4.0.3.1"] = 4
    d[f"{rem}.5.0.3.1"] = _mac(AP_MAC)
    d[f"{rem}.6.0.3.1"] = 5
    d[f"{rem}.7.0.3.1"] = b"GigabitEthernet0"
    d[f"{rem}.9.0.3.1"] = b"AP-C449-1"
    d[f"{rem}.10.0.3.1"] = b"Cisco AP Software, C9120AXI-B\nCopyright"
    d[f"{rem}.12.0.3.1"] = bytes([0x30])           # bridge + WLAN access point
    d[f"{T.LLDP_REM_ADDR}.0.3.1.1.4.10.0.50.3"] = 2
    d[f"{rem}.4.0.2.1"] = 7
    d[f"{rem}.5.0.2.1"] = b"DESKTOP-7Q2"
    d[f"{rem}.6.0.2.1"] = 3
    d[f"{rem}.7.0.2.1"] = _mac(WIN_PC)
    # CDP: the phone and the core
    cdp = T.CDP_CACHE
    d[f"{cdp}.4.1.1"] = bytes([10, 0, 40, 11])
    d[f"{cdp}.6.1.1"] = b"SEP001122334401"
    d[f"{cdp}.7.1.1"] = b"Port 1"
    d[f"{cdp}.8.1.1"] = b"Cisco IP Phone 8845"
    d[f"{cdp}.9.1.1"] = bytes([0, 0, 0, 0x90])
    d[f"{cdp}.4.5.1"] = bytes([10, 0, 0, 1])
    d[f"{cdp}.6.5.1"] = b"CORE-1.lgcc.cuny.edu(FOX123)"
    d[f"{cdp}.7.5.1"] = b"Ethernet4/1"
    d[f"{cdp}.8.5.1"] = b"N7K-C7010"
    d[f"{cdp}.9.5.1"] = bytes([0, 0, 0, 0x29])
    # VLANs (VTP), access VLAN per port, ARP
    for vlan in (1, 10, 40, 1002):
        d[f"{T.CISCO_VLANS}.1.{vlan}"] = 1
    for i, vlan in ((1, 10), (2, 10), (3, 10), (4, 10)):
        d[f"{T.CISCO_PORT_VLAN}.{i}"] = vlan
    d[f"{T.ARP}.6.10.0.10.24"] = _mac(PRINTER)
    # The MAC table, one per VLAN (bridge port n = ifIndex n)
    for table in (v10, v40):
        for i in range(1, 6):
            table[f"{T.BRIDGE_PORTS}.{i}"] = i
    for mac, port in ((PC_BEHIND_PHONE, 1), (WIN_PC, 2), (AP_MAC, 3), (AP_CLIENT, 3), (PRINTER, 4), (UPLINK_MAC, 5)):
        v10[f"{T.BRIDGE_FDB}.1.{_idx_mac(mac)}"] = _mac(mac)
        v10[f"{T.BRIDGE_FDB}.2.{_idx_mac(mac)}"] = port
        v10[f"{T.BRIDGE_FDB}.3.{_idx_mac(mac)}"] = 3
    v10[f"{T.BRIDGE_FDB}.1.{_idx_mac('0011.2233.9999')}"] = _mac("0011.2233.9999")   # the switch itself
    v10[f"{T.BRIDGE_FDB}.2.{_idx_mac('0011.2233.9999')}"] = 0
    v10[f"{T.BRIDGE_FDB}.3.{_idx_mac('0011.2233.9999')}"] = 4
    v40[f"{T.BRIDGE_FDB}.1.{_idx_mac(PHONE_C)}"] = _mac(PHONE_C)
    v40[f"{T.BRIDGE_FDB}.2.{_idx_mac(PHONE_C)}"] = 1
    v40[f"{T.BRIDGE_FDB}.3.{_idx_mac(PHONE_C)}"] = 3
    return {None: d, 10: v10, 40: v40, 1: {}}


def dell():
    d = {}
    d[T.SYS_NAME + ".0"] = b"B2-IDF"
    names = {49: "Gi1/0/1", 50: "Gi1/0/2", 51: "Gi1/0/3", 100: "Te1/0/1"}
    for i, n in names.items():
        d[f"{T.IF_NAME}.{i}"] = n.encode()
    for bport, ifindex in ((1, 49), (2, 50), (3, 51), (24, 100)):
        d[f"{T.BRIDGE_PORTS}.{bport}"] = ifindex
        d[f"{T.QBRIDGE_PVID}.{bport}"] = 20
    # LLDP numbers its ports by ifIndex here, and names them by MAC: the ifName is used
    for i in names:
        d[f"{T.LLDP_LOC_PORT}.2.{i}"] = 3
        d[f"{T.LLDP_LOC_PORT}.3.{i}"] = _mac("f8b1.5600.0000")
    rem = T.LLDP_REM
    d[f"{rem}.4.0.49.1"] = 4
    d[f"{rem}.5.0.49.1"] = _mac(PHONE_D)
    d[f"{rem}.6.0.49.1"] = 3
    d[f"{rem}.7.0.49.1"] = _mac(PHONE_D)
    d[f"{rem}.9.0.49.1"] = b""
    d[f"{rem}.10.0.49.1"] = b"Polycom;VVX-411;3111-48450-001,7;"
    d[f"{rem}.12.0.49.1"] = bytes([0x24])          # bridge + telephone
    d[f"{rem}.4.0.100.2"] = 4
    d[f"{rem}.5.0.100.2"] = _mac("00de.fb00.0001")
    d[f"{rem}.6.0.100.2"] = 5
    d[f"{rem}.7.0.100.2"] = b"Eth4/2"
    d[f"{rem}.9.0.100.2"] = b"CORE-2"
    d[f"{rem}.12.0.100.2"] = bytes([0x28])         # bridge + router
    # Q-BRIDGE: fdb id 1 is VLAN 20, fdb id 2 is VLAN 40
    d[f"{T.QBRIDGE_VLAN_FDB}.0.20"] = 1
    d[f"{T.QBRIDGE_VLAN_FDB}.0.40"] = 2
    for fdb, mac, bport in ((2, PHONE_D, 1), (1, CAMERA, 2), (1, UPLINK_MAC, 24)):
        d[f"{T.QBRIDGE_FDB}.2.{fdb}.{_idx_mac(mac)}"] = bport
        d[f"{T.QBRIDGE_FDB}.3.{fdb}.{_idx_mac(mac)}"] = 3
    return {None: d}


def _python(value):
    if isinstance(value, tuple) and value[0] == "ip":
        return value[1]
    return value


def _key(oid):
    return tuple(int(p) for p in oid.split("."))


def walker(switch, fail_vlans=()):
    """A walk(oid, vlan=None) that reads the made-up tables like a switch would."""
    def walk(oid, vlan=None):
        if vlan in fail_vlans:
            raise T.snmpwalk.SnmpError("No answer.")
        table = switch.get(vlan, {})
        base = _key(oid)
        rows = [(_key(k)[len(base):], _python(v)) for k, v in table.items() if _key(k)[:len(base)] == base
                and len(_key(k)) > len(base)]
        return sorted(rows)
    return walk


def snmprec(table):
    """The table as an snmpsim .snmprec file (OID|type|value, in OID order)."""
    lines = []
    for oid in sorted(table, key=_key):
        v = table[oid]
        if isinstance(v, tuple) and v[0] == "ip":
            lines.append(f"{oid}|64|{v[1]}")
        elif isinstance(v, int):
            lines.append(f"{oid}|2|{v}")
        else:
            lines.append(f"{oid}|4x|{v.hex()}")
    return "\n".join(lines) + "\n"


SWITCHES = [
    {"address": "10.0.1.11", "type": "cisco_ios", "protocol": "telnet", "building": "C Building",
     "location": "C449", "name": ""},
    {"address": "10.0.2.12", "type": "dell_powerconnect", "protocol": "telnet", "building": "B Building",
     "location": "B2 IDF", "name": "B2-IDF"},
    {"address": "10.0.0.1", "type": "cisco_nxos", "protocol": "ssh", "building": "Main", "location": "MDF",
     "name": "CORE-1"},
]
