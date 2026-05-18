# Cisco Port Analyzer

This application connects to a Cisco switch via Telnet or SSH, analyzes the interface counters, and generates an Excel spreadsheet of all physical ports based on their activity (or inactivity) within a specified timeframe (e.g., 6 months). It also correlates interface status to document the **VLAN** each port is assigned to.

It provides both a **Command Line Interface (CLI)** and a **Web Interface**.

## Requirements

Ensure you have Python 3.8+ installed.

## Installation

1. Navigate to the project directory:
   ```bash
   cd cisco-port-analyzer
   ```
2. Activate the virtual environment (Windows):
   ```bash
   .\venv\Scripts\activate
   ```
   (On macOS/Linux: `source venv/bin/activate`)
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage: Web Interface (Recommended)

The easiest way to use the tool is via the built-in web server.

1. Start the Flask server:
   ```bash
   python app.py
   ```
2. Open your web browser and navigate to: [http://localhost:5000](http://localhost:5000)
3. Fill in your switch IP, credentials, select your protocol, and enter the timeframe in months.
4. Select the **Query Type**: whether you want to find "Inactive Ports" (no activity) or "Active Ports" (has activity).
5. Click "Analyze & Download Excel". The spreadsheet will be generated and downloaded directly to your computer.

## Usage: Command Line Interface (CLI)

You can also run the script directly from your terminal:

```bash
python analyzer.py <hostname_or_ip> -u <username> -m <months> -q <query_type>
```

### Options:
- `-u, --username`: (Required) Your login username.
- `-p, --protocol`: The connection protocol. Choices are `telnet` or `ssh`. Default is `telnet`.
- `-o, --output`: The output Excel filename. Default is auto-generated based on the query.
- `-m, --months`: The timeframe to check against, in months. Default is `6`.
- `-q, --query`: The type of query to run. Choices are `inactive` or `active`. Default is `inactive`.

### Example:
```bash
# Find ports with NO activity in the last 3 months
python analyzer.py 192.168.1.10 -u admin -m 3 -q inactive

# Find ports that HAVE had activity in the last 12 months
python analyzer.py 192.168.1.10 -u admin -m 12 -q active
```

When you run the script, it will securely prompt you for your connection password, and optionally an enable secret.

## How It Works
The script runs the `show interfaces` command on the switch to parse the "Last input" and "Last output" counters, as well as `show interfaces status` to document the VLAN configuration. It looks for counters indicating "never" or exceeding the specified time duration (e.g. 26 weeks for 6 months). Unused logic interfaces like VLANs or Loopbacks are ignored.