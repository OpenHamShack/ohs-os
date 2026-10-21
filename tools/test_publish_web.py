"""Behaviour tests for tools/publish-web.sh's upload, verification and removals.

    python3 -m unittest tools/test_publish_web.py

The script runs for real against a stand-in for 20i: `ssh` on PATH is a shim
that runs the remote command in a local folder (so rsync, rm and sha256sum
are the real ones, on real files), and the "website" is a local HTTP server
over that folder's public_html with Range support, which can be told to
ignore Range or to serve a stale copy, as StackCDN has. A month key that is
still valid stands in for the key route, so no Control360 call is made.
Needs bash, rsync and curl.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "publish-web.sh"
FULL_MAX = 1000  # bytes: anything bigger takes the large-file path

SSH_SHIM = """#!/bin/bash
# ssh [options] host command...: run the command in $FAKE_REMOTE_ROOT.
while [ $# -gt 0 ]; do
    case "$1" in
        -i|-o|-l|-p) shift 2 ;;
        -*) shift ;;
        *) shift; break ;;
    esac
done
cd "$FAKE_REMOTE_ROOT" && exec bash -c "$*"
"""


class Site(http.server.ThreadingHTTPServer):
    docroot: Path
    ignore_range = False
    stale: dict[str, bytes] = {}
    log: list[tuple[str, str]] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ANN002 - quiet
        pass

    def do_GET(self):  # noqa: N802 - http.server's name
        srv: Site = self.server  # type: ignore[assignment]
        path = urlsplit(self.path).path.lstrip("/")
        rng = self.headers.get("Range")
        srv.log.append((path, rng or ""))
        if path in srv.stale:
            body = srv.stale[path]
        else:
            f = srv.docroot / path
            if not f.is_file():
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
                return
            body = f.read_bytes()
        if rng and not srv.ignore_range:
            start, end = (int(x) for x in rng.split("=", 1)[1].split("-"))
            part = body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start + len(part) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(part)))
            self.end_headers(); self.wfile.write(part)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # curl hung up once --max-filesize said no


class PublishWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-publish-web."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"; (self.home / "build").mkdir(parents=True)
        self.remote = self.tmp / "remote"; self.docroot = self.remote / "public_html"
        self.docroot.mkdir(parents=True)
        self.local = self.tmp / "site"; self.local.mkdir()
        bin_dir = self.tmp / "bin"; bin_dir.mkdir()
        (bin_dir / "ssh").write_text(SSH_SHIM); (bin_dir / "ssh").chmod(0o755)
        helper = self.tmp / "lab-session.sh"; helper.write_text("exit 1\n")
        standing = self.tmp / "20i"; standing.mkdir()
        (standing / "id_ed25519").write_text("not a key\n")
        (standing / "meta").write_text(f"handle=test\nexpires={int(time.time()) + 86400 * 5}\n")

        self.server = Site(("127.0.0.1", 0), Handler)
        self.server.docroot = self.docroot
        self.server.stale, self.server.log = {}, []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{bin_dir}:{os.environ['PATH']}",
                        FAKE_REMOTE_ROOT=str(self.remote), PUBLISH_LAB_SESSION_HELPER=str(helper),
                        PUBLISH_STANDING_DIR=str(standing), PUBLISH_LOCAL_DIR=str(self.local),
                        PUBLISH_BACKUP_ROOT=str(self.tmp / "backups"),
                        PUBLISH_URL_BASE=f"http://127.0.0.1:{self.server.server_address[1]}",
                        PUBLISH_VERIFY_FULL_MAX=str(FULL_MAX))

    def put(self, root: Path, rel: str, data: bytes) -> None:
        f = root / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(data)

    def run_site(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), "site", "--on-shack", "--docroot-is-ours", *extra],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def site_with_iso(self) -> bytes:
        iso = os.urandom(3 * 1024 * 1024 + 123)
        self.put(self.local, "index.html", b"<h1>site</h1>")
        self.put(self.local, "downloads/openhamshack.iso", iso)
        return iso

    # -- large files -------------------------------------------------------
    def test_a_large_file_is_checked_by_origin_hash_and_a_ranged_read_not_downloaded(self):
        iso = self.site_with_iso()
        r = self.run_site()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.docroot / "downloads/openhamshack.iso").read_bytes(), iso)
        self.assertIn("verified downloads/openhamshack.iso (3145851 bytes) by origin sha256 + ranged HTTPS read", r.stdout)
        iso_gets = [rng for path, rng in self.server.log if path == "downloads/openhamshack.iso"]
        self.assertEqual(iso_gets, [f"bytes={len(iso) - 1048576}-{len(iso) - 1}"])
        # The small page was downloaded whole.
        self.assertIn(("index.html", ""), self.server.log)

    def test_a_stale_copy_at_the_edge_fails_even_when_the_origin_is_right(self):
        iso = self.site_with_iso()
        self.server.stale["downloads/openhamshack.iso"] = iso[:-10] + b"0123456789"
        r = self.run_site()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("is not what was uploaded", r.stdout)
        self.assertNotIn("PASS", r.stdout)

    def test_a_server_that_ignores_range_gets_the_whole_file_instead(self):
        self.site_with_iso()
        self.server.ignore_range = True
        r = self.run_site()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ignored Range; downloading it whole", r.stdout)
        self.assertIn("by origin sha256 + full download", r.stdout)

    def test_the_dry_run_says_how_a_large_file_will_be_checked_and_uploads_nothing(self):
        self.site_with_iso()
        r = self.run_site("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would upload: downloads/openhamshack.iso (3145851 bytes; verified by origin sha256", r.stdout)
        self.assertFalse((self.docroot / "downloads").exists())

    # -- the holding page ------------------------------------------------
    def holding_page(self) -> None:
        for rel in ("index.html", "tokens.css", "icons/atv.svg", "icons/remote.svg",
                    "logo/lockup-horizontal-on-dark.svg", "icon-192.png", "robots.txt", ".htaccess"):
            self.put(self.docroot, rel, b"holding " + rel.encode())
        for rel in ("feeds/catalogue.json", "feeds/.htaccess", "feeds/icons/atv.svg", "feeds/tokens.css"):
            self.put(self.docroot, rel, b"feed " + rel.encode())
        self.put(self.docroot, "notes-from-someone-else.txt", b"keep")
        self.put(self.local, "index.html", b"<h1>site</h1>")
        self.put(self.local, "icon-192.png", b"new icon")
        self.put(self.local, "robots.txt", b"User-agent: *")

    def test_remove_holding_clears_the_holding_pages_leftovers_and_nothing_else(self):
        self.holding_page()
        r = self.run_site("--remove-holding")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for gone in ("tokens.css", "icons", "logo"):
            self.assertFalse((self.docroot / gone).exists(), gone)
        # The site's own files of the same names are the new ones, not removed.
        self.assertEqual((self.docroot / "icon-192.png").read_bytes(), b"new icon")
        self.assertEqual((self.docroot / "robots.txt").read_bytes(), b"User-agent: *")
        # feeds/, dotfiles and files the script never made are untouched.
        for kept in ("feeds/catalogue.json", "feeds/.htaccess", "feeds/icons/atv.svg", "feeds/tokens.css",
                     ".htaccess", "notes-from-someone-else.txt"):
            self.assertTrue((self.docroot / kept).is_file(), kept)
        # What it removed was backed up first.
        backups = list((self.tmp / "backups").glob("ohs-site-*"))
        self.assertTrue((backups[0] / "tokens.css").is_file())

    def test_remove_holding_leaves_a_folder_the_site_still_uses(self):
        self.holding_page()
        self.put(self.local, "icons/atv.svg", b"<svg/>")
        r = self.run_site("--remove-holding")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.docroot / "icons/atv.svg").read_bytes(), b"<svg/>")
        self.assertFalse((self.docroot / "icons/remote.svg").exists())

    def test_remove_holding_dry_run_lists_and_removes_nothing(self):
        self.holding_page()
        r = self.run_site("--remove-holding", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would remove: tokens.css", r.stdout)
        self.assertIn("would remove: icons/atv.svg", r.stdout)
        self.assertNotIn("would remove: icon-192.png", r.stdout)
        self.assertTrue((self.docroot / "tokens.css").is_file())

    def test_the_site_never_removes_anything_under_feeds(self):
        self.holding_page()
        r = self.run_site("--remove", "feeds/catalogue.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("feeds/ is not the site's", r.stdout)
        self.assertTrue((self.docroot / "feeds/catalogue.json").is_file())

    def test_remove_holding_is_refused_for_the_feeds(self):
        r = subprocess.run(["bash", str(SCRIPT), "feeds", "--on-shack", "--remove-holding"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("for the site only", r.stderr)


if __name__ == "__main__":
    unittest.main()
