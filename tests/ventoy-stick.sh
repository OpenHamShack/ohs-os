#!/bin/bash
# Make a Ventoy USB stick image with the ISO copied onto it, for
# tests/boot-test.sh's ventoy-uefi and ventoy-bios modes.
#
#   VENTOY_DIR=~/build/iso/ventoy/ventoy-1.1.17 tests/ventoy-stick.sh <iso> <stick.img>
#
# VENTOY_DIR is an unpacked Ventoy Linux release, checked by hand against
# the release's sha256.txt before use (docs/BUILD.md records the version and
# digest). Ventoy2Disk.sh is run on a loop device over a sparse file exactly
# as a user runs it on a stick: MBR layout (Ventoy's default, which boots
# both firmwares), exFAT data partition, then the ISO copied on as a plain
# file. Needs sudo for the loop device and the mount.
set -euo pipefail

ISO="$(readlink -f "${1:?usage: $0 <iso> <stick.img>}")"
IMG="${2:?usage: $0 <iso> <stick.img>}"
VENTOY_DIR="${VENTOY_DIR:-}"
[[ -x "$VENTOY_DIR/Ventoy2Disk.sh" ]] || { echo "set VENTOY_DIR to an unpacked Ventoy Linux release" >&2; exit 1; }

size_mb=$(( $(stat -c %s "$ISO") / 1048576 + 1024 ))
rm -f "$IMG"
truncate -s "${size_mb}M" "$IMG"
loop="$(sudo losetup --find --show "$IMG")"
mnt="$(mktemp -d "$(dirname "$IMG")/mnt.XXXXXX")"
cleanup() {
  sudo umount "$mnt" 2>/dev/null || true
  sudo losetup -d "$loop" 2>/dev/null || true
  rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
# -I: install even if Ventoy thinks it is already there. It asks twice
# whether to erase the device; the device is our own loop file.
( cd "$VENTOY_DIR" && printf 'y\ny\n' | sudo ./Ventoy2Disk.sh -I "$loop" )
sudo partprobe "$loop" 2>/dev/null || sudo losetup -d "$loop"
if [[ ! -b "${loop}p1" ]]; then
  sudo losetup -d "$loop" 2>/dev/null || true
  loop="$(sudo losetup --find --show --partscan "$IMG")"
fi
sudo mount -t exfat "${loop}p1" "$mnt"
sudo cp "$ISO" "$mnt/"
# Test-only Ventoy settings, so nobody has to press keys: boot this ISO by
# default after a second, and skip the secondary "normal / grub2 mode"
# menu (normal mode is what users are told to pick, docs/INSTALL.md). A
# user's stick has none of this and they choose at Ventoy's menu.
sudo mkdir -p "$mnt/ventoy"
printf '{"control": [{"VTOY_DEFAULT_IMAGE": "/%s"}, {"VTOY_MENU_TIMEOUT": "1"}, {"VTOY_SECONDARY_BOOT_MENU": "0"}]}\n' \
  "$(basename "$ISO")" | sudo tee "$mnt/ventoy/ventoy.json" >/dev/null
sync
ls -l "$mnt"
sudo umount "$mnt"
sudo losetup -d "$loop"
sudo chown "$(id -u):$(id -g)" "$IMG"
echo "ventoy stick: $IMG"
