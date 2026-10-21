# openhamshack-os

The OpenHamShack controller image: a hybrid UEFI/BIOS installer ISO for any x64 PC, built from Debian Trixie with the Dock360 OpenHamShack edition and the store preloaded.

- [docs/BUILD.md](docs/BUILD.md): building the ISO, its inputs and outputs, and the QEMU boot test.
- [docs/INSTALL.md](docs/INSTALL.md): installing it (Rufus DD mode, Ventoy, DVD, BIOS or UEFI, Secure Boot off).
- [docs/UPDATES.md](docs/UPDATES.md): updating an installed controller from the signed feeds, publishing them (`tools/publish-feeds.sh`), and what an update cannot carry.

`build.sh` is the entry point; `image/` makes the controller disk and the ISO, `installer/` is what the ISO runs, `tools/` packages and checks store apps, `tests/` boots it all in QEMU.

Started from the archived Classic appliance's equivalents (mkimage.sh, mkiso.sh, installer/) at 50742ac.

Licence: GPL-3.0-or-later.
