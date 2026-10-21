"""Behaviour tests for openhamshack-feeds-preset, against a real Dock360 database.

    DOCK360_SRC=<dock360 tree> <python with its requirements> -m unittest image/test_feeds_preset.py

Skipped without DOCK360_SRC: the point is to run Dock360's own settings
code, which a stand-in would only imitate.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ["DOCK360_SRC"]) if os.environ.get("DOCK360_SRC") else None


@unittest.skipUnless(SRC and (SRC / "src/dock360").is_dir(), "DOCK360_SRC not set")
class Preset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "PYTHONPATH": str(SRC / "src"), "DOCK360_STATE_DIR": self.tmp.name,
                    "DOCK360_CONFIG_DIR": self.tmp.name}
        self.py(["-c", "from dock360 import db; db.migrate()"])

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, args):
        return subprocess.run([sys.executable, *args], env=self.env, cwd=self.tmp.name,
                              capture_output=True, text=True, check=True).stdout

    def preset(self, base):
        return self.py([str(HERE / "openhamshack-feeds-preset"), base])

    def settings(self):
        out = self.py(["-c", "import json; from dock360.shack import feeds; print(json.dumps(feeds.settings()))"])
        import json
        return json.loads(out)

    def test_a_fresh_controller_gets_both_addresses_and_the_beta_channel(self):
        self.preset("https://openhamshack.com/feeds")
        self.assertEqual(self.settings(), {
            "platformUrl": "https://openhamshack.com/feeds/platform-{channel}.json",
            "catalogueUrl": "https://openhamshack.com/feeds/catalogue.json",
            "channel": "beta"})

    def test_a_value_an_administrator_set_is_kept_even_an_empty_one(self):
        self.py(["-c", "from dock360.shack import feeds; feeds.set_settings('', 'http://club.example/c.json', 'stable')"])
        self.assertEqual(self.preset("https://openhamshack.com/feeds/"), "")
        self.assertEqual(self.settings(), {"platformUrl": "", "catalogueUrl": "http://club.example/c.json",
                                           "channel": "stable"})

    def test_a_bad_base_is_refused_and_writes_nothing(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.preset("ftp://example/feeds/")
        self.assertEqual(self.settings()["platformUrl"], "")


if __name__ == "__main__":
    unittest.main()
