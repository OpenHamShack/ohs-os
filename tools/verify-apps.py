#!/usr/bin/env python3
"""Check every app package for the preloaded store before it goes into an ISO.

    PYTHONPATH=<dock360 src>/src python3 -I tools/verify-apps.py <dock360 src> <apps dir>

A controller checks each package when first boot uploads it, and refuses one
that is unsigned, signed by a key it does not trust for apps, or carries an
image that does not match its declaration. That refusal happens on a user's
PC, in a journal nobody reads, after the ISO has shipped. This runs the same
checks with the same code (Dock360's own unpack, against the keys the
OpenHamShack edition installs for apps) at build time, where a failure stops
the build instead.

Prints one line per package: id, version, signing key fingerprint, image ref.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    src, apps = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(src / "src"))
    from dock360 import bundles, signing  # noqa: PLC0415 - path set just above

    keydir = src / "trusted-keys" / "editions" / "openhamshack" / "modules"
    keys = signing._read_keys(keydir, signing.SCOPE_MODULES)  # noqa: SLF001 - exactly the edition's app keys
    if not keys:
        print(f"no OpenHamShack modules key in {keydir}", file=sys.stderr)
        return 1
    packages = sorted(apps.glob("*.d360mod"))
    if not packages:
        print(f"no .d360mod packages in {apps}", file=sys.stderr)
        return 1
    failed = 0
    for pkg in packages:
        work = Path(tempfile.mkdtemp(prefix="verify-app.", dir=apps))
        try:
            bundle = bundles.unpack(pkg.read_bytes(), pkg.name, work, require_signature=False)
            check = signing.verify_tree(bundle.directory, keys=keys)
            image = bundles.carried_image(bundle.directory)
            if not check.valid:
                print(f"REFUSED {pkg.name}: {'; '.join(check.problems) or check.status}")
                failed = 1
            elif not image:
                # Without a carried image the install would pull from a
                # registry, which a station on a hilltop cannot reach.
                print(f"REFUSED {pkg.name}: carries no container image (package it with --with-image)")
                failed = 1
            else:
                print(f"ok {bundle.id} {bundle.manifest.version} signed by {check.key.fingerprint} image {image['ref']}")
        except Exception as exc:  # noqa: BLE001 - every reason is a refusal, said plainly
            print(f"REFUSED {pkg.name}: {exc}")
            failed = 1
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return failed


if __name__ == "__main__":
    sys.exit(main())
