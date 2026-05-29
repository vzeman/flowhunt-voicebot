from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebot.sip_trunks import SipTrunk, SipTrunkStore, render_pjsip_trunks


class SipTrunkStoreTests(unittest.TestCase):
    def test_upsert_persists_redacted_trunk_and_renders_pjsip_include(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SipTrunkStore(
                str(Path(directory) / "sip_trunks.json"),
                str(Path(directory) / "asterisk" / "pjsip-trunks.conf"),
            )

            trunk = store.upsert(
                SipTrunk(
                    trunk_id="customer-1",
                    host="sip.example.com",
                    user="user-1",
                    password="secret",
                    auth_user="auth-1",
                    contact_user="contact-1",
                    from_user="from-1",
                    display_name="Customer 1",
                )
            )

            self.assertEqual(trunk.redacted_dict()["password"], {"configured": True, "redacted": True})
            self.assertEqual(store.get("customer-1").host, "sip.example.com")
            rendered = Path(directory, "asterisk", "pjsip-trunks.conf").read_text(encoding="utf-8")
            self.assertIn("[trunk-customer-1-reg]", rendered)
            self.assertIn("endpoint=trunk-customer-1-endpoint", rendered)
            self.assertIn("client_uri=sip:user-1@sip.example.com", rendered)
            self.assertIn("username=auth-1", rendered)
            self.assertIn("contact_user=contact-1", rendered)
            self.assertIn("from_user=from-1", rendered)
            self.assertIn("line=yes", rendered)
            self.assertIn("password=secret", rendered)

    def test_disabled_trunk_is_kept_in_registry_but_not_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            include = Path(directory) / "pjsip-trunks.conf"
            store = SipTrunkStore(str(Path(directory) / "sip_trunks.json"), str(include))

            store.upsert(SipTrunk("customer-1", "sip.example.com", "user-1", "secret"))
            store.set_enabled("customer-1", False)

            self.assertFalse(store.get("customer-1").enabled)
            self.assertNotIn("trunk-customer-1-reg", include.read_text(encoding="utf-8"))

    def test_rejects_unsafe_trunk_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "trunk_id"):
            render_pjsip_trunks([SipTrunk("bad/id", "sip.example.com", "user-1", "secret")])

    def test_rejects_enabled_trunk_without_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "password is required"):
            render_pjsip_trunks([SipTrunk("customer-1", "sip.example.com", "user-1", "")])


if __name__ == "__main__":
    unittest.main()
