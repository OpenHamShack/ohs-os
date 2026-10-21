#!/bin/bash
# Bootable installer ISO for the OpenHamShack controller, for any x64 PC:
# UEFI or legacy BIOS, as a DVD, as a USB stick written raw (Rufus in DD
# mode, Etcher, dd), or copied onto a Ventoy stick. Secure Boot off.
#
#   sudo DOCK360_SRC=<dir> [APPS_DIR=<dir>] image/mkiso.sh  -> openhamshack-<version>-amd64.iso
#
# build.sh is the usual way in; it prepares DOCK360_SRC from git.
#
# The ISO is a Debian live system (squashfs + live-boot, the shape Ventoy
# handles best) that boots into the touch installer, or with
# shack.install=auto into the unattended one. The controller's disk image
# rides beside the squashfs in /openhamshack, where live-boot mounts the
# medium, rather than inside it.
#
# Boot, four ways into one grub.cfg:
#   BIOS, CD/DVD    El Torito entry 1: GRUB's i386-pc-eltorito core image
#   BIOS, USB/disk  the MBR is GRUB's boot_hybrid.img, which xorriso points
#                   (--grub2-boot-info) at that same core image
#   UEFI, CD/DVD    El Torito entry 2: a FAT image holding BOOTX64.EFI
#   UEFI, USB/disk  the same FAT image again as GPT partition 2 (an ESP)
# Both GRUBs find the medium by a file only it has and read the one
# /boot/grub/grub.cfg (installer/grubmenu.py). The layout is the one Ubuntu
# and grub-mkrescue use for GRUB hybrids; isolinux is not involved.
#
# Started from the archived Classic appliance's mkiso.sh at 50742ac, which was UEFI only
# (no BIOS El Torito entry, no MBR boot code) and had a Try entry.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOP="$(cd "$HERE/.." && pwd)"
DOCK360_SRC="${DOCK360_SRC:?DOCK360_SRC must name a git archive of the Dock360 edition}"
VERSION="${VERSION:-$(tr -d ' \n' < "$DOCK360_SRC/src/dock360/editions/openhamshack/PLATFORM_VERSION")}"
WORK="${WORK:-/var/tmp/openhamshack-iso}"
OUT="${OUT:-$PWD/openhamshack-$VERSION-amd64.iso}"
VOLID="${VOLID:-OPENHAMSHACK}"
# The file both GRUBs search for. Specific to this medium, so a PC with some
# other live system's disk in it does not boot that one's kernel instead.
MARKER=/openhamshack/manifest.json

if [[ "$(id -u)" -ne 0 ]]; then echo "needs root" >&2; exit 1; fi
for tool in xorriso mksquashfs mformat mcopy mmd grub-mkstandalone grub-mkimage zstd; do
  command -v "$tool" >/dev/null || { echo "missing $tool (apt-get install xorriso squashfs-tools mtools grub-efi-amd64-bin grub-pc-bin zstd)" >&2; exit 1; }
done
for f in /usr/lib/grub/i386-pc/boot_hybrid.img /usr/lib/grub/i386-pc/cdboot.img /usr/lib/grub/x86_64-efi/normal.mod; do
  [[ -e "$f" ]] || { echo "missing $f (apt-get install grub-pc-bin grub-efi-amd64-bin)" >&2; exit 1; }
done

rm -rf "$WORK"
mkdir -p "$WORK/iso/live" "$WORK/iso/openhamshack" "$WORK/iso/boot/grub/i386-pc" "$WORK/iso/EFI/BOOT"

echo "== 1/6 controller disk image"
env -u KEEP_STAGE ROLE=system WORK_DIR="$WORK" IMG="$WORK/openhamshack.img" bash "$HERE/mkimage.sh"
raw_bytes=$(stat -c %s "$WORK/openhamshack.img")
raw_sha=$(sha256sum "$WORK/openhamshack.img" | cut -d' ' -f1)
zstd -q -T0 -10 -f "$WORK/openhamshack.img" -o "$WORK/iso/openhamshack/openhamshack.img.zst"
rm -f "$WORK/openhamshack.img"
zst_sha=$(sha256sum "$WORK/iso/openhamshack/openhamshack.img.zst" | cut -d' ' -f1)
cat > "$WORK/iso/openhamshack/manifest.json" <<EOF
{"label": "OpenHamShack $VERSION", "file": "openhamshack.img.zst", "raw_bytes": $raw_bytes,
 "raw_sha256": "$raw_sha", "zst_sha256": "$zst_sha"}
EOF

echo "== 2/6 installer root filesystem"
env -u DOCK360_SRC -u APPS_DIR ROLE=installer WORK_DIR="$WORK" KEEP_STAGE="$WORK/root" \
    IMG="$WORK/installer.img" INSTALLER_PAYLOAD_LINK=/run/live/medium/openhamshack bash "$HERE/mkimage.sh"

echo "== 3/6 squashfs"
cp "$(ls "$WORK"/root/boot/vmlinuz-* | sort -V | tail -1)" "$WORK/iso/live/vmlinuz"
cp "$(ls "$WORK"/root/boot/initrd.img-* | sort -V | tail -1)" "$WORK/iso/live/initrd.img"
mksquashfs "$WORK/root" "$WORK/iso/live/filesystem.squashfs" -comp xz -noappend -quiet -e boot
rm -rf "$WORK/root"

echo "== 4/6 boot menu and BIOS GRUB"
python3 "$TOP/installer/grubmenu.py" "$VERSION" > "$WORK/iso/boot/grub/grub.cfg"
# BIOS: a core image small enough for El Torito's no-emulation load, holding
# only what it takes to find the medium; every other module is loaded from
# the ISO's /boot/grub/i386-pc once prefix points there. biosdisk sees the
# CD and, booted from the hybrid MBR, the stick as a hard disk; part_* and
# iso9660 let search look inside either.
# `normal` again rather than `configfile`: normal mode has already started
# by the time this runs, with no usable prefix, so it never read
# command.lst and `linux` was an unknown command (the first BIOS boot test:
# "can't find command 'linux'"). Entering normal anew with the prefix set
# reads the command list from the ISO, then $prefix/grub.cfg.
cat > "$WORK/bios-embedded.cfg" <<EOF
search --no-floppy --set=root --file $MARKER
set prefix=(\$root)/boot/grub
insmod normal
normal
EOF
grub-mkimage -O i386-pc-eltorito -o "$WORK/iso/boot/grub/i386-pc/eltorito.img" \
    -p /boot/grub -c "$WORK/bios-embedded.cfg" \
    biosdisk iso9660 part_msdos part_gpt fat search search_fs_file configfile normal echo test
cp /usr/lib/grub/i386-pc/*.mod /usr/lib/grub/i386-pc/*.lst "$WORK/iso/boot/grub/i386-pc/"

echo "== 5/6 UEFI GRUB"
# A standalone GRUB keeps its modules in its own memdisk, so prefix must stay
# there - pointing it at the ISO's /boot/grub (which has no x86_64-efi
# modules) left every later command, configfile included, unloadable on the
# appliance. The insmods make search able to read the ISO and its GPT.
cat > "$WORK/efi-embedded.cfg" <<EOF
insmod part_gpt
insmod part_msdos
insmod iso9660
insmod fat
search --no-floppy --set=root --file $MARKER
configfile (\$root)/boot/grub/grub.cfg
EOF
grub-mkstandalone -O x86_64-efi -o "$WORK/iso/EFI/BOOT/BOOTX64.EFI" \
    --locales="" --fonts="" "boot/grub/grub.cfg=$WORK/efi-embedded.cfg"
# Sized from the binary: a standalone GRUB with every module is ~6 MB, far
# past a 2.88 MB floppy image (the appliance's first build: "Disk full").
efi_kb=$(( $(stat -c %s "$WORK/iso/EFI/BOOT/BOOTX64.EFI") / 1024 + 1024 ))
rm -f "$WORK/efiboot.img"
truncate -s "${efi_kb}K" "$WORK/efiboot.img"
mkfs.vfat -n SHACKEFI "$WORK/efiboot.img" >/dev/null
mmd -i "$WORK/efiboot.img" ::/EFI ::/EFI/BOOT
mcopy -i "$WORK/efiboot.img" "$WORK/iso/EFI/BOOT/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI

echo "== 6/6 ISO"
# -partition_offset 16 makes the ISO 9660 tree a partition of its own in the
# MBR/GPT xorriso writes, starting past the tables, so firmware and GRUB on a
# raw-written stick see a sane partition rather than a filesystem at sector
# 0. --mbr-force-bootable sets the active flag some BIOSes insist on before
# they will run MBR code. -iso_mbr_part_type is GPT "basic data" for that
# partition (Ubuntu's choice; 0x83 confused some Windows tools).
rm -f "$OUT"
xorriso -as mkisofs -iso-level 3 -full-iso9660-filenames -joliet -joliet-long -rational-rock \
    -volid "$VOLID" \
    --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
    -partition_offset 16 --mbr-force-bootable \
    -append_partition 2 0xef "$WORK/efiboot.img" -appended_part_as_gpt \
    -iso_mbr_part_type a2a0d0eb-e5b9-3344-87c0-68b6b72699c7 \
    -c /boot.catalog \
    -b /boot/grub/i386-pc/eltorito.img -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info \
    -eltorito-alt-boot -e --interval:appended_partition_2::: -no-emul-boot \
    -output "$OUT" "$WORK/iso" 2>&1 | grep -v -E "^xorriso : UPDATE|^Drive current" || true
[[ -s "$OUT" ]] || { echo "xorriso wrote nothing" >&2; exit 1; }
( cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" ) | tee "$OUT.sha256"
# The image's Debian package list, published beside the ISO.
cp "$WORK/openhamshack.packages.txt" "${OUT%.iso}.packages.txt"
rm -rf "$WORK"
ls -lh "$OUT"
