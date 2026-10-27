"""Made-up switch output in each vendor's layout, used by the tests and the pretend switches."""

IOS_VERSION = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8
C-IDF4-SW1 uptime is 1 year, 23 weeks, 4 days, 2 hours, 11 minutes
System returned to ROM by power-on
"""

IOS_INTERFACES = """Vlan1 is up, line protocol is up
  Hardware is EtherSVI, address is 0011.2233.4455 (bia 0011.2233.4455)
  Last input 00:00:00, output 00:00:00, output hang never
GigabitEthernet0/0 is down, line protocol is down
  Last input never, output never, output hang never
GigabitEthernet1/0/1 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 7c69.f6a1.b201 (bia 7c69.f6a1.b201)
  Description: Printer 218
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
  Last input 00:00:01, output 00:00:00, output hang never
  Last clearing of "show interface" counters never
GigabitEthernet1/0/2 is down, line protocol is down (notconnect)
  Hardware is Gigabit Ethernet, address is 7c69.f6a1.b202 (bia 7c69.f6a1.b202)
  Description: Room 218 desk
  Last input 32w1d, output 32w1d, output hang never
  Last clearing of "show interface" counters never
GigabitEthernet1/0/3 is down, line protocol is down (notconnect)
  Hardware is Gigabit Ethernet, address is 7c69.f6a1.b203 (bia 7c69.f6a1.b203)
  Last input never, output never, output hang never
  Last clearing of "show interface" counters never
GigabitEthernet1/0/4 is administratively down, line protocol is down (disabled)
  Hardware is Gigabit Ethernet, address is 7c69.f6a1.b204 (bia 7c69.f6a1.b204)
  Last input 1y20w, output 1y20w, output hang never
  Last clearing of "show interface" counters never
GigabitEthernet1/0/10 is down, line protocol is down (notconnect)
  Hardware is Gigabit Ethernet, address is 7c69.f6a1.b20a (bia 7c69.f6a1.b20a)
  Last input never, output 3d02h, output hang never
  Last clearing of "show interface" counters never
TenGigabitEthernet1/1/1 is up, line protocol is up (connected)
  Hardware is Ten Gigabit Ethernet, address is 7c69.f6a1.b231 (bia 7c69.f6a1.b231)
  Description: Uplink to core
  Last input 00:00:00, output 00:00:00, output hang never
  Last clearing of "show interface" counters never
Port-channel1 is up, line protocol is up (connected)
  Last input 00:00:00, output 00:00:00, output hang never
"""

IOS_STATUS = """
Port         Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1      Printer 218        connected    15         a-full a-1000 10/100/1000BaseTX
Gi1/0/2      Room 218 desk      notconnect   15           auto   auto 10/100/1000BaseTX
Gi1/0/3                         notconnect   131          auto   auto 10/100/1000BaseTX
Gi1/0/4                         disabled     1            auto   auto 10/100/1000BaseTX
Gi1/0/10                        notconnect   126          auto   auto 10/100/1000BaseTX
Te1/1/1      Uplink to core     connected    trunk        full    10G SFP-10GBase-SR
Po1                             connected    trunk      a-full a-1000
"""

NXOS_VERSION = """Cisco Nexus Operating System (NX-OS) Software
Kernel uptime is 420 day(s), 5 hour(s), 22 minute(s), 3 second(s)
"""

NXOS_INTERFACE = """mgmt0 is up
admin state is up,
  Hardware: GigabitEthernet, address: 0022.5566.7700 (bia 0022.5566.7700)
Ethernet1/1 is up
admin state is up, Dedicated Interface
  Hardware: 1000/10000 Ethernet, address: 0022.5566.7788 (bia 0022.5566.7788)
  Description: To C108 2960x
  Last link flapped 12week(s) 3day(s)
  Last clearing of "show interface" counters never
Ethernet1/2 is down (Link not connected)
admin state is up, Dedicated Interface
  Description: spare
  Last link flapped 40week(s) 2day(s)
  Last clearing of "show interface" counters never
Ethernet1/3 is down (Administratively down)
admin state is down, Dedicated Interface
  Last link flapped never
  Last clearing of "show interface" counters never
Ethernet1/4 is down (XCVR not inserted)
admin state is up, Dedicated Interface
  Last link flapped 1d02h
  Last clearing of "show interface" counters never
Vlan1 is down (Administratively down), line protocol is down, autostate enabled
  Hardware is EtherSVI, address is  0022.5566.7799
"""

NXOS_STATUS = """
--------------------------------------------------------------------------------
Port          Name               Status    Vlan      Duplex  Speed   Type
--------------------------------------------------------------------------------
mgmt0         --                 connected routed    full    1000    --
Eth1/1        To C108 2960x      connected trunk     full    10G     10Gbase-SR
Eth1/2        spare              notconnec 1         auto    auto    10Gbase-SR
Eth1/3        --                 disabled  1         auto    auto    10Gbase-SR
Eth1/4        --                 xcvrAbsen 1         auto    auto    --
"""

DELL_SYSTEM = """
System Description:                       PowerConnect 5548P
System Up Time (days,hour:min:sec):       412,03:24:11
System Contact:
System Name:                              B218-5548P
"""

DELL_STATUS = """
                                             Flow Link          Back   Mdix
Port     Type         Duplex  Speed Neg      ctrl State       Pressure Mode
-------- ------------ ------  ----- -------- ---- ----------- -------- -------
gi1/0/1  1G-Copper    Full    1000  Enabled  Off  Up          Disabled On
gi1/0/2  1G-Copper      --      --     --     --  Down           --     --
gi1/0/3  1G-Copper      --      --     --     --  Down           --     --
te1/0/1  10G-Fiber    Full    10000 Disabled Off  Up          Disabled Off

                                          Flow    Admin Back   Mdix
Ch       Type    Duplex  Speed  Neg      control  State Pressure Mode
-------- ------- ------  -----  -------- -------  ----- -------- ----
Po1         --     --      --      --       --    Down     --     --
"""

DELL_DESCRIPTION = """
Port      Description
-------  -----------
gi1/0/1   Phone 218
gi1/0/2   Desk 2
gi1/0/3
te1/0/1   Uplink
"""

DELL_COUNTERS = """
    Port      InUcastPkts  InMcastPkts  InBcastPkts    InOctets
---------- ------------ ------------ ------------ ------------
  gi1/0/1      120931         2310        5512      93821123
  gi1/0/2           0            0           0             0
  gi1/0/3        5521           11          20        412000
  te1/0/1    99912312       331231      12312   23123123123

    Port      OutUcastPkts OutMcastPkts OutBcastPkts   OutOctets
---------- ------------ ------------ ------------ ------------
  gi1/0/1      100231         9123       12312      72312312
  gi1/0/2           0            0           0             0
  gi1/0/3        3312           10           5        210000
  te1/0/1    88912312       231231      22312   13123123123
"""

DELL_VLAN = """
Created by: D-Default, S-Static, G-GVRP, R-Radius Assigned VLAN, V-Voice VLAN

Vlan       Name           Tagged Ports      UnTagged Ports      Created by
---- ----------------- ------------------ ------------------ ----------------
 1           1                             gi1/0/4-48,Po1-8          DV
 131       Staff                           gi1/0/1-3                 S
"""

ARUBA_VERSION = """Aruba Operating System Software.
ArubaOS (MODEL: ArubaS3500-48P), Version 7.4.1.12
Switch uptime is 205 days 3 hours 41 minutes 12 seconds
Reboot Cause: User reboot.
"""

ARUBA_INTERFACE = """
GE0/0/0 is administratively Up, Link is Up, Line protocol is Up
Hardware is Gigabit Ethernet, Address is 00:0b:86:6a:23:40
Description: GE0/0/0
Encapsulation ARPA, Loopback not set
Configured: Duplex ( AUTO ), Speed ( AUTO )
Negotiated: Duplex (Full), Speed (1 Gbps)
MTU 1514 bytes
Last clearing of counters 0 day 0 hr 0 min 0 sec
link status last changed 12 day 3 hr 2 min 32 sec
    12312 packets input, 9123123 bytes
    9912 packets output, 1231231 bytes
GE0/0/1 is administratively Up, Link is Down, Line protocol is Down
Hardware is Gigabit Ethernet, Address is 00:0b:86:6a:23:41
Description: Room M205 desk
Last clearing of counters 0 day 0 hr 0 min 0 sec
link status last changed 201 day 2 hr 0 min 1 sec
    5123 packets input, 512312 bytes
    3312 packets output, 2312312 bytes
GE0/0/2 is administratively Up, Link is Down, Line protocol is Down
Hardware is Gigabit Ethernet, Address is 00:0b:86:6a:23:42
Description: GE0/0/2
Last clearing of counters 0 day 0 hr 0 min 0 sec
link status last changed 0 day 0 hr 0 min 0 sec
    0 packets input, 0 bytes
    0 packets output, 0 bytes
GE0/0/3 is administratively Down, Link is Down, Line protocol is Down
Hardware is Gigabit Ethernet, Address is 00:0b:86:6a:23:43
Description: GE0/0/3
Last clearing of counters 0 day 0 hr 0 min 0 sec
link status last changed 30 day 0 hr 0 min 0 sec
    10 packets input, 1000 bytes
    10 packets output, 1000 bytes
"""

ARUBA_VLAN = """
VLAN CONFIGURATION
------------------
VLAN  Description  Ports                          AAA Profile
----  -----------  -----                          -----------
1     VLAN0001     GE0/0/3-47 Pc0                 N/A
15    LAB          GE0/0/0-2                      N/A
"""

# What each pretend switch answers, by command
OUTPUTS = {
    "cisco_ios": {"show version": IOS_VERSION, "show interfaces": IOS_INTERFACES,
                  "show interfaces status": IOS_STATUS},
    "cisco_nxos": {"show version": NXOS_VERSION, "show interface": NXOS_INTERFACE,
                   "show interface status": NXOS_STATUS},
    "dell_powerconnect": {"show system": DELL_SYSTEM, "show interfaces status": DELL_STATUS,
                          "show interfaces description": DELL_DESCRIPTION,
                          "show interfaces counters": DELL_COUNTERS, "show vlan": DELL_VLAN},
    "aruba_mas": {"show version": ARUBA_VERSION, "show interface": ARUBA_INTERFACE,
                  "show vlan": ARUBA_VLAN},
}

# port -> (result, vlan, status) we expect at 6 months
EXPECTED = {
    "cisco_ios": {"Gi1/0/1": ("ACTIVE", "15", "up"), "Gi1/0/2": ("INACTIVE", "15", "down"),
                  "Gi1/0/3": ("INACTIVE", "131", "down"), "Gi1/0/4": ("INACTIVE", "1", "disabled"),
                  "Gi1/0/10": ("ACTIVE", "126", "down"), "Te1/1/1": ("ACTIVE", "trunk", "up")},
    "cisco_nxos": {"Eth1/1": ("ACTIVE", "trunk", "up"), "Eth1/2": ("INACTIVE", "1", "down"),
                   "Eth1/3": ("INACTIVE", "1", "disabled"), "Eth1/4": ("ACTIVE", "1", "down")},
    "dell_powerconnect": {"gi1/0/1": ("ACTIVE", "131", "up"), "gi1/0/2": ("INACTIVE", "131", "down"),
                          "gi1/0/3": ("UNSURE", "131", "down"), "te1/0/1": ("ACTIVE", "", "up")},
    "aruba_mas": {"GE0/0/0": ("ACTIVE", "15", "up"), "GE0/0/1": ("INACTIVE", "15", "down"),
                  "GE0/0/2": ("INACTIVE", "15", "down"), "GE0/0/3": ("ACTIVE", "1", "disabled")},
}
