# Building the OpenHamShack controller ISO

One command makes a hybrid installer ISO: it boots UEFI or legacy BIOS, from
a DVD, from a USB stick written raw (Rufus DD mode, Etcher, `dd`), or copied
onto a Ventoy stick, and installs the OpenHamShack controller (Debian Trixie
with the Dock360 OpenHamShack edition) to the PC's internal disk.

## What you need

A Debian 13 (trixie) amd64 machine with sudo, network, and about 25 GB free
for the work directory. Built and tested on ohs-dev (office VM 140).

```
sudo apt-get install debootstrap xorriso squashfs-tools mtools dosfstools \
    parted e2fsprogs rsync zstd grub-efi-amd64-bin grub-pc-bin \
    podman qemu-system-x86 qemu-utils ovmf python3-venv
```

`podman` is only for packaging apps; `qemu-*` and `ovmf` only for the boot
test.

## Inputs

| Input | What | Where it comes from |
|---|---|---|
| Dock360 | the engine with the OpenHamShack edition | `--dock360 <repo> --ref <branch>`; taken with `git archive` of the commit, so a dirty working tree can never reach an ISO. The commit goes into `BUILDINFO`. |
| Store apps | signed `.d360mod` packages carrying their container images | `--apps <dir>`, made by `tools/package-app.sh` and checked by `tools/verify-apps.py` before the build starts |
| First-boot apps | store apps installed at the controller's first start | `--first-apps "<ids>"`, default `shack-qo100wb` (QO-100 WB); an id with no package in `--apps` is skipped with a message |
| Update feeds | where the controller looks for platform and app updates, preset once at first boot and never over an administrator's value | `--feeds-base <URL>`, default `https://openhamshack.com/feeds/`; `""` for none. See [UPDATES.md](UPDATES.md) |
| Debian | packages from `deb.debian.org` (main, contrib, non-free-firmware), Caddy from `trixie-backports` | the network, at build time only |

The modules signing key is used only by `tools/package-app.sh`, on the
build machine. It never enters the image, the ISO or this repository. The
image carries only the OpenHamShack **public** keys the edition already
trusts (`trusted-keys/editions/openhamshack/`).

### Packaging an app for the store

```
python3 -m venv ~/build/iso/venv
git -C ~/dock360 archive feature/ohs-edition | tar -x -C ~/build/iso/dock360-src
(cd ~/build/iso/dock360-src && ~/build/iso/venv/bin/pip install -r requirements.txt)

BUILD_DIR=~/build/iso DOCK360_PY=~/build/iso/venv/bin/python \
  tools/package-app.sh ~/dock360-apps-openhamshack-fakebox openhamshack-fakebox ~/build/iso/apps \
    --key ~/.config/openhamshack/keys/ohs-modules.key --dock360 ~/build/iso/dock360-src
```

It clones the app's repository at `--ref` (default `HEAD`), builds the image
with rootless podman as `localhost/dock360-<id>:<version>`, and writes
`<id>-<version>.d360mod` with the image inside, signed, plus a `.sha256` and
a line in `SOURCES`. Until the eight beta apps exist (M3), the fakebox app is
the stand-in that proves the path.

## Build

```
sudo ./build.sh --dock360 ~/dock360 --ref hub/feature/ohs-edition \
    --apps ~/build/iso/apps --out ~/build/iso/out --work ~/build/iso/work \
    --python ~/build/iso/venv/bin/python
```

About 25 minutes on ohs-dev, most of it debootstrap, apt and pip. Keep
`--work` off `/tmp`: Debian 13's `/tmp` is RAM and the work tree is
gigabytes. The work directory is removed when the build finishes.

### What it does

1. **Controller image** (`image/mkimage.sh`, `ROLE=system`): debootstrap,
   kernel and firmware, then Dock360's own `install.sh` with
   `EDITION=openhamshack`, run unmodified inside the chroot (systemctl in a
   chroot enables units and ignores starts). Everything that needs the
   network happens here, so the installed controller needs none. Then
   whatever install.sh made that must be per controller is removed: the
   database (with the administrator it always creates, the controller id,
   the proxy hop secret), the station screen's token, `secrets/`. They are
   made on the controller instead (below). Disk: GPT with a 1 MiB BIOS boot
   partition (EF02), a 256 MiB ESP and an ext4 root; GRUB installed for
   both `x86_64-efi` (removable path, no NVRAM entry) and `i386-pc`.
2. **Installer root** (`ROLE=installer`): live-boot, sway, Chromium and the
   installer; no Dock360.
3. **ISO** (`image/mkiso.sh`): squashfs, the controller image zstd-compressed
   beside it in `/openhamshack/` with a manifest of its SHA-256s, a BIOS
   El Torito core image (`i386-pc-eltorito`), a UEFI El Torito FAT image
   that is also appended as GPT partition 2, and GRUB's `boot_hybrid.img` as
   the MBR (`--grub2-mbr ... --grub2-boot-info`).

### On the controller

* **First start** (`openhamshack-firstboot.service`): makes the database,
  mints the station screen's device token, uploads each preloaded package
  into the store (the signature is checked again, by the controller's own
  rules), installs `--first-apps`, and only once each is running and
  healthy deletes the preload copies and writes
  `/var/lib/openhamshack/firstboot.done`. Offline. Until then it runs at
  every boot, so a power cut mid-install is finished next time: packages
  are uploaded again, a half-installed app is repaired with
  `dock360ctl update` (or removed and installed again), and every step is
  synced to disk before the next one relies on it.
* **The administrator** is created by whoever first opens
  `http://<controller>:8800/` (first-run setup). Nothing is baked in.
* `openhamshack-podman-pause.service` starts the dock360 account's rootless
  podman from its user manager before the API: if the API is the first to
  run podman after boot, every app container inherits its read-only
  `/proc/sys` and fails to start (see the script for the details; the
  proper fix belongs in Dock360).
* `openhamshack-growroot` grows the root partition to the disk.
* Hostname `openhamshack`, avahi for `openhamshack.local`, the edition's
  front door on port 80 (Caddy), administration on 8800, wired DHCP. No
  password anywhere; SSH is off unless the image was built with
  `SSH_KEYS=<authorized_keys>` (then a `shack` login with sudo).

## Outputs

In `--out`:

| File | What |
|---|---|
| `openhamshack-<version>-amd64.iso` | the installer |
| `openhamshack-<version>-amd64.iso.sha256` | its SHA-256, in `sha256sum -c` form |
| `openhamshack-<version>-amd64.packages.txt` | every Debian package in the controller with version and source package, for source offers (snapshot.debian.org) |
| `openhamshack-<version>-amd64.BUILDINFO` | what went in: this repo's commit, the Dock360 commit, each app with its source commit, first-boot apps |

Check a download with `sha256sum -c openhamshack-<version>-amd64.iso.sha256`.

## Verify

Boot records (both firmwares present):

```
xorriso -indev openhamshack-*.iso -report_el_torito plain
xorriso -indev openhamshack-*.iso -report_system_area plain
```

Expect two El Torito entries (platform `BIOS` with the eltorito image, and
`UEFI` with the appended partition), an MBR with GRUB boot code, a bootable
MBR partition, and a GPT with an EFI system partition.

Unit tests (no ISO needed):

```
python3 -m unittest installer/test_installer.py installer/test_grubmenu.py
```

Boot test, headless under QEMU+KVM (runs as your user; needs `/dev/kvm`):

```
tests/boot-test.sh ~/build/iso/out/openhamshack-*.iso            # uefi-cd bios-cd uefi-usb bios-usb
VENTOY_DIR=~/build/iso/ventoy/ventoy-1.1.17 tests/boot-test.sh <iso> ventoy-uefi ventoy-bios
# an ISO whose --first-apps include the app: wait for first boot's install,
# and cut the power during it once to prove the next boot finishes it
APP=shack-qo100wb FIRST_BOOT=1 tests/boot-test.sh <iso> uefi-usb bios-cd uefi-usb+powercut
# the beta gate 4: all eight in the store, a second app installed offline,
# and a Remote-style WebSocket through the front door (101, never 403)
APP=shack-qo100wb FIRST_BOOT=1 STORE_LISTS="<the eight ids>" THEN_INSTALL=shack-radio \
  WEBSOCKET="shack-radio /api/events" tests/boot-test.sh <iso> uefi-cd bios-cd
```

The WebSocket check exists because platform 2.0.0-beta.4 passed every
other check while refusing every Remote stream with 403 (Dock360 a76b02d):
run against that ISO it fails, which is how it was proved.

Gate 3, no secret anywhere in the ISO (every layer unpacked, a raw scan of
the controller disk, a positive control; about 25 GB of work space and
sudo for the loop mount):

```
ssh shack cat ~/.config/openhamshack/20i/id_ed25519.pub > ~/build/20i-upload.pub
tools/gate3-scan.sh <iso> --needle ~/build/20i-upload.pub    # evidence/ beside the ISO
python3 -m unittest tools/test_secretscan.py
```

It exits 0 PASS, 1 FAIL (a key this host holds was found, or the positive
control was missed) or 3 REVIEW (a strong hit outside stock Debian files and
the Dock360 source): read `gate3-summary.txt` and write the verdict down.

Each mode installs unattended to a fresh 32 GB disk (the ISO's GRUB picks
its unattended entry on a machine whose SMBIOS product is
`openhamshack-ci`), then boots the disk with `-nic user,restrict=on` (no
route out) and runs `tests/probe.py`: discovery on ports 80 and 8800,
first-run setup, login page, the store listing the preloaded app, and
installing it offline from its carried image. Finally the controller ids
of all installs are compared; each must be unique.

Ventoy for the test: version 1.1.17 (24/07/2026), the Linux tarball
`ventoy-1.1.17-linux.tar.gz`, SHA-256
`7fb4ed08cef6a6b4d39dd19260d8c80291a78dfdf9af7d461571e23cbbc43805`, as
listed in the release's `sha256.txt` and GitHub's asset digest. 1.1.18 was
hours old on 09/10/2026 and was not used.

### Traps

* On ohs-dev `/dev/kvm` came up owned by gid 993 (`clock`) because the `kvm`
  group was renumbered after udevd started. `sudo systemctl restart
  systemd-udevd && sudo udevadm trigger --action=change --sysname-match=kvm`
  puts it back to `kvm`.
* `pgrep -f <script>` in a wait loop matches the loop itself; wait on a PID.
* Caddy's cloudsmith apt repository answered `402 Payment Required` on
  09/10/2026, which fails Dock360's `install.sh` on any fresh host. The
  image installs Caddy from trixie-backports first, so install.sh skips it.

## Last verified build

09/10/2026 on ohs-dev, from openhamshack-os `19e9c7d` and Dock360
`hub/feature/ohs-edition` at `48cb7169`, with `openhamshack-fakebox` 0.1.0
(`c6e59ab`) as the stand-in store app:

* `openhamshack-2.0.0-beta.1-amd64.iso`, 1 353 381 888 bytes, SHA-256
  `342643d152af93d8be912029d52351671dee5fa0fed1f34d50cc3da684424e89`
  (not published; staged at `~/build/iso/out2/` on ohs-dev).
* `tests/boot-test.sh`, all six modes: uefi-cd, bios-cd, uefi-usb,
  bios-usb, ventoy-uefi, ventoy-bios each installed unattended (about two
  minutes) and passed all 13 probe checks with networking restricted; six
  different controller ids.
* Separately: the touch installer's page comes up from the default entry
  and offers only the virtual disk; with `--first-apps openhamshack-fakebox` the
  first start installs the app with no network, and it comes back by itself
  after a power cycle without first boot running again.

When a run fails before the kernel speaks on the serial port (firmware,
Ventoy's menu, GRUB), look at `install-screen.png` in the mode's work
directory: the test saves the screen every minute.
