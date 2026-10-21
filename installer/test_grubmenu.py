"""Behaviour tests for the ISO's boot menu, as mkiso.sh writes it.

    python3 -m unittest installer/test_grubmenu.py

The script is run as mkiso.sh runs it and its output parsed, the way GRUB
would read it: a menuentry's title, then its linux and initrd lines.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "grubmenu.py"


def menu(version: str = "2.0.0") -> tuple[dict, list[dict]]:
    out = subprocess.run([sys.executable, str(SCRIPT), version], capture_output=True, text=True, check=True).stdout
    settings = dict(re.findall(r"^set (\w+)=(\S+)$", out, re.M))
    entries = []
    for title, body in re.findall(r'^menuentry "([^"]+)" \{\n(.*?)^\}', out, re.M | re.S):
        linux = re.search(r"^\s*linux (\S+) (.*)$", body, re.M)
        initrd = re.search(r"^\s*initrd (\S+)$", body, re.M)
        entries.append({"title": title, "kernel": linux.group(1), "args": linux.group(2).split(),
                        "initrd": initrd.group(1)})
    return settings, entries


class Menu(unittest.TestCase):
    def test_touch_install_is_first_and_the_default(self) -> None:
        settings, entries = menu("2.0.0-beta.1")
        self.assertEqual(settings["default"], "0")
        self.assertEqual(entries[0]["title"], "Install OpenHamShack 2.0.0-beta.1")
        self.assertNotIn("shack.install=auto", entries[0]["args"])

    def test_only_the_unattended_entry_installs_by_itself(self) -> None:
        _, entries = menu()
        auto = [e for e in entries if "shack.install=auto" in e["args"]]
        self.assertEqual(len(auto), 1)
        self.assertIs(auto[0], entries[-1], "the unattended entry must be last, away from a scrolling finger")
        # It names its disk (the installer refuses without one) and that disk
        # is a virtio one, which no real PC has.
        self.assertIn("shack.disk=/dev/vda", auto[0]["args"])
        self.assertIn("console=ttyS0,115200n8", auto[0]["args"])
        self.assertIn("/dev/vda", auto[0]["title"])

    def test_every_entry_boots_the_one_live_system(self) -> None:
        _, entries = menu()
        self.assertEqual(len(entries), 3)
        for e in entries:
            self.assertEqual((e["kernel"], e["initrd"]), ("/live/vmlinuz", "/live/initrd.img"), e["title"])
            self.assertEqual(e["args"][:2], ["boot=live", "components"], e["title"])

    def test_safe_graphics(self) -> None:
        _, entries = menu()
        safe = [e for e in entries if "nomodeset" in e["args"]]
        self.assertEqual(len(safe), 1)
        self.assertTrue(all("safe graphics" in e["title"] and "quiet" not in e["args"] for e in safe))

    def test_a_ci_machine_selects_the_unattended_entry(self) -> None:
        # The SMBIOS check sets default to the unattended entry's index; a
        # menu reordered without updating it would leave CI at the touch page
        # until it timed out, or pick some other entry.
        out = subprocess.run([sys.executable, str(SCRIPT), "1"], capture_output=True, text=True, check=True).stdout
        _, entries = menu("1")
        chosen = int(re.search(r"^\s+set default=(\d+)$", out, re.M).group(1))
        self.assertIn("shack.install=auto", entries[chosen]["args"])
        self.assertIn('"$shack_product" = "openhamshack-ci"', out)

    def test_long_enough_to_choose(self) -> None:
        settings, _ = menu()
        self.assertGreaterEqual(int(settings["timeout"]), 5)

    def test_needs_a_version(self) -> None:
        self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPT)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
