"""Behaviour tests for gate 3's scanner (tools/secretscan.py).

    python3 -m unittest tools/test_secretscan.py

Each test plants throwaway keys in a temporary tree and runs the scanner's
own commands over it, as tools/gate3-scan.sh does over an unpacked ISO.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("secretscan", HERE / "secretscan.py")
ss = importlib.util.module_from_spec(spec)
# The file scan hands work to a process pool, which pickles functions by
# module name: the module must be importable under it.
sys.modules["secretscan"] = ss
spec.loader.exec_module(ss)


def dock360_key(path: Path) -> bytes:
    seed = os.urandom(32)
    path.write_text("# Dock360 signing key\n# SHA256:notakeyjustacommentfingerprint00000000000\n"
                    f"dock360-ed25519-private {base64.b64encode(seed).decode()}\n")
    return seed


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="secretscan-test."))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "keys").mkdir()
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        self.seed = dock360_key(self.tmp / "keys" / "signing.key")

    def files(self) -> list[list[str]]:
        out = self.tmp / "hits.tsv"
        ss.cmd_files([str(out), str(self.tmp / "keys" / "signing.key"), "--", str(self.tree)])
        return [line.rstrip("\n").split("\t") for line in out.read_text().splitlines()]

    def test_a_key_is_found_raw_as_hex_and_as_base64(self):
        (self.tree / "raw.bin").write_bytes(os.urandom(100) + self.seed + os.urandom(100))
        (self.tree / "hex.txt").write_text("seed=" + self.seed.hex())
        (self.tree / "b64.json").write_text('{"k": "%s"}' % base64.b64encode(self.seed).decode())
        found = {Path(p).name for kind, p, _ in self.files() if kind.startswith("KNOWN-SECRET")}
        self.assertEqual(found, {"raw.bin", "hex.txt", "b64.json"})

    def test_the_key_files_format_label_is_not_a_secret(self):
        # Dock360's own signing.py carries the label; on the beta.4 ISO it
        # was five false needle hits in Dock360's source.
        (self.tree / "signing.py").write_text('LABEL = "dock360-ed25519-private"\n')
        self.assertEqual([h for h in self.files() if h[0].startswith("KNOWN-SECRET")], [])

    def test_a_pem_header_alone_is_not_a_key_but_one_with_a_body_is(self):
        (self.tree / "const.py").write_text('HDR = "-----BEGIN OPENSSH PRIVATE KEY-----"\n')
        (self.tree / "id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 70 + "\n")
        pem = {Path(p).name for kind, p, _ in self.files() if kind == "pem-private-key"}
        self.assertEqual(pem, {"id"})

    def test_the_raw_scan_finds_a_key_in_free_space(self):
        disk = self.tmp / "disk.img"
        disk.write_bytes(b"\0" * (70 << 20) + self.seed + b"\0" * 4096)
        out = self.tmp / "raw.tsv"
        ss.cmd_raw([str(out), str(disk), str(self.tmp / "keys" / "signing.key")])
        kinds = [line.split("\t")[0] for line in out.read_text().splitlines()]
        self.assertTrue(any(k.startswith("KNOWN-SECRET") for k in kinds), kinds)


class Classify(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="secretscan-test."))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp / "root"
        (self.root / "var/lib/dpkg/info").mkdir(parents=True)
        (self.root / "usr/lib").mkdir(parents=True)
        self.raw = self.tmp / "raw.tsv"
        self.raw.write_text("scanned-bytes\t100\t\n")

    def classify(self, rows: list[tuple[str, Path]], *extra: str) -> tuple[int, str]:
        tsv = self.tmp / "files.tsv"
        tsv.write_text("".join(f"{k}\t{p}\t0\n" for k, p in rows))
        summary = self.tmp / "summary.txt"
        rc = ss.cmd_classify([str(tsv), str(self.raw), str(summary), "--root", str(self.root), *extra])
        return rc, summary.read_text()

    def test_a_key_in_a_file_debian_shipped_unchanged_passes(self):
        lib = self.root / "usr/lib/libgnutls.so"
        lib.write_bytes(b"stock bytes")
        (self.root / "var/lib/dpkg/info/libgnutls.md5sums").write_text(
            f"{hashlib.md5(b'stock bytes').hexdigest()}  usr/lib/libgnutls.so\n")
        rc, text = self.classify([("pem-private-key", lib)])
        self.assertEqual(rc, 0, text)
        self.assertIn("verdict PASS", text)

    def test_the_same_file_changed_after_install_needs_a_person(self):
        lib = self.root / "usr/lib/libgnutls.so"
        lib.write_bytes(b"changed bytes")
        (self.root / "var/lib/dpkg/info/libgnutls.md5sums").write_text(
            f"{hashlib.md5(b'stock bytes').hexdigest()}  usr/lib/libgnutls.so\n")
        rc, text = self.classify([("pem-private-key", lib)])
        self.assertEqual(rc, 3)
        self.assertIn("verdict REVIEW", text)

    def test_a_fixture_identical_to_the_dock360_source_passes(self):
        src = self.tmp / "src"
        (src / "tests").mkdir(parents=True)
        (src / "tests/test_tailscale.py").write_text('KEY = "tskey-auth-constructed0000"\n')
        shipped = self.root / "opt/dock360/releases/1.0/tests/test_tailscale.py"
        shipped.parent.mkdir(parents=True)
        shipped.write_text('KEY = "tskey-auth-constructed0000"\n')
        rc, text = self.classify([("tailscale-key", shipped)], "--dock360-src", str(src))
        self.assertEqual(rc, 0, text)

    def test_a_debian_conffile_unchanged_passes(self):
        # Chromium's apikeys is a conffile: its md5 is in dpkg's status, not
        # in any md5sums file.
        keys = self.root / "etc/chromium.d/apikeys"
        keys.parent.mkdir(parents=True)
        keys.write_text('export GOOGLE_API_KEY="AIza..."\n')
        (self.root / "var/lib/dpkg/status").write_text(
            "Package: chromium-common\nConffiles:\n"
            f" /etc/chromium.d/apikeys {hashlib.md5(keys.read_bytes()).hexdigest()}\n\n")
        rc, text = self.classify([("google-api-key", keys)])
        self.assertEqual(rc, 0, text)

    def test_a_library_in_one_container_layer_is_known_from_a_later_one(self):
        apps = self.tmp / "apps"
        lib = apps / "shack-x/layers/2/usr/lib/libgnutls.so"
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b"stock bytes")
        info = apps / "shack-x/layers/5/var/lib/dpkg/info"
        info.mkdir(parents=True)
        (info / "libgnutls.md5sums").write_text(f"{hashlib.md5(b'stock bytes').hexdigest()}  usr/lib/libgnutls.so\n")
        rc, text = self.classify([("pem-private-key", lib)], "--apps", str(apps))
        self.assertEqual(rc, 0, text)

    def test_any_known_key_fails_even_in_the_raw_disk(self):
        self.raw.write_text("KNOWN-SECRET:ohs-master.key:raw\t1\t4096\nscanned-bytes\t100\t\n")
        rc, text = self.classify([])
        self.assertEqual(rc, 1)
        self.assertIn("verdict FAIL", text)


if __name__ == "__main__":
    unittest.main()
