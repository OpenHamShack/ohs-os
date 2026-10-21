#!/usr/bin/env python3
"""Gate 3's secret scanner: every file of an unpacked ISO, and the raw disk.

    secretscan.py files <out.tsv> <needle file>... -- <root>...
    secretscan.py raw <out.tsv> <disk image> <needle file>...
    secretscan.py classify <files.tsv> <raw.tsv> <summary.txt> [--dock360-src DIR]
                           [--root ROOT]... [--apps DIR] [--ignore-under DIR]

Never prints or stores a secret value: a hit is a kind, a path and a byte
offset. Run by tools/gate3-scan.sh, which unpacks every layer of the ISO
first; written for the beta.4 ISO's gate 3 (09/10/2026) and kept so the next
ISO gets the same scan rather than a remembered one.

Two kinds of search:
- patterns for any credential a build could leak (PEM private keys with a
  body, cloud and chat tokens, credentials in URLs, password assignments) and
  for build-host traces (/home/user, 20i);
- needles: the keys this build host actually holds (the OpenHamShack master
  and modules signing keys, its SSH key, the 20i upload key's public half),
  searched byte for byte in every form a leak could take - the base64 text,
  the decoded bytes, hex. A pattern can miss a key in an odd format; a needle
  cannot.

`classify` sorts the hits so a person reviews only what matters: a hit in a
file Debian shipped unchanged (its md5 matches the image's own dpkg
md5sums), or in a file identical to the Dock360 source the image was built
from (its tests carry constructed fixtures), is labelled as such; strong
kinds left over need a person's eyes; any needle hit fails outright.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

PATTERNS = {
    # A PEM header followed by a base64 body is a key; the header alone is a
    # string constant inside ssh, openssl, python's ssl module and so on.
    "pem-private-key": rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----\r?\n(?:[A-Za-z0-9-]+: [^\n]*\n)*\r?\n?[A-Za-z0-9+/=]{40,}",
    "putty-key": rb"PuTTY-User-Key-File-\d: ",
    "aws-access-key": rb"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])",
    "github-token": rb"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})",
    "slack-token": rb"xox[abprs]-[0-9A-Za-z-]{10,}",
    "google-api-key": rb"AIza[0-9A-Za-z_-]{35}",
    "anthropic-key": rb"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai-key": rb"sk-(?:proj-)?[A-Za-z0-9]{40,}",
    "tailscale-key": rb"tskey-[a-z]+-[A-Za-z0-9-]{10,}",
    "stripe-live": rb"(?:sk|rk)_live_[0-9A-Za-z]{16,}",
    "jwt": rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}",
    "wireguard-private": rb"PrivateKey\s*=\s*[A-Za-z0-9+/]{42,43}=",
    "url-credentials": rb"[a-z][a-z0-9+.-]{1,10}://[A-Za-z0-9._%-]{1,64}:[^@\s/'\"<>{}$]{6,}@[A-Za-z0-9.-]+\.[a-z]{2,}",
    "password-assignment": rb"(?i)(?:password|passwd|api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token)[\"']?\s*[:=]\s*[\"'][^\"'\s$%{}<>]{8,}[\"']",
    "home-path": rb"/home/user",
    "20i": rb"(?i)stackcp|20i\.com|api\.20i",
}
RX = [(k, re.compile(v)) for k, v in PATTERNS.items()]
# Kinds that are a credential if they are anything; the rest (assignments,
# URL examples, the home path) are mostly documentation and are listed for
# hygiene, not failed on.
STRONG = {"pem-private-key", "putty-key", "aws-access-key", "github-token", "slack-token", "google-api-key",
          "anthropic-key", "openai-key", "tailscale-key", "stripe-live", "jwt", "wireguard-private"}
RAW_KINDS = {"pem-private-key", "putty-key", "aws-access-key", "github-token", "anthropic-key",
             "tailscale-key", "wireguard-private", "20i"}
SKIP_DIRS = {"proc", "sys"}
MAX = 256 * 1024 * 1024
NEEDLES: list[tuple[str, bytes]] = []
# Dock360's key files begin their data line with this format label. It is
# public text (Dock360's signing.py writes it), and matching it as a needle
# found Dock360's own source, not a key - so labels are not needles.
LABEL = re.compile(rb"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def load_needles(paths: list[str]) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for p in paths:
        name = os.path.basename(p)
        text = open(p, "rb").read()
        if text.startswith(b"ssh-ed25519 "):
            # A public key whose private half lives elsewhere (the 20i upload
            # key is on shack). Its 32 raw bytes also sit inside the private
            # key file, so finding neither rules out both.
            blob = text.split()[1]
            out += [(f"{name}:pub-blob", blob), (f"{name}:pub-raw", base64.b64decode(blob)[-32:])]
        elif b"BEGIN OPENSSH PRIVATE KEY" in text:
            lines = [ln for ln in text.splitlines() if ln and not ln.startswith(b"-----")]
            raw = base64.b64decode(b"".join(lines))
            out += [(f"{name}:b64-line{i}", ln) for i, ln in enumerate(lines) if len(ln) >= 40]
            # The private section's ed25519 key: 64 bytes after a 32-byte public key.
            m = re.search(rb"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20(.{32})\x00\x00\x00\x40(.{32})", raw, re.S)
            if m:
                seed = m.group(2)
                out += [(f"{name}:seed-raw", seed), (f"{name}:seed-hex", seed.hex().encode()),
                        (f"{name}:seed-b64", base64.b64encode(seed)[:40])]
        else:
            for line in text.splitlines():
                if line.startswith(b"#") or not line.strip():
                    continue
                for tok in re.findall(rb"[A-Za-z0-9+/=_-]{16,}", line):
                    if LABEL.match(tok):
                        continue
                    out.append((f"{name}:token", tok))
                    for dec in (base64.b64decode, base64.urlsafe_b64decode):
                        try:
                            raw = dec(tok + b"=" * (-len(tok) % 4))
                        except ValueError:
                            continue
                        if len(raw) >= 16:
                            out += [(f"{name}:raw", raw), (f"{name}:hex", raw.hex().encode())]
    seen: set[bytes] = set()
    res = []
    for k, v in out:
        if v not in seen and len(v) >= 16:
            seen.add(v)
            res.append((k, v))
    return res


def scan(path: str) -> list[tuple[str, str, int]]:
    hits: list[tuple[str, str, int]] = []
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return hits
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(MAX)
        if size > MAX:
            hits.append((path, "truncated-scan", size))
    except OSError as exc:
        return [(path, "unreadable:" + exc.__class__.__name__, 0)]
    for kind, rx in RX:
        for m in rx.finditer(data):
            hits.append((path, kind, m.start()))
            if len(hits) > 50:
                break
    for kind, needle in NEEDLES:
        i = data.find(needle)
        if i >= 0:
            hits.append((path, "KNOWN-SECRET:" + kind, i))
    return hits


def _init(n: list[tuple[str, bytes]]) -> None:
    global NEEDLES
    NEEDLES = n


def walk(roots: list[str]):
    for root in roots:
        for d, dirs, files in os.walk(root):
            if d == root:
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                yield os.path.join(d, f)


def cmd_files(argv: list[str]) -> int:
    out, sep = argv[0], argv.index("--")
    needles = load_needles(argv[1:sep])
    roots = argv[sep + 1:]
    print(f"{len(needles)} needles from {sep - 1} key files", file=sys.stderr)
    n = 0
    with open(out, "w") as o, ProcessPoolExecutor(min(6, os.cpu_count() or 2), initializer=_init,
                                                  initargs=(needles,)) as ex:
        for hits in ex.map(scan, walk(roots), chunksize=64):
            n += 1
            for p, k, off in hits:
                o.write(f"{k}\t{p}\t{off}\n")
    print(f"scanned {n} files", file=sys.stderr)
    with open(out + ".count", "w") as o:
        o.write(f"{n}\n")
    return 0


def cmd_raw(argv: list[str]) -> int:
    """Every byte of a disk image, free space included: what a file walk
    cannot see (deleted files, slack) is where a removed key would linger."""
    out, img, keys = argv[0], argv[1], argv[2:]
    needles = load_needles(keys)
    rx = [(k, r) for k, r in RX if k in RAW_KINDS]
    chunk, overlap = 64 << 20, 4096
    hits: dict[str, set[int]] = {}
    pos, prev = 0, b""
    with open(img, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            d, base = prev + b, pos - len(prev)
            for k, r in rx:
                for m in r.finditer(d):
                    hits.setdefault(k, set()).add(base + m.start())
            for k, n in needles:
                i = d.find(n)
                while i >= 0:
                    hits.setdefault("KNOWN-SECRET:" + k, set()).add(base + i)
                    i = d.find(n, i + 1)
            prev, pos = d[-overlap:], pos + len(b)
    with open(out, "w") as o:
        for k, v in sorted(hits.items()):
            o.write(f"{k}\t{len(v)}\t{','.join(str(x) for x in sorted(v)[:5])}\n")
        o.write(f"scanned-bytes\t{pos}\t\n")
    return 0


def _dpkg_md5s(root: str) -> dict[str, str]:
    """Debian's own md5 of every file it installed under root: md5sums for
    ordinary files, and the status file's Conffiles for configuration (the
    installer root's /etc/chromium.d/apikeys, Debian's public Google key,
    is one)."""
    md5s: dict[str, str] = {}
    info = os.path.join(root, "var/lib/dpkg/info")
    if os.path.isdir(info):
        for name in os.listdir(info):
            if name.endswith(".md5sums"):
                for line in open(os.path.join(info, name), encoding="utf-8", errors="replace"):
                    digest, _, rel = line.strip().partition("  ")
                    md5s[rel] = digest
    status = os.path.join(root, "var/lib/dpkg/status")
    if os.path.isfile(status):
        for m in re.finditer(r"^ (/\S+) ([0-9a-f]{32})(?: obsolete)?$",
                             open(status, encoding="utf-8", errors="replace").read(), re.M):
            md5s[m.group(1).lstrip("/")] = m.group(2)
    return md5s


def _layer_roots(apps: str) -> dict[str, dict[str, str]]:
    """Each container layer of each unpacked app as a root of its own, with
    the md5s of every layer of that app: a library unpacked in one layer is
    registered in the dpkg database a later layer writes."""
    roots: dict[str, dict[str, str]] = {}
    for app in sorted(os.listdir(apps)) if os.path.isdir(apps) else []:
        layers = os.path.join(apps, app, "layers")
        if not os.path.isdir(layers):
            continue
        union: dict[str, str] = {}
        dirs = [os.path.join(layers, n) for n in sorted(os.listdir(layers))]
        for d in dirs:
            union.update(_dpkg_md5s(d))
        for d in dirs:
            roots[d] = union
    return roots


def _same_as_source(path: str, src: str) -> bool:
    """Is this file byte for byte a file of the Dock360 source tree, wherever
    the release put it? Matched by the longest path suffix that exists."""
    parts = path.split(os.sep)
    for i in range(1, len(parts)):
        candidate = os.path.join(src, *parts[i:])
        if os.path.isfile(candidate):
            try:
                return open(candidate, "rb").read() == open(path, "rb").read()
            except OSError:
                return False
    return False


def cmd_classify(argv: list[str]) -> int:
    files_tsv, raw_tsv, summary = argv[:3]
    src, roots, ignore, apps = "", [], [], ""
    rest = argv[3:]
    while rest:
        flag, value, rest = rest[0], rest[1], rest[2:]
        if flag == "--dock360-src":
            src = value
        elif flag == "--root":
            roots.append(value)
        elif flag == "--apps":
            apps = value
        elif flag == "--ignore-under":
            ignore.append(value)
        else:
            raise SystemExit(f"unknown option {flag}")
    md5s = {r: _dpkg_md5s(r) for r in roots}
    md5s.update(_layer_roots(apps))
    rows = [ln.rstrip("\n").split("\t") for ln in open(files_tsv) if ln.strip()]
    known, review, stock, source, weak, other = [], [], [], [], {}, []
    for kind, path, off in rows:
        if any(path.startswith(d) for d in ignore):
            continue
        if kind.startswith("KNOWN-SECRET"):
            known.append((kind, path, off))
        elif kind in STRONG:
            label = ""
            for r in md5s:
                if path.startswith(r.rstrip("/") + "/"):
                    rel = os.path.relpath(path, r)
                    want = md5s[r].get(rel)
                    try:
                        if want and hashlib.md5(open(path, "rb").read()).hexdigest() == want:
                            label = "debian-stock"
                    except OSError:
                        pass
            if not label and src and _same_as_source(path, src):
                label = "dock360-source"
            (stock if label == "debian-stock" else source if label else review).append((kind, path, off))
        elif kind in PATTERNS:
            weak.setdefault(kind, []).append(path)
        else:
            other.append((kind, path, off))
    raw_known, raw_lines = [], []
    for ln in open(raw_tsv):
        kind, count, offsets = (ln.rstrip("\n").split("\t") + ["", ""])[:3]
        raw_lines.append(f"  {kind}: {count}" + (f" (first offsets {offsets})" if offsets else ""))
        if kind.startswith("KNOWN-SECRET"):
            raw_known.append(kind)
    verdict = "FAIL" if known or raw_known else "REVIEW" if review else "PASS"
    with open(summary, "w") as o:
        o.write(f"verdict {verdict}\n")
        o.write(f"needle hits in files: {len(known)}; in the raw disk: {len(raw_known)}\n")
        for kind, path, off in known:
            o.write(f"  KNOWN {kind} {path} @{off}\n")
        o.write(f"strong kinds needing a person: {len(review)}\n")
        for kind, path, off in review:
            o.write(f"  {kind} {path} @{off}\n")

        def grouped(title: str, hits: list) -> None:
            o.write(f"{title}: {len(hits)}\n")
            by: dict[tuple[str, str], int] = {}
            for kind, path, _ in hits:
                by[(kind, path)] = by.get((kind, path), 0) + 1
            for (kind, path), n in sorted(by.items()):
                o.write(f"  {kind} x{n} {path}\n")
        grouped("strong kinds in files Debian shipped unchanged (dpkg md5 matches)", stock)
        grouped("strong kinds in files identical to the Dock360 source", source)
        o.write("weak kinds (documentation, examples, build-host traces), counted:\n")
        for kind, paths in sorted(weak.items()):
            o.write(f"  {kind}: {len(paths)} in {len(set(paths))} files\n")
            for p in sorted(set(paths))[:40]:
                o.write(f"    {p}\n")
        o.write(f"not fully scanned: {len(other)}\n")
        for kind, path, off in other:
            o.write(f"  {kind} {path} {off}\n")
        o.write("raw disk image:\n" + "\n".join(raw_lines) + "\n")
    print(verdict)
    return {"PASS": 0, "REVIEW": 3, "FAIL": 1}[verdict]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    cmd, argv = sys.argv[1], sys.argv[2:]
    return {"files": cmd_files, "raw": cmd_raw, "classify": cmd_classify}[cmd](argv)


if __name__ == "__main__":
    sys.exit(main())
