"""Behaviour tests for the feed publisher's JSON half (tools/feedgen.py).

    python3 -m unittest tools/test_feedgen.py
    DOCK360_SRC=<dock360 tree> <python with its requirements> -m unittest tools/test_feedgen.py

The second form also runs the station's own reader (shack/feeds.py) and
Dock360's version ordering over what feedgen writes; without DOCK360_SRC
those tests are skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("feedgen", HERE / "feedgen.py")
fg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fg)

SRC = Path(os.environ["DOCK360_SRC"]) if os.environ.get("DOCK360_SRC") else None
needs_dock360 = unittest.skipUnless(SRC and (SRC / "src/dock360").is_dir(), "DOCK360_SRC not set")


class Versions(unittest.TestCase):
    def test_each_publish_raises_the_beta_number(self):
        self.assertEqual(fg.next_version("2.0.0-beta.1", []), "2.0.0-beta.2")
        self.assertEqual(fg.next_version("2.0.0-beta.1", ["2.0.0-beta.1", "2.0.0-beta.9"]), "2.0.0-beta.10")

    def test_a_person_moving_the_line_on_wins_over_old_publishes(self):
        self.assertEqual(fg.next_version("2.1.0-beta.1", ["2.0.0-beta.7"]), "2.1.0-beta.2")

    def test_after_a_final_release_the_next_is_a_patch(self):
        self.assertEqual(fg.next_version("2.0.0-beta.4", ["2.0.0"]), "2.0.1")

    def test_engine_version_is_above_its_base_and_rises_by_the_minute(self):
        a = fg.engine_version("0.12.25", "202610091300")
        b = fg.engine_version("0.12.25", "202610100900")
        self.assertEqual(a, "0.12.26-ohs.202610091300")
        self.assertLess(fg.version_key("0.12.25"), fg.version_key(a))
        self.assertLess(fg.version_key(a), fg.version_key(b))
        self.assertLess(fg.version_key(b), fg.version_key("0.12.26"))

    def test_engine_base_must_be_plain(self):
        with self.assertRaises(fg.FeedGenError):
            fg.engine_version("0.12.26-ohs.202610091300")

    @needs_dock360
    def test_dock360_orders_engine_versions_the_same_way(self):
        # Dock360 compares a pre-release suffix as text, not as numbers; the
        # fixed-width timestamp is what keeps that right.
        sys.path.insert(0, str(SRC / "src"))
        from dock360 import release
        a = fg.engine_version("0.12.25", "202610091300")
        b = fg.engine_version("0.12.25", "202612311259")
        self.assertTrue(release.is_newer(a, "0.12.25"))
        self.assertTrue(release.is_newer(b, a))
        self.assertTrue(release.is_newer("0.12.26", b))


class Feeds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "www"
        self.pkg = Path(self.tmp.name) / "dock360-0.12.26-ohs.202610091300.d360upd"
        self.pkg.write_bytes(b"package bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def test_platform_feed_lists_the_package_relative_to_itself(self):
        feed = fg.write_platform(self.root, "beta", "2.0.0-beta.2", "0.12.26-ohs.1", self.pkg)
        self.assertEqual(feed["releases"][0]["url"], f"packages/{self.pkg.name}")
        on_disk = self.root / "feeds" / feed["releases"][0]["url"]
        self.assertEqual(on_disk.read_bytes(), b"package bytes")
        self.assertEqual(feed["releases"][0]["sha256"], fg.sha256(self.pkg))
        written = json.loads((self.root / "feeds/platform-beta.json").read_text())
        self.assertEqual(written, feed)

    def test_sequence_rises_and_an_older_version_is_refused(self):
        first = fg.write_platform(self.root, "beta", "2.0.0-beta.2", "e", self.pkg)
        second = fg.write_platform(self.root, "beta", "2.0.0-beta.3", "e", self.pkg)
        self.assertGreater(second["sequence"], first["sequence"])
        with self.assertRaises(fg.FeedGenError):
            fg.write_platform(self.root, "beta", "2.0.0-beta.3", "e", self.pkg)

    def test_catalogue_keeps_apps_already_published_and_skips_older_packages(self):
        apps = Path(self.tmp.name) / "apps"
        apps.mkdir()
        (apps / "a.d360mod").write_bytes(b"a1")
        info = {"a.d360mod": {"id": "shack-a", "version": "1.0.0", "name": "A", "kind": "a", "summary": ""}}
        inspect = lambda pkg, src: info[pkg.name]  # noqa: E731
        fg.write_catalogue(self.root, Path("unused"), apps, inspect)
        (apps / "a.d360mod").unlink()
        (apps / "b.d360mod").write_bytes(b"b1")
        (apps / "old.d360mod").write_bytes(b"a0")
        info["b.d360mod"] = {"id": "shack-b", "version": "0.1.0", "name": "B", "kind": "b", "summary": ""}
        info["old.d360mod"] = {**info["a.d360mod"], "version": "0.9.0"}
        feed = fg.write_catalogue(self.root, Path("unused"), apps, inspect)
        self.assertEqual([(a["id"], a["version"]) for a in feed["apps"]], [("shack-a", "1.0.0"), ("shack-b", "0.1.0")])
        self.assertEqual((self.root / "feeds/apps/shack-a-1.0.0.d360mod").read_bytes(), b"a1")

    @needs_dock360
    def test_a_station_accepts_what_is_written_and_signed(self):
        """Signed with throwaway keys standing in for master and modules, the
        feeds pass the station's own verify(); swapped keys do not."""
        sys.path.insert(0, str(SRC / "src"))
        from dock360 import signing
        src = Path(self.tmp.name) / "src"
        (src / "src").mkdir(parents=True)
        (src / "src" / "dock360").symlink_to(SRC / "src" / "dock360")
        keys = {}
        for sub in ("updates", "modules"):
            private, public = signing.generate_keypair(sub)
            d = src / "trusted-keys/editions/openhamshack" / sub
            d.mkdir(parents=True)
            (d / f"{sub}.pub").write_text(public.encode())
            keys[sub] = private
        apps = Path(self.tmp.name) / "apps"
        apps.mkdir()
        (apps / "a.d360mod").write_bytes(b"a1")
        fg.write_platform(self.root, "beta", "2.0.0-beta.2", "e", self.pkg)
        fg.write_catalogue(self.root, src, apps, lambda p, s: {"id": "shack-a", "version": "1.0.0",
                                                               "name": "A", "kind": "a", "summary": ""})
        feeds = self.root / "feeds"

        def sign(name, key):
            data = (feeds / name).read_bytes()
            (feeds / (name + ".sig")).write_text(signing.sign_bytes(data, keys[key]))

        sign("platform-beta.json", "updates")
        sign("catalogue.json", "modules")
        lines = fg.check(self.root, src, "beta")
        self.assertTrue(any("2.0.0-beta.2" in line for line in lines), lines)
        self.assertTrue(any("shack-a 1.0.0" in line for line in lines), lines)
        sign("platform-beta.json", "modules")
        with self.assertRaises(Exception):
            fg.check(self.root, src, "beta")


if __name__ == "__main__":
    unittest.main()
