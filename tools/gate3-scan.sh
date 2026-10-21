#!/bin/bash
# Gate 3: no secret in the ISO. Unpacks every layer of an OpenHamShack ISO
# and scans it all with tools/secretscan.py, then writes the evidence.
#
#   tools/gate3-scan.sh <iso> [--out DIR] [--work DIR] [--needle FILE]...
#
# Layers unpacked (what a user who downloads the ISO can reach):
#   iso/       the ISO9660 tree (GRUB, the manifest, the El Torito BIOS image)
#   efiimg/    the UEFI El Torito FAT image (GPT partition 2 of the ISO)
#   live/      /live/filesystem.squashfs, the installer's root; its regular
#              file count is checked against unsquashfs's own listing
#   initrd/    the live initrd and the installed system's initrd
#   disk.img   /openhamshack/openhamshack.img.zst decompressed, checked
#              against the manifest's raw_sha256, then scanned byte for byte
#              (free space included) and its ESP and root mounted read-only
#   apps/      every preloaded .d360mod: the package, and each container
#              layer extracted to its own directory, so a file a later layer
#              deleted or overwrote is scanned too
# The mounted root is scanned as root (shadow, sudoers, /root and
# /var/lib/dock360 are root-only); everything else as this user.
#
# Needles default to the keys this build host holds: the OpenHamShack master
# and modules signing keys and ~/.ssh/id_ed25519. Add the 20i upload key's
# public half (it lives on shack: `ssh shack cat
# ~/.config/openhamshack/20i/id_ed25519.pub > <file>`) with --needle.
# A positive control - a throwaway OpenSSH key and a throwaway Dock360-format
# key, planted raw, as hex and as base64 - is scanned with them; if any form
# is not found the scan itself is broken and the gate fails.
#
# Writes to --out (default: evidence/ beside the ISO): gate3-summary.txt
# (verdict and the hits sorted for a person), gate3-scan-hits.tsv (kind, path,
# offset - never a value) and gate3-raw-hits.tsv. Exit 0 PASS, 3 REVIEW (strong
# hits outside stock Debian files and the Dock360 source: read the summary),
# 1 FAIL. Work goes in --work (default ~/build/gate3-<pid>), about 25 GB,
# removed at the end; never /tmp, which is RAM on Debian 13.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ISO="$(readlink -f "${1:?usage: $0 <iso> [--out DIR] [--work DIR] [--needle FILE]...}")"; shift
OUT="$(dirname "$ISO")/evidence"; WORK="$HOME/build/gate3-$$"; NEEDLES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    --needle) NEEDLES+=("$(readlink -f "$2")"); shift 2 ;;
    *) sed -n '2,5p' "$0" >&2; exit 2 ;;
  esac
done
for k in "$HOME/.config/openhamshack/keys/ohs-master.key" "$HOME/.config/openhamshack/keys/ohs-modules.key" \
         "$HOME/.ssh/id_ed25519"; do
  [[ -r "$k" ]] && NEEDLES+=("$k")
done
[[ ${#NEEDLES[@]} -gt 0 ]] || { echo "no needle keys readable" >&2; exit 2; }
BUILDINFO="${ISO%.iso}.BUILDINFO"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
[[ -e "$WORK" ]] && { echo "$WORK exists; give an empty --work" >&2; exit 2; }
mkdir -p "$WORK"; W="$(cd "$WORK" && pwd)"
LOOP=""
cleanup() {
  sudo umount "$W/sys" "$W/esp" 2>/dev/null || true
  [[ -n "$LOOP" ]] && sudo losetup -d "$LOOP" 2>/dev/null || true
  sudo rm -rf "$W"
}
trap cleanup EXIT
say() { echo "== $*"; }
me="$(id -u):$(id -g)"

say "ISO tree"
xorriso -osirrox on -indev "$ISO" -extract / "$W/iso" >/dev/null 2>&1
chmod -R u+rwX "$W/iso"

say "UEFI El Torito image (GPT partition 2)"
python3 -I - "$ISO" "$W/efi.img" <<'PY'
import struct, sys
iso, out = sys.argv[1], sys.argv[2]
with open(iso, "rb") as f:
    f.seek(512); hdr = f.read(92)
    assert hdr[:8] == b"EFI PART", "no GPT in the ISO"
    lba, count, size = struct.unpack_from("<QII", hdr, 72)
    f.seek(lba * 512); entries = f.read(count * size)
    for i in range(count):
        e = entries[i * size:(i + 1) * size]
        first, last = struct.unpack_from("<QQ", e, 32)
        if e[:16] == bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b"):  # EFI system partition
            f.seek(first * 512); open(out, "wb").write(f.read((last - first + 1) * 512)); break
    else:
        raise SystemExit("no EFI system partition in the ISO's GPT")
PY
mkdir -p "$W/efiimg"; mcopy -s -n -i "$W/efi.img" ::/ "$W/efiimg/"

say "installer root (squashfs)"
# Device nodes cannot be made as a user; the count check below is what says
# every regular file came out.
unsquashfs -q -no-xattrs -d "$W/live" "$W/iso/live/filesystem.squashfs" >/dev/null 2>&1 || true
chmod -R u+rwX "$W/live" 2>/dev/null || true
listed=$(unsquashfs -lls "$W/iso/live/filesystem.squashfs" | grep -c '^-')
found=$(find "$W/live" -type f | wc -l)
echo "   $found regular files (squashfs lists $listed)"
[[ "$listed" == "$found" ]] || { echo "squashfs unpacked $found of $listed files" >&2; exit 1; }

say "controller disk image"
zstd -q -d "$W/iso/openhamshack/openhamshack.img.zst" -o "$W/disk.img"
want=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["raw_sha256"])' "$W/iso/openhamshack/manifest.json")
have=$(sha256sum "$W/disk.img" | cut -d' ' -f1)
[[ "$want" == "$have" ]] || { echo "disk image sha256 $have is not the manifest's $want" >&2; exit 1; }
echo "   sha256 matches the manifest"
LOOP=$(sudo losetup -r -P --show -f "$W/disk.img")
mkdir -p "$W/sys" "$W/esp"
for part in "$LOOP"p*; do
  case "$(sudo blkid -o value -s TYPE "$part" 2>/dev/null)" in
    vfat) sudo mount -o ro "$part" "$W/esp" ;;
    ext4) sudo mount -o ro,noload "$part" "$W/sys" ;;
  esac
done
mountpoint -q "$W/sys" || { echo "no root filesystem in the disk image" >&2; exit 1; }

say "initrds"
mkdir -p "$W/initrd"
unmkinitramfs "$W/iso/live/initrd.img" "$W/initrd/live" 2>/dev/null || true
for i in "$W"/sys/boot/initrd.img-*; do
  sudo unmkinitramfs "$i" "$W/initrd/sys-$(basename "$i")" 2>/dev/null || true
done
for d in "$W"/initrd/*; do
  [[ -n "$(sudo find "$d" -type f -print -quit)" ]] || { echo "initrd $d unpacked empty" >&2; exit 1; }
done
sudo chown -R "$me" "$W/initrd"; chmod -R u+rwX "$W/initrd"

say "preloaded apps and every container layer"
mkdir -p "$W/apps"
for pkg in "$W"/sys/var/lib/openhamshack/preload/*.d360mod; do
  [[ -e "$pkg" ]] || continue
  name="$(basename "$pkg" .d360mod)"; d="$W/apps/$name"; mkdir -p "$d/pkg" "$d/image"
  sudo cat "$pkg" | tar -xz -C "$d/pkg"
  img="$(find "$d/pkg" -name image.tar.gz | head -1)"
  [[ -n "$img" ]] || { echo "   $name: no carried image"; continue; }
  tar -xzf "$img" -C "$d/image"
  # docker-archive (podman save's default) lists its layers in manifest.json;
  # OCI layouts keep them as blobs. Either way each is a tar, maybe gzipped.
  python3 -I - "$d/image" <<'PY' > "$W/layers-$name.txt"
import json, os, sys
root = sys.argv[1]
if os.path.exists(os.path.join(root, "manifest.json")):
    for layer in json.load(open(os.path.join(root, "manifest.json")))[0]["Layers"]:
        print(os.path.join(root, layer))
else:
    idx = json.load(open(os.path.join(root, "index.json")))
    def blob(d): return os.path.join(root, "blobs", *d.split(":"))
    man = json.load(open(blob(idx["manifests"][0]["digest"])))
    for layer in man["layers"]:
        print(blob(layer["digest"]))
PY
  n=0
  while read -r layer; do
    n=$((n + 1)); mkdir -p "$d/layers/$n"
    tar -x --no-same-owner --no-same-permissions --exclude='dev/*' -f "$layer" -C "$d/layers/$n" 2>/dev/null || true
    rm -f "$layer"
  done < "$W/layers-$name.txt"
  rm -f "$img"
  chmod -R u+rwX "$d" 2>/dev/null || true
  echo "   $name: $n layers"
done

say "positive control"
C="$W/control"; mkdir -p "$C/keys" "$C/tree"
ssh-keygen -q -t ed25519 -N "" -C "gate3 control" -f "$C/keys/control-ssh" >/dev/null
python3 -I - "$C/keys" "$C/tree" <<'PY'
import base64, os, re, sys
keys, tree = sys.argv[1], sys.argv[2]
seed = os.urandom(32)
# Dock360's private key file: comment lines, then the format label and the key.
open(f"{keys}/control-dock360.key", "w").write(
    "# Dock360 signing key (gate 3 positive control)\n# throwaway\n"
    f"dock360-ed25519-private {base64.b64encode(seed).decode()}\n")
raw = base64.b64decode(b"".join(l for l in open(f"{keys}/control-ssh", "rb").read().splitlines()
                                if l and not l.startswith(b"-----")))
sshseed = re.search(rb"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20.{32}\x00\x00\x00\x40(.{32})", raw, re.S).group(1)
pad = lambda b: os.urandom(777) + b + os.urandom(333)
for name, data in {"ssh-keyfile": open(f"{keys}/control-ssh", "rb").read(), "ssh-raw.bin": pad(sshseed),
                   "ssh-hex.txt": b"x=" + sshseed.hex().encode(),
                   "dock360-keyfile": open(f"{keys}/control-dock360.key", "rb").read(),
                   "dock360-raw.bin": pad(seed), "dock360-hex.txt": b"k " + seed.hex().encode() + b"\n",
                   "dock360-b64.txt": b'"' + base64.b64encode(seed) + b'"'}.items():
    open(f"{tree}/{name}", "wb").write(data)
PY

say "file scan"
roots=("$W/iso" "$W/efiimg" "$W/live" "$W/initrd" "$W/apps" "$W/sys" "$W/esp" "$C/tree")
sudo python3 -I "$HERE/secretscan.py" files "$W/files.tsv" "${NEEDLES[@]}" "$C/keys/control-ssh" \
  "$C/keys/control-dock360.key" -- "${roots[@]}"
sudo chown "$me" "$W/files.tsv" "$W/files.tsv.count"
missed=()
for f in "$C"/tree/*; do
  grep -qP "^KNOWN-SECRET:control-[^\t]*\t\Q$f\E\t" "$W/files.tsv" || missed+=("$(basename "$f")")
done
if [[ ${#missed[@]} -gt 0 ]]; then
  echo "positive control: NOT FOUND ${missed[*]} - the scanner is broken" >&2; exit 1
fi
echo "   positive control: all $(ls "$C/tree" | wc -l) planted forms found"

say "raw disk scan"
python3 -I "$HERE/secretscan.py" raw "$W/raw.tsv" "$W/disk.img" "${NEEDLES[@]}"

say "classify"
srcargs=()
if [[ -r "$BUILDINFO" ]]; then
  commit=$(awk '$1=="dock360"{print $3}' "$BUILDINFO")
  if [[ -n "$commit" ]] && git -C "$HOME/dock360" cat-file -e "$commit^{commit}" 2>/dev/null; then
    mkdir -p "$W/dock360-src"; git -C "$HOME/dock360" archive "$commit" | tar -x -C "$W/dock360-src"
    srcargs=(--dock360-src "$W/dock360-src")
  fi
fi
set +e
sudo python3 -I "$HERE/secretscan.py" classify "$W/files.tsv" "$W/raw.tsv" "$W/summary.txt" \
  --root "$W/sys" --root "$W/live" --apps "$W/apps" --ignore-under "$C/" "${srcargs[@]}"
rc=$?
set -e
sudo chown "$me" "$W/summary.txt"
{
  echo "Gate 3 secret scan - $(basename "$ISO")"
  echo "sha256 $(cut -d' ' -f1 < "${ISO}.sha256" 2>/dev/null || sha256sum "$ISO" | cut -d' ' -f1)"
  echo "scanned $(date -u +%FT%TZ) on $(hostname) by tools/gate3-scan.sh; hits are kind, path, offset only"
  echo "files scanned: $(cat "$W/files.tsv.count"); installer root $found regular files (squashfs lists $listed)"
  echo "needles: ${NEEDLES[*]##*/}; positive control found in every planted form"
  echo
  sed "s#$W/##g" "$W/summary.txt"
} > "$OUT/gate3-summary.txt"
awk -F'\t' -v c="$C/" -v w="$W/" 'index($2, c) != 1 { sub("^" w, "", $2); print $1 "\t" $2 "\t" $3 }' \
  "$W/files.tsv" > "$OUT/gate3-scan-hits.tsv"
cp "$W/raw.tsv" "$OUT/gate3-raw-hits.tsv"
say "$(head -1 "$W/summary.txt") - $OUT/gate3-summary.txt"
exit "$rc"
