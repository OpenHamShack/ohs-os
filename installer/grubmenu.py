#!/usr/bin/env python3
"""The ISO's GRUB menu, written by mkiso.sh to /boot/grub/grub.cfg:

    python3 installer/grubmenu.py <version> > grub.cfg

A script of its own, rather than a heredoc in mkiso.sh, so that the menu can
be tested without building an ISO. The same file is read by both GRUBs on
the ISO (the BIOS El Torito image and the EFI one), so it uses nothing only
one of them has.

Started from the archived Classic appliance's installer/grubmenu.py at 50742ac. The Try
entries are gone (the live system no longer carries the controller) and an
unattended entry is added for CI.
"""

from __future__ import annotations

import sys

KERNEL = "/live/vmlinuz"
INITRD = "/live/initrd.img"
LIVE = "boot=live components"
# The unattended entry's disk. /dev/vda is a virtio disk, which only a
# virtual machine has: chosen by hand on a real PC the entry finds no such
# disk and the installer refuses (installer.py auto()), so the entry cannot
# erase a real machine's disk by accident.
CI_DISK = "/dev/vda"
CI_ARGS = f"{LIVE} console=tty0 console=ttyS0,115200n8 shack.install=auto shack.disk={CI_DISK}"
# A machine whose SMBIOS product name is this boots the unattended entry
# without anyone choosing it: QEMU's `-smbios type=1,product=openhamshack-ci`.
# A boot test cannot type at a menu reliably, and no real PC is named this.
CI_PRODUCT = "openhamshack-ci"


def entries(version: str) -> list[tuple[str, str]]:
    """(title, kernel arguments). The touch installer stays first and the
    default, as it always was: a medium booted with nobody watching then does
    nothing until two taps on a disk. Safe graphics follows (quiet splash
    off, so there is something to read when it does not work either), then
    the unattended entry, last so that nobody scrolling lands on it."""
    return [
        (f"Install OpenHamShack {version}", f"{LIVE} quiet splash"),
        (f"Install OpenHamShack {version} (safe graphics)", f"{LIVE} nomodeset"),
        (f"Unattended install to {CI_DISK} (virtual machines and CI; serial console)", CI_ARGS),
    ]


def menu(version: str) -> str:
    items = entries(version)
    ci_index = next(i for i, (_, args) in enumerate(items) if "shack.install=auto" in args)
    out = [
        # Five seconds: long enough to read the choices before Install starts.
        "set timeout=5",
        "set default=0",
        "insmod all_video",
        # The menu on the first serial port as well, when there is one; a PC
        # without one keeps the screen alone (terminal_output refuses a list
        # naming a terminal that does not exist, so it is guarded).
        "if serial --unit=0 --speed=115200; then",
        "    terminal_input console serial",
        "    terminal_output console serial",
        "fi",
        "insmod smbios",
        "if smbios --type 1 --get-string 5 --set shack_product; then",
        f'    if [ "$shack_product" = "{CI_PRODUCT}" ]; then',
        f"        set default={ci_index}",
        "        set timeout=1",
        "    fi",
        "fi",
    ]
    for title, args in items:
        out += [f'menuentry "{title}" {{', f"    linux {KERNEL} {args}", f"    initrd {INITRD}", "}"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.exit("usage: grubmenu.py <version>")
    sys.stdout.write(menu(sys.argv[1].strip()))
