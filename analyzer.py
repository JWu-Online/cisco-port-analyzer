import argparse
import re
import getpass
import pandas as pd
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException
import io

def parse_interfaces(output):
    interfaces = []
    current_interface = {}
    
    intf_pattern = re.compile(r"^([A-Za-z0-9/.-]+) is (up|down|administratively down), line protocol is (up|down|administratively down|notconnect)")
    desc_pattern = re.compile(r"^\s+Description:\s+(.*)$")
    last_pattern = re.compile(r"^\s+Last input (.*?), output (.*?),")

    for line in output.splitlines():
        intf_match = intf_pattern.match(line)
        if intf_match:
            if current_interface:
                interfaces.append(current_interface)
            current_interface = {
                'interface': intf_match.group(1),
                'status': intf_match.group(2),
                'protocol': intf_match.group(3),
                'description': '',
                'last_input': 'unknown',
                'last_output': 'unknown'
            }
            continue
            
        if current_interface:
            desc_match = desc_pattern.match(line)
            if desc_match:
                current_interface['description'] = desc_match.group(1).strip()
                continue
                
            last_match = last_pattern.match(line)
            if last_match:
                current_interface['last_input'] = last_match.group(1).strip()
                current_interface['last_output'] = last_match.group(2).strip()
                
    if current_interface:
        interfaces.append(current_interface)
        
    return interfaces

def is_inactive_duration(time_str, months):
    if time_str == 'never' or time_str == 'unknown':
        return True
    
    target_weeks = int(months * 4.333)
    
    if 'y' in time_str:
        year_match = re.search(r'(\d+)y', time_str)
        years = int(year_match.group(1)) if year_match else 0
        
        week_match = re.search(r'y(\d+)w', time_str)
        weeks = int(week_match.group(1)) if week_match else 0
        
        total_weeks = (years * 52) + weeks
        if total_weeks >= target_weeks:
            return True
        return False
    
    week_match = re.search(r'(\d+)w', time_str)
    if week_match:
        weeks = int(week_match.group(1))
        if weeks >= target_weeks:
            return True
            
    return False

def parse_vlans_from_status(output_status):
    vlan_map = {}
    status_keywords = ['connected', 'notconnect', 'disabled', 'err-disabled', 'err-disable', 'inactive', 'suspended', 'xcvr-inval', 'monitoring', 'faulty', 'down']
    for line in output_status.splitlines():
        if line.startswith('Port ') or line.startswith('---'):
            continue
        parts = line.split()
        if len(parts) >= 4:
            port = parts[0]
            vlan = 'unknown'
            for i in range(len(parts) - 1, 0, -1):
                if parts[i] in status_keywords:
                    if i + 1 < len(parts):
                        vlan = parts[i+1]
                    break
            vlan_map[port] = vlan
    return vlan_map

def map_vlan_to_interface(full_intf, vlan_map):
    match = re.search(r'([0-9/.-]+)$', full_intf)
    if match:
        nums = match.group(1)
        for short_port, vlan in vlan_map.items():
            if short_port.endswith(nums) and short_port[0].lower() == full_intf[0].lower():
                return vlan
    return 'unknown'

def get_ports(host, username, password, secret, protocol, months, query_type='inactive'):
    device = {
        'device_type': 'cisco_ios_telnet' if protocol == 'telnet' else 'cisco_ios',
        'host': host,
        'username': username,
        'password': password,
        'secret': secret,
    }

    net_connect = ConnectHandler(**device)
    if secret:
        net_connect.enable()
        
    output = net_connect.send_command("show interfaces")
    output_status = net_connect.send_command("show interfaces status")
    net_connect.disconnect()

    interfaces = parse_interfaces(output)
    vlan_map = parse_vlans_from_status(output_status)
    
    filtered_ports = []
    for intf in interfaces:
        if any(x in intf['interface'].lower() for x in ['vlan', 'loopback', 'port-channel']):
            continue
            
        input_inactive = is_inactive_duration(intf['last_input'], months)
        output_inactive = is_inactive_duration(intf['last_output'], months)
        
        vlan = map_vlan_to_interface(intf['interface'], vlan_map)
        
        if query_type == 'inactive':
            condition_met = input_inactive and output_inactive
        else: # active
            condition_met = not input_inactive or not output_inactive
        
        if condition_met:
            filtered_ports.append({
                'Interface': intf['interface'],
                'Description': intf['description'],
                'Status': f"{intf['status']} / {intf['protocol']}",
                'VLAN': vlan,
                'Last Input': intf['last_input'],
                'Last Output': intf['last_output']
            })

    return filtered_ports

def generate_excel_bytes(ports):
    df = pd.DataFrame(ports)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Ports')
        
        worksheet = writer.sheets['Ports']
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value)) for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 2
    
    output.seek(0)
    return output

def main():
    parser = argparse.ArgumentParser(description="Find switch ports inactive or active for a given duration.")
    parser.add_argument("host", help="Hostname or IP address of the Cisco switch")
    parser.add_argument("-u", "--username", required=True, help="Username for authentication")
    parser.add_argument("-p", "--protocol", choices=['telnet', 'ssh'], default='telnet', help="Connection protocol (default: telnet)")
    parser.add_argument("-o", "--output", help="Output Excel filename (default: auto-generated based on query)")
    parser.add_argument("-m", "--months", type=float, default=6, help="Months of inactivity/activity (default: 6)")
    parser.add_argument("-q", "--query", choices=['inactive', 'active'], default='inactive', help="Query type: inactive or active ports (default: inactive)")
    
    args = parser.parse_args()
    
    output_filename = args.output
    if not output_filename:
        output_filename = f"{args.query}_ports.xlsx"
        
    password = getpass.getpass(prompt="Password: ")
    enable_secret = getpass.getpass(prompt="Enable Secret (press enter if not needed): ")

    print(f"Connecting to {args.host} via {args.protocol}...")
    try:
        ports = get_ports(
            args.host, args.username, password, enable_secret, args.protocol, args.months, args.query
        )
    except Exception as e:
        print(f"An error occurred: {e}")
        return

    if not ports:
        print(f"No {args.query} ports found for the {args.months} months timeframe.")
        return

    print(f"Found {len(ports)} {args.query} ports. Writing to {output_filename}...")
    
    try:
        excel_data = generate_excel_bytes(ports)
        with open(output_filename, "wb") as f:
            f.write(excel_data.read())
        print(f"Successfully saved to {output_filename}")
    except Exception as e:
        print(f"Failed to write Excel file: {e}")

if __name__ == "__main__":
    main()