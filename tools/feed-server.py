#!/usr/bin/env python3
"""Serve a feed root (tools/publish-feeds.sh --out) read-only over HTTP.

    feed-server.py <root> [--bind 192.0.2.140] [--port 8780]

A LAN mirror of openhamshack.com/feeds/ for stations that cannot wait for
the website: http://<bind>:<port>/feeds/platform-beta.json and so on. Plain
HTTP is enough because the feeds and the packages are signed; what this
must not do is say more than the site would, so it serves files and nothing
else - GET and HEAD only, no directory listings, nothing outside the root
(dotfiles included), no uploads.

Standard library only, so it runs as a systemd user service on any build
machine without a virtualenv (tools/openhamshack-feeds.service).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "OpenHamShackFeeds"
    sys_version = ""

    def list_directory(self, path):  # noqa: D102 - listings are off
        self.send_error(404)
        return None

    def send_head(self):
        # Hidden files (a .tmp mid-write, an editor's swap file) are not
        # published files; neither is any path that leaves the root.
        parts = self.path.split("?", 1)[0].split("/")
        if any(p.startswith(".") for p in parts if p):
            self.send_error(404)
            return None
        return super().send_head()

    def do_POST(self):  # noqa: N802 - only reading
        self.send_error(405)

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):  # one line per request to the journal
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    args = ap.parse_args()
    root = os.path.realpath(args.root)
    handler = functools.partial(Handler, directory=root)
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        print(f"serving {root} on http://{args.bind}:{args.port}/", file=sys.stderr, flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
