"""
Pretend switches for testing without real hardware (shared with Switch Port Finder).

    MockCLI      - answers commands the way a switch would
    TelnetSwitch - serves a MockCLI over telnet (username/password prompts)
    SSHSwitch    - serves a MockCLI over SSH (needs paramiko, which netmiko installs)
"""

import socket
import socketserver
import threading

import paramiko


class MockCLI:
    def __init__(self, hostname, outputs, ping_ok=(), start_enabled=True, enable_secret=None,
                 prompt_style="cisco"):
        self.hostname = hostname
        self.outputs = outputs              # {"command": "output"}
        self.ping_ok = set(ping_ok)
        self.prompt_style = prompt_style          # "cisco": HOST#   "aruba": (HOST) #
        self.enabled = start_enabled
        self.enable_secret = enable_secret
        self.awaiting_enable = False
        self.commands = []

    @property
    def prompt(self):
        mark = "#" if self.enabled else ">"
        if self.prompt_style == "aruba":
            return f"({self.hostname}) {mark}"
        return f"{self.hostname}{mark}"

    def handle(self, line):
        """Return the text to send back for one line typed (without the prompt)."""
        cmd = line.strip()
        self.commands.append(cmd)
        if self.awaiting_enable:
            self.awaiting_enable = False
            if cmd == self.enable_secret:
                self.enabled = True
                return ""
            return "% Access denied\n"
        if cmd == "":
            return ""
        if cmd == "enable":
            if self.enable_secret:
                self.awaiting_enable = True
                return "Password: "
            self.enabled = True
            return ""
        if cmd.startswith(("terminal ", "no page", "screen-length")):
            return ""
        if cmd.startswith("ping"):
            ip = next((t for t in cmd.split() if t.count(".") == 3), "")
            if ip in self.ping_ok:
                return ("Type escape sequence to abort.\nSending 2, 100-byte ICMP Echos to "
                        f"{ip}, timeout is 1 seconds:\n!!\nSuccess rate is 100 percent (2/2)\n")
            return ("Type escape sequence to abort.\nSending 2, 100-byte ICMP Echos to "
                    f"{ip}, timeout is 1 seconds:\n..\nSuccess rate is 0 percent (0/2)\n")
        if cmd in self.outputs:
            return self.outputs[cmd].rstrip("\n") + "\n"
        return "                 ^\n% Invalid input detected at '^' marker.\n"


class _Lines:
    """Turns a stream of bytes into typed lines (handles \\r\\n, \\r\\0 and telnet IAC)."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == 255 and i + 1 < len(data):          # IAC
                cmd = data[i + 1]
                i += 3 if cmd in (251, 252, 253, 254) else 2
                continue
            out.append(b)
            i += 1
        self.buf += bytes(out)
        self.buf = self.buf.replace(b"\r\n", b"\n").replace(b"\r\x00", b"\n").replace(b"\r", b"\n")
        lines = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            lines.append(line.decode(errors="ignore"))
        return lines


def _serve_shell(send, recv, cli, after_login=""):
    send(after_login + "\r\n" + cli.prompt)
    lines = _Lines()
    while True:
        data = recv()
        if not data:
            return
        for line in lines.feed(data):
            send(line + "\r\n")                          # echo, like a real switch
            if line.strip() in ("exit", "logout", "quit"):
                return
            out = cli.handle(line)
            if out.endswith("Password: "):
                send(out)
                continue
            send(out.replace("\n", "\r\n") + cli.prompt)


class TelnetSwitch:
    def __init__(self, cli, username, password, port=0, user_prompt="Username: "):
        self.cli, self.username, self.password = cli, username, password
        self.user_prompt = user_prompt         # Aruba: "User: ", Dell PowerConnect: "User Name:"
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                def send(s):
                    try:
                        sock.sendall(s.encode())
                    except OSError:
                        pass
                recv = lambda: sock.recv(4096)
                lines = _Lines()

                def read_line():
                    while True:
                        data = recv()
                        if not data:
                            return None
                        got = lines.feed(data)
                        got = [g for g in got if g.strip()] or got
                        if got and got[0].strip():
                            return got[0].strip()

                send("\r\n\r\nUser Access Verification\r\n\r\n" + outer.user_prompt)
                user = read_line()
                send("Password: ")
                pwd = read_line()
                if user != outer.username or pwd != outer.password:
                    send("\r\n% Login invalid\r\n\r\n" + outer.user_prompt)
                    return
                _serve_shell(send, recv, outer.cli)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class SSHSwitch:
    _host_key = None

    def __init__(self, cli, username, password, port=0):
        if SSHSwitch._host_key is None:
            SSHSwitch._host_key = paramiko.RSAKey.generate(2048)
        self.cli, self.username, self.password = cli, username, password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(10)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client):
        outer = self

        class Server(paramiko.ServerInterface):
            def __init__(self):
                self.shell = threading.Event()

            def check_auth_password(self, username, password):
                ok = username == outer.username and password == outer.password
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else \
                    paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_pty_request(self, *args):
                return True

            def check_channel_shell_request(self, channel):
                self.shell.set()
                return True

        transport = paramiko.Transport(client)
        transport.add_server_key(SSHSwitch._host_key)
        server = Server()
        try:
            transport.start_server(server=server)
            chan = transport.accept(20)
            if chan is None or not server.shell.wait(10):
                return
            _serve_shell(lambda s: chan.sendall(s.encode()), lambda: chan.recv(4096), outer.cli)
            chan.close()
        except Exception:
            pass
        finally:
            transport.close()

    def stop(self):
        self.running = False
        self.sock.close()
