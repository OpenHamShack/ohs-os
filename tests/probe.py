#!/usr/bin/env python3
"""Check that an installed controller answers as one, over HTTP only.

    python3 -I tests/probe.py --admin http://127.0.0.1:P1 --front http://127.0.0.1:P2 \
        [--app openhamshack-fakebox] [--wait 900] [--out result.json]
        [--first-boot] [--store-lists "id id ..."] [--then-install id]
        [--websocket "id /path"]

Run by tests/boot-test.sh against a VM booted from the disk the ISO
installed, with QEMU's networking restricted (no route out), so everything
here - first start, the store, installing an app - is proved offline. Each
check prints PASS or FAIL with what it saw; the exit status is the number
of failures. It is a user's first ten minutes, in order:

  1. the discovery document on the front door (port 80) and the admin port
  2. a fresh controller sends you to first-run setup (no baked-in admin)
  3. setup makes the administrator; signing out shows the login page
  4. the store lists the preloaded app (first boot uploaded it)
  5. installing it from the store works and the app answers - or, with
     --first-boot, for an app first boot installs itself: first boot's
     install finishes (discovery lists the app, running) and the app
     answers. Installing it again would race first boot for the same app.
  6. with --store-lists, every one of those apps is in the store (the beta
     ISO carries all eight); with --then-install, one more of them installs
     offline from its carried image after first boot's app, so the store
     is proved with a second app alongside the first.
  7. with --websocket, a device signs in as Remote does (a bearer token from
     /api/shack/token) and opens that app's WebSocket through the front door
     on port 80: the handshake must come back 101 from the app, not 403.
     Every OpenHamShack Remote stream goes this way, and platform 2.0.0-beta.4
     refused all of them while plain requests worked (forward_auth turned
     its own check into a WebSocket request; Dock360 a76b02d), so nothing
     short of a real handshake through the installed Caddy proves it.

Stdlib only, so it runs on any build host.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)
    return bool(ok)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003 - urllib's signature
        return None


class Client:
    """A browser in miniature: cookies kept, redirects visible."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def request(self, path: str, data: dict | None = None, timeout: float = 30) -> tuple[int, dict, str]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers={"Accept": "text/html"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")

    def csrf(self, path: str = "/") -> str:
        _, _, html = self.request(path)
        found = re.search(r'data-csrf="([^"]*)"', html)
        return found.group(1) if found else ""


def wait_for(what: str, fn, seconds: float, every: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            if fn():
                return True
        except (OSError, urllib.error.URLError) as exc:
            last = exc
        time.sleep(every)
    print(f"  gave up waiting for {what} after {seconds:.0f}s ({last})", flush=True)
    return False


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True)
    ap.add_argument("--front", required=True)
    ap.add_argument("--app", default="openhamshack-fakebox")
    ap.add_argument("--wait", type=float, default=900)
    ap.add_argument("--first-boot", action="store_true",
                    help="the app is one first boot installs (build.sh --first-apps)")
    ap.add_argument("--store-lists", default="",
                    help="space-separated app ids the store must list (besides --app)")
    ap.add_argument("--then-install", default="",
                    help="with --first-boot: install this app from the store afterwards, offline")
    ap.add_argument("--websocket", default="",
                    help='"<app id> <path>": open that WebSocket through the front door as a signed-in device')
    ap.add_argument("--out", default="")
    ap.add_argument("--pages", default="",
                    help="a folder to save what an owner would see at the end: the store, the apps, discovery")
    args = ap.parse_args()
    admin = Client(args.admin)

    wait_for("the admin interface", lambda: admin.request("/healthz", timeout=5)[0] == 200, args.wait)

    # 1. Discovery, on both ports. Through the front door it goes via Caddy,
    # which only routes it once the API has written the edition's fragment.
    doc = {}
    def front_ready() -> bool:
        nonlocal doc
        doc = get_json(args.front + "/.well-known/openhamshack")
        return True
    wait_for("the front door", front_ready, 300)
    check("front door (80) serves /.well-known/openhamshack",
          doc.get("openhamshack") is not None and bool(doc.get("controllerId")),
          f"openhamshack={doc.get('openhamshack')} version={doc.get('version')} controllerId={doc.get('controllerId')}")
    try:
        direct = get_json(args.admin + "/.well-known/openhamshack")
    except (OSError, ValueError) as exc:
        direct = {"error": str(exc)}
    check("admin port (8800) serves the same document",
          direct.get("controllerId") == doc.get("controllerId") and bool(direct.get("controllerId")),
          f"controllerId={direct.get('controllerId')}")

    # 2. No administrator was baked in: every page leads to setup.
    status, headers, _ = admin.request("/login")
    check("a fresh controller sends /login to first-run setup",
          status in (302, 303, 307) and headers.get("location", headers.get("Location", "")).endswith("/setup"),
          f"{status} -> {headers.get('location', headers.get('Location'))}")
    status, _, html = admin.request("/setup")
    check("setup page loads", status == 200 and 'name="password_confirm"' in html, f"HTTP {status}")

    # 3. Make the administrator, sign out, and see the login page.
    password = "Ohs-" + secrets.token_urlsafe(12)
    status, headers, html = admin.request("/setup", {"username": "owner", "password": password,
                                                     "password_confirm": password})
    signed_in = status in (302, 303) and any(c.name for c in admin.jar)
    check("first-run setup creates the administrator", signed_in, f"HTTP {status}")
    token = admin.csrf("/")
    admin.request("/logout", {"_csrf": token})
    fresh = Client(args.admin)
    status, _, html = fresh.request("/login")
    check("admin login page loads", status == 200 and 'type="password"' in html, f"HTTP {status}")
    status, _, _ = fresh.request("/login", {"username": "owner", "password": password, "_csrf": ""})
    check("the administrator can sign in", status in (302, 303), f"HTTP {status}")
    admin = fresh

    # 4. The store lists the preloaded app. First boot uploads it after the
    # API starts, so it may take a little while to appear.
    def listed() -> bool:
        status, _, html = admin.request("/catalogue")
        return status == 200 and args.app in html
    check(f"the store lists {args.app}", wait_for("the preloaded app in the store", listed, args.wait, 10))
    if args.store_lists:
        # First boot uploads them one at a time, so the last can lag the first.
        want = args.store_lists.split()
        missing: list[str] = want
        def all_listed() -> bool:
            nonlocal missing
            status, _, html = admin.request("/catalogue")
            missing = [a for a in want if status != 200 or f"/catalogue/{a}?" not in html]
            return not missing
        check(f"the store lists all {len(want)} apps", wait_for("every preloaded app", all_listed, args.wait, 10),
              f"missing {missing}" if missing else " ".join(want))

    if args.first_boot:
        first_boot_app(args, admin, doc)
        if args.then_install:
            install_and_answer(args, admin, args.then_install)
        if args.websocket:
            websocket_through_front_door(args, "owner", password)
        save_pages(args, admin)
        return finish(args, doc)

    # 5. Install it from the store, offline, and see it answer.
    if not install_and_answer(args, admin, args.app):
        return finish(args)
    try:
        after = get_json(args.front + "/.well-known/openhamshack")
        check("discovery still answers after the install", bool(after.get("controllerId")),
              f"apps={[a.get('id') for a in after.get('apps', [])]}")
    except (OSError, ValueError) as exc:
        check("discovery still answers after the install", False, str(exc))
    if args.websocket:
        websocket_through_front_door(args, "owner", password)
    save_pages(args, admin)
    return finish(args, doc)


def install_and_answer(args, admin: Client, app: str) -> bool:
    """Install APP from the store, offline, and see it answer. False if it never started."""
    token = admin.csrf(f"/catalogue/{app}")
    status, headers, html = admin.request(f"/catalogue/{app}/install",
                                          {"_csrf": token, "autostart": "on", "start_now": "on"})
    location = headers.get("location", headers.get("Location", ""))
    job = re.search(r"/jobs/(\d+)", location)
    if not check(f"installing {app} starts a job", status in (302, 303) and job is not None,
                 f"HTTP {status} -> {location}"):
        return False
    tail = {}
    def finished() -> bool:
        nonlocal tail
        status, _, body = admin.request(f"/jobs/{job.group(1)}/tail")
        tail = json.loads(body) if status == 200 else {}
        return bool(tail) and not tail.get("running")
    wait_for("the install job", finished, 900, 5)
    log = tail.get("log", "")
    check(f"installing {app} with no network succeeds", tail.get("status") == "ok",
          f"status={tail.get('status')}; last lines: {' | '.join(log.strip().splitlines()[-4:])}")
    check(f"{app}'s carried image was loaded, not pulled", "pull" not in log.lower() or "load" in log.lower(),
          next((line for line in log.splitlines() if "image" in line.lower()), ""))

    def answers() -> bool:
        status, _, _ = admin.request(f"/app/{app}/", timeout=10)
        return status == 200
    check(f"{app} answers through the controller", wait_for("the app", answers, 180, 5))
    return True


def websocket_through_front_door(args, username: str, password: str) -> None:
    """7. A WebSocket handshake through port 80, carrying a device token.

    Raw socket rather than a library: the probe is stdlib only, and the
    question is the status line the front door sends back, which a client
    library hides behind its own exception. 101 with the right accept key
    means the platform checked the request and the app took the upgrade;
    403 is the beta.4 failure, where the check itself was refused.
    """
    app, _, path = args.websocket.partition(" ")
    path = path.strip() or "/"
    try:
        req = urllib.request.Request(args.front + "/api/shack/token", method="POST",
                                     headers={"Content-Type": "application/json"},
                                     data=json.dumps({"username": username, "password": password,
                                                      "deviceName": "boot-test probe", "deviceKind": "phone",
                                                      "clientId": "boot-test-probe"}).encode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read()).get("token", "")
    except (OSError, ValueError) as exc:
        token = ""
        print(f"  token request failed: {exc}", flush=True)
    if not check("a device signs in on the front door (/api/shack/token)", token.startswith("shackdev_"),
                 "token issued" if token else "no token"):
        return
    try:
        apps = get_json(args.front + "/.well-known/openhamshack").get("apps", [])
    except (OSError, ValueError):
        apps = []
    base = next((a.get("base", "") for a in apps if a.get("id") == app), "")
    if not check(f"discovery gives {app} an address on the front door", bool(base), f"base={base!r}"):
        return
    url = urllib.parse.urlsplit(args.front)
    target = "/" + base.strip("/") + "/" + path.lstrip("/")
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    head, status = "", 0
    try:
        with socket.create_connection((url.hostname, url.port or 80), timeout=15) as sock:
            sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {url.netloc}\r\n"
                          f"Authorization: Bearer {token}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                          f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        status = int(head.split(" ", 2)[1]) if head.startswith("HTTP/") else 0
    except (OSError, ValueError, IndexError) as exc:
        head = f"error: {exc}"
    headers = {k.strip().lower(): v.strip() for k, _, v in
               (line.partition(":") for line in head.split("\r\n")[1:]) if k}
    check(f"a WebSocket to {app}{path} through the front door is not refused (not 403)",
          status not in (0, 401, 403), f"HTTP {status}: {head.splitlines()[0] if head else 'no answer'}")
    check(f"the WebSocket to {app}{path} upgrades (101, accept key right)",
          status == 101 and headers.get("sec-websocket-accept") == want,
          f"HTTP {status}, upgrade={headers.get('upgrade')!r}, accept {'ok' if headers.get('sec-websocket-accept') == want else 'wrong/missing'}")


def first_boot_app(args, admin: Client, doc: dict) -> None:
    """5, for an app first boot installs: wait for that install, then use it.

    Discovery lists an app from the moment its install creates the row, so
    "listed" alone would pass half way through; it is "listed and running".
    A big carried image (QO-100 WB's is 600 MB) takes minutes to load on a
    slow disk, hence the long wait.
    """
    entry: dict = {}
    def installed() -> bool:
        nonlocal entry
        apps = get_json(args.front + "/.well-known/openhamshack").get("apps", [])
        entry = next((a for a in apps if a.get("id") == args.app), {})
        return bool(entry.get("running"))
    ok = wait_for(f"first boot to install {args.app}", installed, args.wait, 10)
    check(f"first boot installed {args.app} and discovery lists it running", ok,
          f"entry={entry or 'absent'}")
    if not ok:
        return

    # Running is the unit; answering is the app. Through /app/<id>/, the
    # mount the controller's own pages frame, which every installed app has
    # whether or not it is published on an address of its own.
    seen = {}
    def answers() -> bool:
        status, _, html = admin.request(f"/app/{args.app}/", timeout=10)
        seen.update(status=status, size=len(html))
        return status == 200 and len(html) > 0
    check(f"{args.app} answers through the controller", wait_for("the app", answers, 180, 5),
          f"HTTP {seen.get('status')}, {seen.get('size')} bytes")


def save_pages(args, admin: Client) -> None:
    """Evidence for a person: the pages as the controller served them, signed in."""
    if not args.pages:
        return
    import pathlib  # noqa: PLC0415 - only when asked
    out = pathlib.Path(args.pages); out.mkdir(parents=True, exist_ok=True)
    for name, path in (("dashboard", "/"), ("store", "/catalogue"), ("apps", "/modules")):
        status, _, html = admin.request(path)
        (out / f"{name}.html").write_text(f"<!-- GET {path}: HTTP {status} -->\n{html}")
    try:
        (out / "well-known.json").write_text(json.dumps(get_json(args.front + "/.well-known/openhamshack"), indent=2))
    except (OSError, ValueError) as exc:
        (out / "well-known.json").write_text(json.dumps({"error": str(exc)}))


def finish(args, doc: dict | None = None) -> int:
    failures = sum(1 for r in results if not r["ok"])
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"failures": failures, "controllerId": (doc or {}).get("controllerId"),
                       "results": results}, f, indent=2)
    print(f"{len(results) - failures} passed, {failures} failed", flush=True)
    return failures


if __name__ == "__main__":
    sys.exit(main())
