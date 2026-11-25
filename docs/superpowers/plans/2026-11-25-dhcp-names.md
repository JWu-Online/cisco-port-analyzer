# Device tracker: names from DHCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Name the devices the Device tracker finds by the MAC table only, using host names from Windows DHCP leases.

**Architecture:** At each scan, after the switches are read, `tracker.py` runs one PowerShell command (the team's
`Get-DhcpServerv4Scope | Get-DhcpServerv4Lease`, per configured server) as the current Windows user, parses its JSON,
and fills Name (and an empty IP) by MAC before the scan is compared and saved. The servers are a setting on the page.

**Tech Stack:** Python 3.9+, Flask, PowerShell 5.1 with the DhcpServer module (RSAT), vanilla JS pages.

**Spec:** `docs/superpowers/specs/2026-11-25-dhcp-names-design.md`

## Global Constraints

- DHCP servers setting is **empty by default**; empty = no lookup. Comma-separated list.
- Only devices whose `found_by` is exactly `"MAC table"` get the DHCP name. LLDP/CDP names always win.
- Leases used: `AddressState` in `Active`, `ActiveReservation`, `InactiveReservation`; MAC ClientIds only
  (`^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$`). Several for one MAC: a reservation wins, else the latest expiry.
- Host name shortened to its first label (`pc123.lgcc.cuny.edu` -> `pc123`).
- The lease IP fills `ip` only when it's empty.
- Lookup timeout 120 s. Any failure: the scan still finishes and saves; the problem is logged and shown on the scan page.
- No password saved, no Desktop CSV, no "Renamed" change type, Windows only (elsewhere: skipped with a note).
- Python 3.9 syntax; pages stay ES5 + the existing `static/app.css` classes.

## Review Focus

- A server name with a quote, semicolon or space (`lgccdc1'; Remove-Item`) must be refused, never put in the script → test in Task 1.
- RSAT missing ("is not recognized") must say the DHCP PowerShell tools aren't installed, not a raw PowerShell error → Task 1.
- One of two servers down: leases from the other are still used, and the problem names the failed server → Task 1.
- A lease whose HostName is empty, or whose ClientId is a DUID, must not blank or break a device → Task 1 / Task 2.
- A DHCP lookup that times out must not lose the switch results → Task 2 (`run_scan` with a reader returning a problem).

---

### Task 1: The DHCP lookup

**Files:**
- Modify: `tracker.py` (new section after the SNMP-login section)
- Test: `tests/test_tracker.py` (new `test_dhcp_lookup()`, added to `main()`)

**Interfaces:**
- Produces:
  - `parse_servers(text: str) -> tuple[list[str], list[str]]` — (servers, problems). Splits on commas/whitespace;
    each must match `^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$`; duplicates dropped, order kept.
  - `read_dhcp(servers: list[str], run=None, timeout: int = 120) -> tuple[dict, str]` — ({mac: lease}, problem).
    `run(args: list[str], timeout: int) -> tuple[int, str, str]` (code, stdout, stderr) defaults to a subprocess call
    of `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command <script>` with no window.
    lease = `{"name", "ip", "state", "expires", "server", "scope"}`; mac in `aabb.ccdd.eeff`.
  - `pick_leases(rows: list[dict]) -> dict` — the filtering/choosing above, from the script's rows.
  - Module flag reuse: `IS_WINDOWS` (already exists); not Windows -> `({}, "DHCP names are only looked up on Windows.")`.

- [ ] **Step 1: Write the failing tests** `test_dhcp_lookup()`:
  - `parse_servers("lgccdc1, lgccdc2 lgccdc1")` == `(["lgccdc1", "lgccdc2"], [])`; `parse_servers("")` == `([], [])`;
    `parse_servers("dc1'; rm x")` has a problem and no server containing `'`.
  - With `IS_WINDOWS = True` and a fake `run` returning JSON rows
    `{"server","scope","ip","name","mac","state","expires"}` (`mac` as `AA-BB-CC-00-00-01`):
    Active `pc123.lgcc.cuny.edu` -> `leases["aabb.cc00.0001"]["name"] == "pc123"`; an `Expired` row ignored; for one MAC an
    Active expiring later beats one expiring sooner, and an `ActiveReservation` beats both; a row with `name: ""` keeps
    `name == ""`; a DUID `mac` ignored.
  - A row `{"server": "dc2", "error": "The term 'Get-DhcpServerv4Scope' is not recognized..."}` alongside dc1 rows:
    dc1 leases kept; problem mentions RSAT / "DHCP PowerShell tools".
  - `{"server": "dc2", "error": "Access is denied"}` -> problem mentions `dc2`.
  - Fake `run` raising `subprocess.TimeoutExpired` -> `({}, problem mentioning "2 minutes")`.
  - The script handed to `run` contains `'lgccdc1'` and never the refused name.
- [ ] **Step 2: Run** `python tests/test_tracker.py` — expect FAIL (`parse_servers` not defined).
- [ ] **Step 3: Implement** `parse_servers`, `pick_leases`, `read_dhcp` and the script builder in `tracker.py`.
  The PowerShell sets `$ErrorActionPreference='Stop'` and UTF-8 output, loops the servers (single-quoted, already
  validated) with a per-server `try/catch` adding `{server, error}`, applies the team's `ClientId` filter, emits
  strings (`[string]` IP/scope, `LeaseExpiryTime.ToString('s')`), and ends with `ConvertTo-Json -InputObject @($out) -Compress`.
- [ ] **Step 4: Run** `python tests/test_tracker.py` — expect ALL PASSED.
- [ ] **Step 5: Commit** `tracker.py tests/test_tracker.py` — "Device tracker: look up DHCP leases by MAC".

### Task 2: Using the names in a scan

**Files:**
- Modify: `tracker.py` (`devices_from`, new `apply_dhcp`, `DEVICE_COLUMNS`, `device_rows`, `page_data`, `run_scan`,
  `DEFAULT_SETTINGS`)
- Test: `tests/test_tracker.py` (`test_where`, `test_scan_job`)

**Interfaces:**
- Consumes: `read_dhcp`, `parse_servers` (Task 1).
- Produces:
  - `DEFAULT_SETTINGS["dhcp_servers"] = ""`.
  - Each device gains `name_from: str` — the neighbor's `found_by` (`"LLDP"`, `"CDP"`, `"CDP + LLDP"`) when the name
    came from a neighbor, `"DHCP"` when from a lease, `""` when none.
  - `apply_dhcp(devices: dict, leases: dict) -> int` — names filled.
  - `run_scan(..., dhcp_reader=None)` — `dhcp_reader(servers) -> (leases, problem)`, default `read_dhcp`. Called only
    when `parse_servers(settings["dhcp_servers"])` gives servers; before `compare()`.
  - Summary/page key `"dhcp": {"servers": [...], "leases": int, "named": int, "problem": str}` (`None` when no servers).
  - `DEVICE_COLUMNS` gets `("Name from", "name_from")` right after `("Name", "name")`; rows carry `name_from`.

- [ ] **Step 1: Write the failing tests**:
  - `apply_dhcp` on the sample scan with leases for `S.PRINTER` (`name "PRN-C449"`, `ip "10.0.10.99"`), `S.WIN_PC`
    (`"OTHER"`) and `S.PHONE_C`: printer name `"PRN-C449"`, `name_from "DHCP"`, IP stays `"10.0.10.24"` (ARP already had
    one); a MAC-only device with no IP gets the lease IP; `WIN_PC` keeps `"DESKTOP-7Q2"` with `name_from "LLDP"`; the
    phone keeps its CDP name; return value counts only filled names.
  - A lease with `name ""` leaves the device nameless and isn't counted.
  - `run_scan` with `settings["dhcp_servers"] = "dc1"` and a fake reader: the saved `scan.json` has
    `dhcp == {"servers": ["dc1"], "leases": 1, "named": 1, "problem": ""}`, the history entry for the printer has the
    DHCP name, `devices.csv` header has `Name from` after `Name`.
  - `run_scan` with a reader returning `({}, "took over 2 minutes")`: state `"done"`, scan saved, a `warn` log line
    containing the problem.
  - `dhcp_servers ""`: reader never called, `dhcp` is `None`.
- [ ] **Step 2: Run** `python tests/test_tracker.py` — expect FAIL.
- [ ] **Step 3: Implement** as in Interfaces. Log `"DHCP: N leases from <servers>, M devices named."` (`ok`) or the
  problem (`warn`).
- [ ] **Step 4: Run** `python tests/test_tracker.py` — expect ALL PASSED.
- [ ] **Step 5: Commit** — "Device tracker: name MAC-only devices from DHCP".

### Task 3: The setting, the Test button and the pages

**Files:**
- Modify: `app.py` (`tracker_setup_save`, new `POST /api/tracker/dhcp-test`), `templates/tracker.html`,
  `templates/tracker_view.html`, `README.md` (Device tracker section)
- Test: `tests/test_tracker.py` (`test_app`)

**Interfaces:**
- Consumes: `parse_servers`, `read_dhcp`, `DEFAULT_SETTINGS["dhcp_servers"]`, page key `dhcp`, row key `name_from`.
- Produces: `POST /api/tracker/setup` accepts `dhcp_servers` (saved as `", ".join(servers)`; problems -> 400).
  `POST /api/tracker/dhcp-test {"servers": text}` -> `{"ok", "leases": int, "problem": str}` (400 on bad/empty names).

- [ ] **Step 1: Write the failing tests** in `test_app`: GET setup returns `dhcp_servers == ""` on a fresh data folder;
  POST `{"dhcp_servers": "dc1, dc2"}` saves `"dc1, dc2"`; POST `{"dhcp_servers": "dc1;x"}` -> 400; dhcp-test with
  `tracker.read_dhcp` swapped for a fake -> `{"ok": true, "leases": 2}`; dhcp-test with `""` -> 400;
  `/tracker/view/<run>` and `/tracker/demo` still 200.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** the routes; on `tracker.html` a panel "Names from DHCP" (text field, empty, placeholder
  `lgccdc1, lgccdc2`, Save and Test buttons, a hint that it runs as your Windows account and needs the DHCP PowerShell
  tools); on `tracker_view.html` a "Name from" column in Every device, a muted `DHCP` tag next to DHCP names in both
  tables' rows where `name_from == "DHCP"` (changes rows get `name_from` from the device row via `key`), the subtitle
  adds "M names from DHCP", and a `warn` notice when `dhcp.problem`. Demo: `tracker_demo` sets `dhcp_servers` and a
  fake reader so the demo shows DHCP names. README: what the field does, RSAT + read access needed, the failure note.
- [ ] **Step 4: Run** all suites (`test_tracker`, `test_saved`, `test_lldp`, `test_network`, `test_portdocs`,
  `test_end_to_end`) — ALL PASSED; screenshot `/tracker` and `/tracker/demo` (light) and check the new panel/column.
- [ ] **Step 5: Commit** — "Device tracker: DHCP servers setting, Test, Name from column".
