# Device tracker: names from DHCP (design)

Approved by Jackie on 2026-11-25. A small change.

## Why

The Device tracker finds many devices by the MAC table only (printers, cameras, most PCs). Those have no name, so a
"Moved" or "New" row only shows a MAC address. The Windows DHCP server knows each MAC's host name. Matching the two
names those devices.

## What

- **Setting:** a "DHCP servers" field on the Device tracker page, **empty by default** (no server is built in). One or
  more servers, separated by commas. Empty = no DHCP lookup. A **Test** button shows how many leases came back.
- **When:** every scan (by hand and the daily one), after the switches are read. The app runs the same lookup as the
  team's command, as the Windows user running the scan (no password saved):
  `Get-DhcpServerv4Scope -ComputerName <server> | Get-DhcpServerv4Lease -ComputerName <server>`, keeping only leases
  whose ClientId is a MAC address (six hex pairs). It reads the result directly; no Desktop CSV.
- **Matching:** by MAC address (`aabb.ccdd.eeff`, the tracker's own format). A device found **by the MAC table only**
  (no LLDP/CDP name) gets the lease's host name as its Name. LLDP/CDP names always win.
- **Calls made (Claude's, approved):**
  - Only Active leases and reservations (Active / ActiveReservation / InactiveReservation); expired or declined leases
    are ignored. If one MAC has several, the newest lease wins (latest expiry; a reservation counts as newest).
  - The host name is shortened to its first part (`pc123.lgcc.cuny.edu` -> `pc123`), like switch names.
  - If the switches gave no IP for the device, the lease's IP fills it in.
  - A **Name from** column (LLDP, CDP or DHCP) on the scan page and in devices.csv.
- **If the lookup fails** (DHCP PowerShell tools not installed, no access, server down, over 2 minutes): the scan
  still finishes and is saved; the log and the scan page say names weren't added from DHCP and why. Not Windows:
  skipped with a note.

## Not doing

- No Desktop CSV, no separate DHCP export.
- No "Renamed" change type; a new name for the same MAC just updates the name.
- No DHCPv6, no other DHCP products.

## Approaches considered

1. **The app runs the PowerShell lookup at each scan (picked).** Always current, nothing extra to schedule, runs as the
   same Windows account as the scan. Needs the DHCP Server PowerShell module (RSAT) on the PC, which the team has.
2. Read a CSV exported by the team's command. Names only as fresh as the last export, and a second job to keep running.
3. Query Microsoft DHCP from Python directly. No maintained library for it; RSAT already works.

## Left to the builder

PowerShell invocation details (JSON output, no profile, hidden window, timeout), where the matching lives in
tracker.py, page and CSV layout of the new column, tests with a stand-in for PowerShell.
