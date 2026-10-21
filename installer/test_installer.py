"""Behaviour tests for the USB installer.

    python3 -m unittest installer/test_installer.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("installer", HERE / "installer.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


def lsblk(*devs: dict) -> str:
    return json.dumps({"blockdevices": list(devs)})


SATA = {"name": "sda", "path": "/dev/sda", "size": 512_000_000_000, "model": "XINCUU-512GB",
        "type": "disk", "rm": False, "ro": False, "tran": "sata"}
STICK = {"name": "sdb", "path": "/dev/sdb", "size": 32_000_000_000, "model": "Cruzer",
         "type": "disk", "rm": True, "ro": False, "tran": "usb"}


class Disks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        inst.PAYLOAD = Path(self.tmp.name)
        (inst.PAYLOAD / "manifest.json").write_text(json.dumps({"raw_bytes": 6_442_450_944}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_offers_the_internal_disk_only(self) -> None:
        got = inst.disks(lsblk(SATA, STICK), booted_from="/dev/sdb")
        self.assertEqual([d["path"] for d in got], ["/dev/sda"])

    def test_never_offers_the_disk_it_booted_from(self) -> None:
        # Booted from USB, the stick can enumerate as sda and the internal
        # disk as sdb; nothing may be decided by name.
        internal_as_sdb = dict(SATA, name="sdb", path="/dev/sdb")
        stick_as_sda = dict(STICK, name="sda", path="/dev/sda", rm=False, tran="sata")
        got = inst.disks(lsblk(stick_as_sda, internal_as_sdb), booted_from="/dev/sda")
        self.assertEqual([d["path"] for d in got], ["/dev/sdb"])

    def test_skips_disks_too_small_for_the_image(self) -> None:
        tiny = dict(SATA, name="mmcblk0", path="/dev/mmcblk0", size=4_000_000_000, tran=None)
        self.assertEqual(inst.disks(lsblk(tiny), booted_from=None), [])


class Install(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = os.urandom(3 * inst.CHUNK + 12345)
        (root / "img.raw").write_bytes(self.raw)
        subprocess.run(["zstd", "-q", "-3", str(root / "img.raw"), "-o", str(root / "img.zst")], check=True)
        zst = (root / "img.zst").read_bytes()
        inst.PAYLOAD = root
        (root / "manifest.json").write_text(json.dumps({
            "file": "img.zst", "raw_bytes": len(self.raw),
            "raw_sha256": hashlib.sha256(self.raw).hexdigest(),
            "zst_sha256": hashlib.sha256(zst).hexdigest()}))
        self.target = root / "disk"
        self.target.write_bytes(b"\0" * (len(self.raw) + inst.CHUNK))
        self.saved = inst.remove_stale_boot_entries
        inst.remove_stale_boot_entries = lambda: None
        inst.state.update(phase="idle", touched=False, message="")

    def tearDown(self) -> None:
        inst.remove_stale_boot_entries = self.saved
        self.tmp.cleanup()

    def test_writes_and_verifies_the_image(self) -> None:
        inst.install(str(self.target))
        self.assertEqual(inst.state["phase"], "done", inst.state["message"])
        self.assertEqual(self.target.read_bytes()[: len(self.raw)], self.raw)

    def test_a_damaged_stick_leaves_the_disk_untouched(self) -> None:
        zst = inst.PAYLOAD / "img.zst"
        data = bytearray(zst.read_bytes())
        data[100] ^= 0xFF
        zst.write_bytes(bytes(data))
        before = self.target.read_bytes()
        inst.install(str(self.target))
        self.assertEqual(inst.state["phase"], "failed")
        self.assertFalse(inst.state["touched"])
        self.assertEqual(self.target.read_bytes(), before)

    def test_a_missing_decompressor_does_not_claim_the_disk_was_written(self) -> None:
        saved = inst.subprocess.Popen
        inst.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("zstd"))
        try:
            inst.install(str(self.target))
        finally:
            inst.subprocess.Popen = saved
        self.assertEqual(inst.state["phase"], "failed")
        self.assertFalse(inst.state["touched"])


class Unattended(unittest.TestCase):
    """shack.install=auto: the CI path. Real image, real (file) disk; only
    the disk list and the power-off are stood in for."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = os.urandom(2 * inst.CHUNK + 777)
        (root / "img.raw").write_bytes(self.raw)
        subprocess.run(["zstd", "-q", "-3", str(root / "img.raw"), "-o", str(root / "img.zst")], check=True)
        inst.PAYLOAD = root
        (root / "manifest.json").write_text(json.dumps({
            "file": "img.zst", "raw_bytes": len(self.raw),
            "raw_sha256": hashlib.sha256(self.raw).hexdigest(),
            "zst_sha256": hashlib.sha256((root / "img.zst").read_bytes()).hexdigest()}))
        self.disk = root / "vda"
        self.disk.write_bytes(b"\0" * (len(self.raw) + inst.CHUNK))
        self.saved = (inst.disks, inst.remove_stale_boot_entries)
        inst.disks = lambda *a, **k: [{"path": str(self.disk), "model": "QEMU HARDDISK", "size": 1 << 34}]
        inst.remove_stale_boot_entries = lambda: None
        inst.state.update(phase="idle", touched=False, message="", done=0, total=0)
        self.lines: list[str] = []
        self.power: list[str] = []

    def tearDown(self) -> None:
        inst.disks, inst.remove_stale_boot_entries = self.saved
        self.tmp.cleanup()

    def run_auto(self, cmdline: str) -> int:
        return inst.auto(cmdline, out=self.lines.append, power=self.power.append)

    def test_installs_the_named_disk_and_powers_off(self) -> None:
        rc = self.run_auto(f"boot=live shack.install=auto shack.disk={self.disk}")
        self.assertEqual(rc, 0, self.lines)
        self.assertEqual(self.disk.read_bytes()[: len(self.raw)], self.raw)
        self.assertEqual(self.lines[-1], "OPENHAMSHACK-INSTALL: done")
        self.assertEqual(self.power, ["poweroff"])

    def test_reboot_or_nothing_when_asked(self) -> None:
        self.run_auto(f"shack.install=auto shack.disk={self.disk} shack.after=none")
        self.assertEqual(self.power, [])
        self.run_auto(f"shack.install=auto shack.disk={self.disk} shack.after=reboot")
        self.assertEqual(self.power, ["reboot"])

    def test_refuses_without_a_named_disk(self) -> None:
        before = self.disk.read_bytes()
        self.assertNotEqual(self.run_auto("boot=live shack.install=auto"), 0)
        self.assertEqual(self.disk.read_bytes(), before)
        self.assertIn("needs shack.disk", self.lines[-1])
        self.assertEqual(self.power, [])

    def test_refuses_a_disk_the_page_would_not_offer(self) -> None:
        # The boot medium, a USB stick, anything too small: disks() has
        # already left them out, and the command line cannot put them back.
        before = self.disk.read_bytes()
        self.assertNotEqual(self.run_auto("shack.install=auto shack.disk=/dev/sdb"), 0)
        self.assertEqual(self.disk.read_bytes(), before)
        self.assertIn("/dev/sdb is not a disk this installer may write", self.lines[-1])
        self.assertEqual(self.power, [])

    def test_a_damaged_image_fails_loudly_and_stays_on(self) -> None:
        zst = inst.PAYLOAD / "img.zst"
        data = bytearray(zst.read_bytes())
        data[50] ^= 0xFF
        zst.write_bytes(bytes(data))
        self.assertEqual(self.run_auto(f"shack.install=auto shack.disk={self.disk}"), 1)
        self.assertTrue(self.lines[-1].startswith("OPENHAMSHACK-INSTALL: failed: "), self.lines)
        # Left on, so whoever reads the console sees why.
        self.assertEqual(self.power, [])

    def test_reads_only_shack_settings(self) -> None:
        self.assertEqual(inst.cmdline_options("quiet shack.disk=/dev/vda boot=live shack.install=auto shack.x"),
                         {"shack.disk": "/dev/vda", "shack.install": "auto"})


if __name__ == "__main__":
    unittest.main()


class PowerNowTest(unittest.TestCase):
    """Restart after taking the stick out must not wait on the live root,
    which has gone with the stick (real PC, 09/10/2026): sync, then reboot(2)."""

    def test_syncs_then_forces(self) -> None:
        import installer as inst
        calls: list = []
        from unittest import mock
        with mock.patch.object(inst.os, "sync", lambda: calls.append("sync")), \
             mock.patch.object(inst.subprocess, "run", lambda argv, **kw: calls.append(argv)), \
             mock.patch.object(inst.time, "sleep", lambda s: None):
            inst.power_now("reboot")
            inst.power_now("poweroff")
        self.assertEqual(calls, ["sync", ["systemctl", "reboot", "--force", "--force"],
                                 "sync", ["systemctl", "poweroff", "--force", "--force"]])
