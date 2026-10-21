#!/bin/bash
# Package one store app with its container image, signed, for the ISO's
# preloaded store (build.sh --apps DIR).
#
#   tools/package-app.sh <git repo> <module dir in repo> <out dir> --key <modules key> \
#       [--ref <branch|commit>] [--dock360 <dock360 source dir>]
#
# Runs on the build machine as an ordinary user: rootless podman builds the
# image, and `dock360ctl module package --with-image --key` writes and signs
# the .d360mod. The private key is read here and nowhere else; it never goes
# into the image, the ISO or this repo. The result is the same file an app's
# own release would publish to the store feed, so the ISO preloads exactly
# what a station would download.
#
# The source is a fresh shallow clone of <ref>, never the working tree: a
# package built from a tree with uncommitted edits records a commit it does
# not contain, and dock360ctl refuses one anyway.
set -euo pipefail

usage() { echo "usage: $0 <repo> <module-dir> <out-dir> --key <key> [--ref REF] [--dock360 DIR]" >&2; exit 2; }
[[ $# -ge 3 ]] || usage
REPO="$1"; MODDIR="$2"; OUT="$3"; shift 3
KEY=""; REF="HEAD"
DOCK360_SRC="${DOCK360_SRC:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --key) KEY="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --dock360) DOCK360_SRC="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$KEY" && -r "$KEY" ]] || { echo "need a readable --key (the OpenHamShack modules key)" >&2; exit 2; }
[[ -n "$DOCK360_SRC" && -d "$DOCK360_SRC/src/dock360" ]] || { echo "need --dock360 <dir> (a git archive of the edition)" >&2; exit 2; }
PY="${DOCK360_PY:-python3}"
dock360ctl() { PYTHONPATH="$DOCK360_SRC/src" "$PY" -m dock360.cli "$@"; }

WORK="$(mktemp -d "${BUILD_DIR:-$HOME/build}/package-app.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
git clone -q --no-local "$REPO" "$WORK/src"
git -C "$WORK/src" checkout -q "$REF"
commit="$(git -C "$WORK/src" rev-parse --short HEAD)"
src="$WORK/src/$MODDIR"
id="$(sed -n 's/^id: *//p' "$src/module.yaml" | head -1)"
version="$(sed -n 's/^version: *//p' "$src/module.yaml" | head -1)"
# The tag Dock360 runs a locally built module under (modules._local_image_tag);
# a carried image must arrive as exactly this or the install refuses it.
tag="localhost/dock360-${id}:${version}"
context="$(sed -n '/^  build:/,/^  [a-z]/{s/^    context: *//p}' "$src/module.yaml" | head -1)"
containerfile="$(sed -n '/^  build:/,/^  [a-z]/{s/^    containerfile: *//p}' "$src/module.yaml" | head -1)"
echo "== $id $version from $REPO@$commit -> $tag"
podman build -q -t "$tag" -f "$src/${context:-.}/${containerfile:-Containerfile}" "$src/${context:-.}"
mkdir -p "$OUT"
dock360ctl module package "$src" --with-image --key "$KEY" -o "$OUT/${id}-${version}.d360mod"
sha256sum "$OUT/${id}-${version}.d360mod" | sed "s#  .*/#  #" > "$OUT/${id}-${version}.d360mod.sha256"
echo "$id $version $commit" >> "$OUT/SOURCES"
