"""
Reading a switch over SNMP: ask for a table and get every row back.

Read-only: this only ever sends GETBULK requests (reads); it can't change anything on a switch.
SNMP v2c (a community string) and v3 (a username with its own passwords, encrypted) both work.

    with Session("10.0.1.11", creds) as s:
        rows = s.walk("1.3.6.1.2.1.31.1.1.1.1")      # ifName: [((index...), value), ...]

Each row is (the numbers after the table's OID, the value). Text comes back as bytes, numbers as int,
IP addresses as "10.0.1.5".
"""

import asyncio

V2C, V3 = "2c", "3"

# What the switches call them -> pysnmp's names (looked up when a session starts, so the app still runs without it)
AUTH = {"SHA": "usmHMACSHAAuthProtocol", "SHA-256": "usmHMAC192SHA256AuthProtocol",
        "SHA-384": "usmHMAC256SHA384AuthProtocol", "SHA-512": "usmHMAC384SHA512AuthProtocol",
        "MD5": "usmHMACMD5AuthProtocol", "none": "usmNoAuthProtocol"}
PRIV = {"AES": "usmAesCfb128Protocol", "AES-192": "usmAesCfb192Protocol", "AES-256": "usmAesCfb256Protocol",
        "DES": "usmDESPrivProtocol", "none": "usmNoPrivProtocol"}


class SnmpError(Exception):
    pass


def _hl():
    try:
        from pysnmp.hlapi.v3arch import asyncio as hl
    except ImportError as exc:
        raise SnmpError("The SNMP library (pysnmp) isn't installed. Close the app and start it again with "
                        "RunAnalyzer.bat; it installs new libraries by itself. (Needs Python 3.9 or newer.)") from exc
    return hl


def parse_oid(oid):
    return tuple(int(p) for p in str(oid).strip().strip(".").split("."))


def _value(val, hl):
    """A pysnmp value -> bytes, int or str. None for 'no such object' / end of the table."""
    if isinstance(val, (hl.NoSuchObject, hl.NoSuchInstance, hl.EndOfMibView)):
        return None
    if isinstance(val, hl.IpAddress):
        return ".".join(str(b) for b in val.asOctets())
    if isinstance(val, hl.OctetString):
        return val.asOctets()
    if isinstance(val, hl.ObjectIdentifier):
        return ".".join(str(n) for n in val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return str(val)


class Session:
    """One switch. Use it from one thread (each switch gets its own)."""

    def __init__(self, host, creds, timeout=3, retries=1, port=161, max_repetitions=25):
        self.hl = hl = _hl()
        self.host, self.creds, self.max_rep = host, creds, max_repetitions
        version = creds.get("version")
        if version not in (V2C, V3):
            raise SnmpError("Pick SNMP version 2c or 3.")
        if version == V2C and not creds.get("community"):
            raise SnmpError("Enter the SNMP community string.")
        if version == V3:
            if not creds.get("username"):
                raise SnmpError("Enter the SNMPv3 username.")
            if creds.get("auth_protocol", "SHA") not in AUTH or creds.get("priv_protocol", "AES") not in PRIV:
                raise SnmpError("Unknown SNMPv3 authentication or privacy type.")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)           # pysnmp works on this thread's loop
        try:
            self.engine = self.loop.run_until_complete(self._engine())
            self.target = self.loop.run_until_complete(
                hl.UdpTransportTarget.create((host, int(port)), timeout=timeout, retries=retries))
        except Exception as exc:
            self.close()
            raise SnmpError(f"Can't reach {host}: {exc}") from exc
        self._auth = {}

    async def _engine(self):
        return self.hl.SnmpEngine()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            if getattr(self, "engine", None) is not None:
                self.engine.close_dispatcher()
        except Exception:   # noqa: BLE001 - closing never matters more than the answer we already have
            pass
        if not self.loop.is_closed():
            self.loop.close()
        asyncio.set_event_loop(None)

    def _auth_for(self, vlan):
        """Cisco keeps one MAC table per VLAN: 'community@10' (v2c) or the context 'vlan-10' (v3)."""
        if vlan in self._auth:
            return self._auth[vlan]
        hl, c = self.hl, self.creds
        if c["version"] == V2C:
            community = c["community"] + (f"@{vlan}" if vlan is not None else "")
            auth, ctx = hl.CommunityData(community, mpModel=1), hl.ContextData()
        else:
            auth_proto = c.get("auth_protocol") or "SHA"
            priv_proto = c.get("priv_protocol") or "AES"
            auth = hl.UsmUserData(
                c["username"],
                authKey=c.get("auth_password") or None if auth_proto != "none" else None,
                privKey=c.get("priv_password") or None if priv_proto != "none" else None,
                authProtocol=getattr(hl, AUTH[auth_proto]), privProtocol=getattr(hl, PRIV[priv_proto]))
            ctx = hl.ContextData(contextName=(f"vlan-{vlan}" if vlan is not None else "").encode())
        self._auth[vlan] = (auth, ctx)
        return auth, ctx

    def walk(self, oid, vlan=None, max_rows=500000):
        """Every row under `oid`: [(index tuple, value)]. [] when the switch doesn't have that table."""
        return self.loop.run_until_complete(self._walk(parse_oid(oid), vlan, max_rows))

    async def _walk(self, base, vlan, max_rows):
        hl = self.hl
        auth, ctx = self._auth_for(vlan)
        rows, last = [], base
        while True:
            err_ind, err_status, _err_index, var_binds = await hl.bulk_cmd(
                self.engine, auth, self.target, ctx, 0, self.max_rep,
                hl.ObjectType(hl.ObjectIdentity(".".join(map(str, last)))), lookupMib=False)
            if err_ind:
                raise SnmpError(_explain(str(err_ind), self.creds))
            if err_status:
                if str(err_status) in ("noSuchName", "2"):         # an old agent's way of saying 'no more'
                    return rows
                raise SnmpError(f"The switch answered with an error: {err_status.prettyPrint()}")
            if not var_binds:
                return rows
            for vb in var_binds:
                name, val = vb[0], vb[1]
                oid = tuple(name)
                if oid[:len(base)] != base or isinstance(val, hl.EndOfMibView):
                    return rows
                if oid <= last:                                     # going backwards: stop instead of looping
                    return rows
                value = _value(val, hl)
                if value is not None:
                    rows.append((oid[len(base):], value))
                last = oid
                if max_rows and len(rows) >= max_rows:
                    return rows


def _explain(error, creds):
    low = error.lower()
    if "timeout" in low or "no snmp response" in low:
        if creds.get("version") == V2C:
            return ("No answer. Check that SNMP is on, the community string is right and the switch lets this PC "
                    "ask (an SNMP access list).")
        return ("No answer. Check that SNMP is on, the username and passwords are right and the switch lets this "
                "PC ask (an SNMP access list).")
    if "unknown" in low and "user" in low:
        return "The switch doesn't know that SNMPv3 username."
    if "digest" in low:
        return "Wrong SNMPv3 authentication password or type (SHA, MD5...)."
    if "decrypt" in low or "cipher" in low:
        return ("Wrong SNMPv3 privacy (encryption) password or type (AES, DES...). (Or this PC is missing the "
                "'cryptography' library: start the app with RunAnalyzer.bat to install it.)")
    if "unsupported security level" in low or "unsupportedseclevel" in low:
        return "The switch expects a different SNMPv3 security level (with or without encryption)."
    return error
