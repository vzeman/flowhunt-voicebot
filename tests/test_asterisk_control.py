from __future__ import annotations

import socket
import unittest

from voicebot.asterisk_control import AsteriskAMI, _ami_field


class RecordingAMI(AsteriskAMI):
    def __init__(self) -> None:
        super().__init__("127.0.0.1", 5038, "user", "pass")
        self.actions: list[dict[str, str]] = []

    def find_channel(self, call_id: str) -> str | None:
        return "PJSIP/test-00000001"

    def action(self, fields: dict[str, str]) -> str:
        self.actions.append(fields)
        return "Response: Success\r\n"


class AsteriskControlTests(unittest.TestCase):
    def test_send_dtmf_uses_play_dtmf_ami_action(self) -> None:
        ami = RecordingAMI()

        result = ami.send_dtmf("call-1", "5")

        self.assertTrue(result.ok)
        self.assertEqual(
            ami.actions,
            [{"Action": "PlayDTMF", "Channel": "PJSIP/test-00000001", "Digit": "5"}],
        )

    def test_ami_field_rejects_crlf_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "AMI fields must not contain CR or LF characters"):
            _ami_field("123\r\nAction: Hangup")

    def test_send_action_rejects_crlf_values_before_writing(self) -> None:
        left, right = socket.socketpair()
        try:
            ami = AsteriskAMI("127.0.0.1", 5038, "user", "pass")

            with self.assertRaisesRegex(ValueError, "AMI fields must not contain CR or LF characters"):
                ami._send_action(left, {"Action": "Redirect", "Exten": "123\nAction: Hangup"})

            right.settimeout(0.01)
            with self.assertRaises(TimeoutError):
                right.recv(1024)
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
