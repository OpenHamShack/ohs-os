"""Behaviour tests for server/link (openhamshack.com/api/link/link.php).

    python3 -m unittest tools/test_link_server.py

The real PHP runs in the official php CLI image under podman, served by
PHP's built-in server with several workers (a long poll holds one): link.php
in a web root laid out as public_html/api/link, lib.php and admin.php in a
scratch ~/ohs-link. 20i runs PHP 8.0 and the image is 8.3: lib.php keeps to
8.0 syntax, and tools/publish-link.sh lints it with 20i's own php first.
Skipped without podman or the image (docker.io/library/php:8.3-cli).

The client (ohs-libs shack_packet/weblink.py) has its own tests against a
Python stand-in; OHS_LINK_CLIENT_SRC=<ohs-libs>/python/shack_packet/src
also runs it against this PHP (test_the_real_client_*), which is the test
that says the two agree.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server" / "link"
IMAGE = os.environ.get("OHS_PHP_IMAGE", "docker.io/library/php:8.3-cli")
CLIENT_SRC = os.environ.get("OHS_LINK_CLIENT_SRC", "")


def have_image() -> bool:
    if not shutil.which("podman"):
        return False
    return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def token() -> str:
    return secrets.token_hex(16)


@unittest.skipUnless(have_image(), f"needs podman and {IMAGE}")
class LinkServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ohs-link-test-", dir=os.path.expanduser("~/build")))
        web = cls.tmp / "public_html" / "api" / "link"
        web.mkdir(parents=True)
        shutil.copy(SERVER / "link.php", web / "link.php")
        cls.root = cls.tmp / "ohs-link"
        (cls.root / "bin").mkdir(parents=True)
        for f in ("lib.php", "admin.php"):
            shutil.copy(SERVER / f, cls.root / "bin" / f)
        cls.port = free_port()
        cls.name = f"ohs-link-test-{os.getpid()}"
        subprocess.run(["podman", "run", "-d", "--rm", "--name", cls.name, "-p", f"127.0.0.1:{cls.port}:8000",
                        "-v", f"{cls.tmp}:/home/site:Z", "-e", "OHS_LINK_ROOT=/home/site/ohs-link",
                        "-e", "PHP_CLI_SERVER_WORKERS=8", IMAGE,
                        "php", "-S", "0.0.0.0:8000", "-t", "/home/site/public_html"],
                       check=True, capture_output=True)
        cls.url = f"http://127.0.0.1:{cls.port}/api/link/link.php"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url, timeout=1)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["podman", "rm", "-f", cls.name], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for d in ("pairs", "logs", "rate"):
            shutil.rmtree(self.root / d, ignore_errors=True)

    def op(self, op: str, **body) -> tuple[int, dict]:
        data = json.dumps({"op": op, **body}).encode()
        req = urllib.request.Request(self.url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def hello(self, call, peer, tok=None, **extra):
        tok = tok or token()
        status, out = self.op("hello", v=1, call=call, peer=peer, token=tok, version="0.3.0", env={"os": "test"},
                              **extra)
        return tok, status, out

    # ------------------------------------------------------------ pairing
    def test_a_pair_is_both_stations_asking_for_each_other(self):
        ta, s, a = self.hello("GM4BRI-1", "GM0OTB")
        self.assertEqual(s, 200, a)
        self.assertEqual(a["peer"], {"state": "absent"})
        self.assertEqual(a["first"], "GM0OTB")
        # Someone else asking for GM4BRI-1 is a different pair entirely.
        _tc, s, c = self.hello("M0ABC", "GM4BRI-1")
        self.assertEqual(c["peer"], {"state": "absent"})
        self.assertNotEqual(c["pair"], a["pair"])
        tb, s, b = self.hello("gm0otb-0", "gm4bri-1")
        self.assertEqual(s, 200, b)
        self.assertEqual(b["pair"], a["pair"])
        self.assertEqual(b["call"], "GM0OTB")
        self.assertEqual(b["peer"]["state"], "here")
        self.assertEqual(b["peer"]["hello"]["call"], "GM4BRI-1")
        # A hears that B joined.
        s, p = self.op("poll", call="GM4BRI-1", peer="GM0OTB", token=ta, cursor=0, wait=0)
        self.assertEqual(s, 200, p)
        self.assertEqual([m["t"] for m in p["msgs"]], ["peer"])
        self.assertEqual(p["msgs"][0]["state"], "joined")
        self.assertEqual(p["peer"]["state"], "here")

    def test_ssids_are_different_stations(self):
        _t, _s, a = self.hello("GM4BRI-1", "GM0OTB")
        _t, _s, b = self.hello("GM0OTB", "GM4BRI-2")
        self.assertNotEqual(a["pair"], b["pair"])

    def test_bad_calls_and_same_call_are_refused(self):
        for call, peer in (("NOTACALL", "GM0OTB"), ("GM4BRI-16", "GM0OTB"), ("GM4BRI/P", "GM0OTB"),
                           ("GM4BRI", "gm4bri-0")):
            _t, s, out = self.hello(call, peer)
            self.assertEqual(s, 400, (call, peer, out))

    # ------------------------------------------------------------ relaying
    def test_messages_go_both_ways_in_order_with_a_cursor(self):
        ta, _s, _a = self.hello("GM4BRI", "GM0OTB")
        tb, _s, _b = self.hello("GM0OTB", "GM4BRI")
        _s, p = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=0, wait=0)
        cur_a = p["cursor"]
        s, out = self.op("send", call="GM0OTB", peer="GM4BRI", token=tb,
                         msgs=[{"t": "frame", "n": i, "hex": "aa" * i, "wall": time.time()} for i in range(1, 4)])
        self.assertEqual((s, out["n"], out["delivered"]), (200, 3, True))
        _s, p = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=cur_a, wait=0)
        self.assertEqual([m["n"] for m in p["msgs"]], [1, 2, 3])
        # Nothing again from the new cursor.
        _s, again = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=p["cursor"], wait=0)
        self.assertEqual(again["msgs"], [])
        self.op("send", call="GM4BRI", peer="GM0OTB", token=ta, msgs=[{"t": "tele", "kind": "rx"}])
        _s, pb = self.op("poll", call="GM0OTB", peer="GM4BRI", token=tb, cursor=0, wait=0)
        self.assertEqual([m["t"] for m in pb["msgs"]], ["tele"])

    def test_a_long_poll_answers_as_soon_as_something_arrives_and_holds_otherwise(self):
        ta, _s, _a = self.hello("GM4BRI", "GM0OTB")
        tb, _s, _b = self.hello("GM0OTB", "GM4BRI")
        _s, p = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=0, wait=0)
        cur = p["cursor"]
        t0 = time.monotonic()
        _s, empty = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=cur, wait=1.5)
        self.assertGreaterEqual(time.monotonic() - t0, 1.4)
        self.assertEqual(empty["msgs"], [])
        got = {}

        def waiter():
            t = time.monotonic()
            got["out"] = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=cur, wait=10)
            got["s"] = time.monotonic() - t
        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.5)
        self.op("send", call="GM0OTB", peer="GM4BRI", token=tb, msgs=[{"t": "frame", "n": 1, "hex": "00"}])
        th.join(15)
        self.assertEqual([m["n"] for m in got["out"][1]["msgs"]], [1])
        self.assertLess(got["s"], 1.5)

    def test_the_relay_kinds_cannot_be_sent_by_a_station(self):
        ta, _s, _a = self.hello("GM4BRI", "GM0OTB")
        s, out = self.op("send", call="GM4BRI", peer="GM0OTB", token=ta, msgs=[{"t": "peer", "state": "left"}])
        self.assertEqual((s, out["error"]), (400, "bad-message"))

    # ------------------------------------------------------------ tokens
    def test_a_wrong_token_is_refused(self):
        self.hello("GM4BRI", "GM0OTB")
        s, out = self.op("send", call="GM4BRI", peer="GM0OTB", token=token(), msgs=[])
        self.assertEqual((s, out["error"]), (409, "superseded"))

    def test_a_new_run_of_a_callsign_replaces_the_old_one_and_the_peer_hears_it(self):
        ta, _s, _a = self.hello("GM4BRI", "GM0OTB")
        tb, _s, _b = self.hello("GM0OTB", "GM4BRI")
        ta2, s, a2 = self.hello("GM4BRI", "GM0OTB")
        self.assertEqual(a2["peer"]["state"], "here")
        s, out = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=0, wait=0)
        self.assertEqual((s, out["error"]), (409, "superseded"))
        _s, pb = self.op("poll", call="GM0OTB", peer="GM4BRI", token=tb, cursor=0, wait=0)
        self.assertEqual([(m["t"], m["state"]) for m in pb["msgs"]], [("peer", "left"), ("peer", "joined")])

    def test_bye_tells_the_other_station(self):
        ta, _s, _a = self.hello("GM4BRI", "GM0OTB")
        tb, _s, _b = self.hello("GM0OTB", "GM4BRI")
        s, _out = self.op("bye", call="GM0OTB", peer="GM4BRI", token=tb, why="stopped")
        self.assertEqual(s, 200)
        _s, p = self.op("poll", call="GM4BRI", peer="GM0OTB", token=ta, cursor=0, wait=0)
        self.assertEqual(p["msgs"][-1]["state"], "left")
        self.assertEqual(p["msgs"][-1]["why"], "stopped")
        self.assertEqual(p["peer"]["state"], "absent")
        s, out = self.op("send", call="GM0OTB", peer="GM4BRI", token=tb, msgs=[])
        self.assertEqual((s, out["error"]), (409, "left"))

    # ------------------------------------------------------------ limits and storage
    def test_get_and_oversized_bodies_are_refused(self):
        try:
            urllib.request.urlopen(self.url, timeout=5)
            self.fail("GET answered")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 405)
        big = json.dumps({"op": "send", "pad": "x" * 300000}).encode()
        req = urllib.request.Request(self.url, data=big, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("a 300 KB body answered 200")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)

    def test_hellos_from_one_address_are_limited(self):
        codes = [self.hello(f"M{i % 10}AB{chr(65 + i // 10)}", "GM0OTB")[1] for i in range(62)]
        self.assertEqual(codes[:60], [200] * 60)
        self.assertEqual(codes[60:], [429, 429])

    def test_everything_is_logged_outside_the_web_root_for_the_operator(self):
        ta, _s, a = self.hello("GM4BRI", "GM0OTB")
        tb, _s, _b = self.hello("GM0OTB", "GM4BRI")
        self.op("send", call="GM4BRI", peer="GM0OTB", token=ta, msgs=[{"t": "frame", "n": 1, "hex": "ab",
                                                                         "wall": time.time()}])
        logs = list((self.root / "logs").glob(f"*-{a['pair']}.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        lines = [json.loads(x) for x in logs[0].read_text().splitlines()]
        self.assertEqual([x.get("event") or x["msg"]["t"] for x in lines], ["join", "join", "frame"])
        self.assertEqual(lines[2]["from"], "GM4BRI")
        self.assertIsNotNone(lines[2]["lag_s"])
        self.assertFalse([p for p in (self.tmp / "public_html").rglob("*") if p.name != "link.php" and p.is_file()])
        # Tokens are never stored, only their hashes.
        state = (self.root / "pairs" / a["pair"] / "state.json").read_text()
        self.assertNotIn(ta, state)
        self.assertNotIn(tb, state)

    # ------------------------------------------------------------ the real client
    @unittest.skipUnless(CLIENT_SRC, "set OHS_LINK_CLIENT_SRC to ohs-libs' shack_packet/src")
    def test_the_real_client_pairs_and_relays_frames_both_ways(self):
        sys.path.insert(0, CLIENT_SRC)
        from shack_packet.weblink import WebRelayClient
        a = WebRelayClient(self.url, call="GM4BRI-1", peer="GM0OTB-1", role="rf-sender")
        b = WebRelayClient(self.url, call="GM0OTB-1", peer="GM4BRI-1", role="rf-receiver")
        got_a, got_b = [], []
        a.on_frame, b.on_frame = got_a.append, got_b.append
        try:
            a.start()
            self.assertEqual(a.state, "waiting")
            b.start()
            deadline = time.monotonic() + 10
            while not (a.ready and b.ready) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(a.ready and b.ready, (a.state, b.state))
            self.assertEqual(a.first, "GM0OTB-1")
            frame = bytes.fromhex("9c6e9a8a9a40e09c6e9a8a9a40613f")      # any AX.25 frame without FCS
            a.send_frame(frame)
            b.send_frame(frame)
            deadline = time.monotonic() + 10
            while not (got_a and got_b) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual((got_a, got_b), ([frame], [frame]))
            self.assertIsNotNone(a.clock())
        finally:
            a.close()
            b.close()


if __name__ == "__main__":
    unittest.main()
