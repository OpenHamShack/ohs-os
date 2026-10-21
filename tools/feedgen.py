#!/usr/bin/env python3
"""The JSON half of tools/publish-feeds.sh: versions, feed files and checks.

    feedgen.py next-version  <PLATFORM_VERSION at the ref> [published ...]
    feedgen.py newest [version ...]
    feedgen.py engine-version <VERSION at the ref> [--when YYYYmmddHHMM]
    feedgen.py platform  <web root> --channel C --version V --engine E --package P [--notes N]
    feedgen.py catalogue <web root> <dock360 src> <apps dir>
    feedgen.py check     <web root> <dock360 src> [--channel C]

The shell script does the git and the signing (with dock360ctl, where the
keys are read); this file does what is easier to get right, and to test, in
Python. The format it writes is the station's: docs/openhamshack-feeds.md in
the Dock360 edition, read by src/dock360/shack/feeds.py. `check` runs that
very reader over the signed result, so a feed this tool accepts is one a
station accepts.

Everything lives under <web root>/feeds/, laid out exactly as it will sit
on openhamshack.com/feeds/ (platform-<channel>.json, catalogue.json, their
.sig files, packages/, apps/), with package addresses relative to the feed.
A LAN mirror and the website are then the same directory, copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: How long a feed stays good. A station refuses an expired feed, so a site
#: that stops publishing is noticed rather than trusted for ever; four weeks
#: is long enough that a missed week does not strand anyone, short enough
#: that a dead site shows within the month. Re-publish (even unchanged) at
#: least that often.
EXPIRES_DAYS = 28

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class FeedGenError(RuntimeError):
    pass


def version_key(version: str) -> tuple:
    """The station's ordering (shack/feeds.py version_key): pre-releases
    before the release, their numeric parts compared as numbers."""
    m = _VERSION.match(str(version or "").strip())
    if not m:
        raise FeedGenError(f"{version!r} is not a version")
    pre = m.group(4)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, parts)


def next_version(at_ref: str, published: list[str]) -> str:
    """The platform version the next publish presents.

    The newest of what the ref says and what has already been published,
    with its last numeric part raised by one: 2.0.0-beta.1 -> 2.0.0-beta.2.
    The ref's own PLATFORM_VERSION is only ever raised by a person, when the
    beta moves to a new line (2.1.0-beta.1, or 2.0.0 itself); every publish
    in between is numbered here, so no feature commit touches the file and two
    sessions cannot set it over each other (~/.claude/CLAUDE.md, Releases).
    """
    newest = max([at_ref, *published], key=version_key)
    m = _VERSION.match(newest)
    pre = m.group(4)
    if pre is None:
        # A final release was published; the next publish is its first patch.
        return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    parts = pre.split(".")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].isdigit():
            parts[i] = str(int(parts[i]) + 1)
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}-{'.'.join(parts)}"
    return f"{newest}.1"


def engine_version(base: str, when: str | None = None) -> str:
    """The engine (Dock360) version stamped into a published package.

    It has to differ from every engine a station has run: the updater
    installs each release into /opt/dock360/releases/<version> and refuses
    to stage over the one in use, so republishing the edition at the
    engine's own VERSION (0.12.25 on the box today) cannot be applied.

    It is <base with patch + 1>-ohs.<UTC minute>: above the base it was cut
    from, so an app's `min_dock360: <base>` still holds (a pre-release of the
    base itself would sort below it and fail that check); below the next real
    Dock360 release; and rising with every publish without a counter to keep,
    because the timestamp is fixed-width and Dock360 compares a pre-release
    suffix as text. A station never sees this number as its version - it is
    told the platform version - so the oddity costs nothing.
    """
    m = _VERSION.match(base.strip())
    if not m or m.group(4):
        raise FeedGenError(f"engine VERSION {base!r} is not a plain x.y.z")
    when = when or datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    if not re.match(r"^\d{12}$", when):
        raise FeedGenError(f"--when {when!r} is not YYYYmmddHHMM")
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}-ohs.{when}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _stamp(old: dict | None) -> dict:
    """sequence, issued, expires. The sequence is the Unix time, and always
    above the previous file's: a station refuses a lower one, so it must
    rise even if the clock here was set back."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sequence = max(int(time.time()), int((old or {}).get("sequence", 0)) + 1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {"sequence": sequence, "issued": now.strftime(fmt),
            "expires": (now + timedelta(days=EXPIRES_DAYS)).strftime(fmt)}


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, feed: dict) -> None:
    # The signature covers these exact bytes; write once, never re-serialise.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(feed, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_platform(root: Path, channel: str, version: str, engine: str, package: Path, notes: str = "") -> dict:
    """Write feeds/platform-<channel>.json listing one release.

    Only the newest release is listed: a station offers only releases above
    its own version and applies one package, which carries the whole
    platform, so older entries would only be clutter. Older package files are
    left where they are (a station part-way through a download keeps its
    file). A version not above the one already published is refused - the
    station would never offer it."""
    feeds = root / "feeds"
    path = feeds / f"platform-{channel}.json"
    old = _read(path)
    for rel in (old or {}).get("releases", []):
        if version_key(version) <= version_key(rel["version"]):
            raise FeedGenError(f"{version} is not above {rel['version']}, already in {path.name}")
    target = feeds / "packages" / package.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if package.resolve() != target.resolve():
        shutil.copy2(package, target)
    feed = {"feed": "openhamshack-platform", "schema": 1, "channel": channel, **_stamp(old),
            "releases": [{"version": version, "engine": engine, "url": f"packages/{target.name}",
                          "sha256": sha256(target), "size": target.stat().st_size, "notes": notes}]}
    _write(path, feed)
    return feed


def _inspect_app(pkg: Path, src: Path) -> dict:
    """id, version, name, kind, summary of a signed app package, refusing one
    a station would refuse: not signed by the edition's app key, or carrying
    no container image (a station with no route out could not install it).
    The same checks with the same code as tools/verify-apps.py."""
    sys.path.insert(0, str(src / "src"))
    from dock360 import bundles, signing  # noqa: PLC0415 - path set just above

    keys = signing._read_keys(src / "trusted-keys/editions/openhamshack/modules",  # noqa: SLF001
                              signing.SCOPE_MODULES)
    work = Path(tempfile.mkdtemp(prefix="feedgen."))
    try:
        bundle = bundles.unpack(pkg.read_bytes(), pkg.name, work, require_signature=False)
        check = signing.verify_tree(bundle.directory, keys=keys)
        if not check.valid:
            raise FeedGenError(f"{pkg.name} is not signed by the OpenHamShack modules key: "
                               f"{'; '.join(check.problems) or check.status}")
        if not bundles.carried_image(bundle.directory):
            raise FeedGenError(f"{pkg.name} carries no container image (package it with --with-image)")
        m = bundle.manifest
        shack = m.openhamshack if isinstance(getattr(m, "openhamshack", None), dict) else {}
        return {"id": bundle.id, "version": m.version, "name": m.name or bundle.id,
                "kind": shack.get("kind", ""), "summary": m.summary or ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_catalogue(root: Path, src: Path, apps: Path, inspect=_inspect_app) -> dict:
    """Write feeds/catalogue.json from the signed packages in <apps>.

    The catalogue is merged, not rebuilt: an app already published stays
    listed even when this publish's apps folder lacks it (the folder is a
    build area, and an app disappearing from every station's store because a
    build directory was cleaned would be a strange way to withdraw it). A
    package replaces the entry for its id when its version is the same or
    newer; an older one is skipped with a message."""
    feeds = root / "feeds"
    path = feeds / "catalogue.json"
    old = _read(path)
    entries = {a["id"]: a for a in (old or {}).get("apps", [])}
    (feeds / "apps").mkdir(parents=True, exist_ok=True)
    for pkg in sorted(apps.glob("*.d360mod")):
        info = inspect(pkg, src)
        have = entries.get(info["id"])
        if have and version_key(info["version"]) < version_key(have["version"]):
            print(f"skipped {pkg.name}: {have['version']} is already published", file=sys.stderr)
            continue
        target = feeds / "apps" / f"{info['id']}-{info['version']}.d360mod"
        shutil.copy2(pkg, target)
        entries[info["id"]] = {**info, "url": f"apps/{target.name}", "sha256": sha256(target),
                               "size": target.stat().st_size}
    if not entries:
        raise FeedGenError(f"no app packages in {apps} and none published before")
    feed = {"feed": "openhamshack-catalogue", "schema": 1, **_stamp(old),
            "apps": [entries[k] for k in sorted(entries)]}
    _write(path, feed)
    return feed


def check(root: Path, src: Path, channel: str) -> list[str]:
    """Read the signed feeds back exactly as a station does: shack.feeds.verify
    against the edition's own public keys, each for its own purpose, and each
    listed file's SHA-256 against the file on disk."""
    sys.path.insert(0, str(src / "src"))
    from dock360 import signing  # noqa: PLC0415
    from dock360.shack import feeds  # noqa: PLC0415

    keydir = src / "trusted-keys/editions/openhamshack"
    out = []
    for kind, name, scope, sub in ((feeds.PLATFORM, f"platform-{channel}.json", signing.SCOPE_UPDATES, "updates"),
                                   (feeds.CATALOGUE, "catalogue.json", signing.SCOPE_MODULES, "modules")):
        path = root / "feeds" / name
        if not path.exists():
            continue
        keys = signing._read_keys(keydir / sub, scope)  # noqa: SLF001
        # verify() asks signing.trusted_keys(scope=...), which reads the host's
        # store; point it at the edition's keys for this one call.
        original = signing.trusted_keys
        signing.trusted_keys = lambda directory=None, scope=signing.SCOPE_ALL, _k=keys: _k
        try:
            feed, signer = feeds.verify(kind, path.read_bytes(), (path.parent / (name + ".sig")).read_text())
        finally:
            signing.trusted_keys = original
        for item in feed.get("releases", feed.get("apps", [])):
            f = path.parent / item["url"]
            if sha256(f) != item["sha256"]:
                raise FeedGenError(f"{item['url']} does not match {name}")
            out.append(f"{name}: {item.get('id', 'platform')} {item['version']} sha256 {item['sha256']} "
                       f"{item.get('size', 0)} bytes")
        out.append(f"{name}: sequence {feed['sequence']}, expires {feed['expires']}, signed by {signer}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next-version"); p.add_argument("at_ref"); p.add_argument("published", nargs="*")
    p = sub.add_parser("newest"); p.add_argument("versions", nargs="*")
    p = sub.add_parser("engine-version"); p.add_argument("base"); p.add_argument("--when")
    p = sub.add_parser("platform"); p.add_argument("root", type=Path)
    p.add_argument("--channel", required=True); p.add_argument("--version", required=True)
    p.add_argument("--engine", required=True); p.add_argument("--package", required=True, type=Path)
    p.add_argument("--notes", default="")
    p = sub.add_parser("catalogue"); p.add_argument("root", type=Path); p.add_argument("src", type=Path)
    p.add_argument("apps", type=Path)
    p = sub.add_parser("check"); p.add_argument("root", type=Path); p.add_argument("src", type=Path)
    p.add_argument("--channel", default="beta")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "next-version":
            print(next_version(args.at_ref, args.published))
        elif args.cmd == "newest":
            print(max(args.versions, key=version_key) if args.versions else "")
        elif args.cmd == "engine-version":
            print(engine_version(args.base, args.when))
        elif args.cmd == "platform":
            write_platform(args.root, args.channel, args.version, args.engine, args.package, args.notes)
        elif args.cmd == "catalogue":
            write_catalogue(args.root, args.src, args.apps)
        else:
            print("\n".join(check(args.root, args.src, args.channel)))
    except FeedGenError as exc:
        print(f"feedgen: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
