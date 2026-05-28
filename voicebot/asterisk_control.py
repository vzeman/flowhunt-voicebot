from __future__ import annotations

from dataclasses import dataclass
import socket


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    message: str


class AsteriskAMI:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def hangup(self, call_id: str) -> ControlResult:
        channel = self.find_channel(call_id)
        if not channel:
            return ControlResult(False, f"channel not found for call_id={call_id}")
        response = self.action({"Action": "Hangup", "Channel": channel})
        return ControlResult("Response: Success" in response, response)

    def transfer(self, call_id: str, target: str) -> ControlResult:
        channel = self.find_channel(call_id)
        if not channel:
            return ControlResult(False, f"channel not found for call_id={call_id}")
        response = self.action(
            {
                "Action": "Redirect",
                "Channel": channel,
                "Context": "agent-transfer",
                "Exten": target,
                "Priority": "1",
            }
        )
        return ControlResult("Response: Success" in response, response)

    def send_dtmf(self, call_id: str, digit: str) -> ControlResult:
        channel = self.find_channel(call_id)
        if not channel:
            return ControlResult(False, f"channel not found for call_id={call_id}")
        response = self.action({"Action": "PlayDTMF", "Channel": channel, "Digit": digit})
        return ControlResult("Response: Success" in response, response)

    def command(self, command: str) -> ControlResult:
        response = self.action({"Action": "Command", "Command": command})
        return ControlResult("Response: Success" in response, response)

    def reload_pjsip(self) -> ControlResult:
        return self.command("pjsip reload")

    def send_register(self, registration: str) -> ControlResult:
        return self.command(f"pjsip send register {registration}")

    def send_unregister(self, registration: str) -> ControlResult:
        return self.command(f"pjsip send unregister {registration}")

    def show_registrations(self) -> ControlResult:
        return self.command("pjsip show registrations")

    def find_channel(self, call_id: str) -> str | None:
        response = self.action({"Action": "Command", "Command": "core show channels concise"})
        for line in response.splitlines():
            if line.startswith("Output: "):
                line = line.removeprefix("Output: ").strip()
            fields = line.split("!")
            if len(fields) >= 8 and fields[5] == "AudioSocket" and call_id in fields[6]:
                return fields[0]
        return None

    def action(self, fields: dict[str, str]) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            self._read_until(sock, b"\r\n")
            self._send_action(sock, {"Action": "Login", "Username": self.username, "Secret": self.password})
            login_response = self._read_response(sock)
            if "Response: Success" not in login_response:
                return login_response
            self._send_action(sock, fields)
            response = self._read_response(sock)
            self._send_action(sock, {"Action": "Logoff"})
            return response

    def _send_action(self, sock: socket.socket, fields: dict[str, str]) -> None:
        payload = "".join(f"{_ami_field(key)}: {_ami_field(value)}\r\n" for key, value in fields.items()) + "\r\n"
        sock.sendall(payload.encode())

    def _read_response(self, sock: socket.socket) -> str:
        return self._read_until(sock, b"\r\n\r\n").decode(errors="replace")

    def _read_until(self, sock: socket.socket, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data


def _ami_field(value: str) -> str:
    text = str(value)
    if "\r" in text or "\n" in text:
        raise ValueError("AMI fields must not contain CR or LF characters")
    return text
