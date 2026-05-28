from __future__ import annotations

import unittest

from voicebot.api_surface import api_surface_by_area, prototype_endpoints, public_endpoints_are_workspace_scoped


class ApiSurfaceTests(unittest.TestCase):
    def test_public_endpoints_are_workspace_scoped(self) -> None:
        self.assertTrue(public_endpoints_are_workspace_scoped())

    def test_api_surface_covers_required_areas(self) -> None:
        grouped = api_surface_by_area()

        for area in ("admin", "channel", "runtime", "session", "transcript", "task", "provider", "testing"):
            self.assertIn(area, grouped)

    def test_prototype_endpoints_are_identified(self) -> None:
        endpoints = prototype_endpoints()

        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["path"], "/webrtc/test")
        self.assertEqual(endpoints[0]["visibility"], "prototype")


if __name__ == "__main__":
    unittest.main()
