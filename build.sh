#!/bin/bash
# Build the OpenHamShack controller ISO.
#
#   sudo ./build.sh --dock360 ~/dock360 [--ref feature/ohs-edition] [--apps DIR]
#                   [--first-apps "shack-qo100wb"] [--out DIR] [--work DIR]
#                   [--python /path/to/venv/bin/python] [--feeds-base URL]
#
#   --dock360     the Dock360 git repository; the edition is taken from --ref
#                 with `git archive`, so uncommitted edits in a working tree
#                 can never reach an ISO, and the commit is recorded
#   --apps        signed .d360mod packages, each carrying its image
#                 (tools/package-app.sh makes them), preloaded into the store;
#                 checked at build time by tools/verify-apps.py
#   --first-apps  store apps installed at the controller's first start, from
#                 those packages (default: shack-qo100wb, the QO-100 WB app)
#   --feeds-base  where the controller looks for updates: first boot presets
#                 Station updates to <URL>platform-{channel}.json,
#                 <URL>catalogue.json and channel beta, never over a value an
#                 administrator has set (default https://openhamshack.com/feeds/;
#                 "" leaves them empty). docs/UPDATES.md
#   --python      a Python with Dock360's requirements, for the package check
#                 (system python3 on Debian lacks PyYAML)
#
# Writes openhamshack-<version>-amd64.iso, its .sha256, the Debian package
# list and BUILDINFO into --out. docs/BUILD.md has the details.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOCK360_REPO=""; REF="feature/ohs-edition"; APPS=""; FIRST_APPS="shack-qo100wb"
OUT_DIR="$PWD"; WORK="/var/tmp/openhamshack-iso"; PY="python3"
FEEDS_BASE="https://openhamshack.com/feeds/"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dock360) DOCK360_REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --apps) APPS="$2"; shift 2 ;;
    --first-apps) FIRST_APPS="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --feeds-base) FEEDS_BASE="$2"; shift 2 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ "$(id -u)" -eq 0 ]] || { echo "needs root (debootstrap, loop devices, chroot)" >&2; exit 1; }
[[ -n "$DOCK360_REPO" ]] || { echo "--dock360 <repo> is required" >&2; exit 2; }
# Checked here rather than at first boot, where a typo would only show as a
# feed that never loads. Written into a shell-sourced file, so no quotes,
# spaces or line breaks.
if [[ -n "$FEEDS_BASE" ]]; then
  [[ "$FEEDS_BASE" =~ ^https?://[A-Za-z0-9.:/_~%-]+$ ]] || { echo "--feeds-base must be an http(s) URL" >&2; exit 2; }
  [[ "$FEEDS_BASE" == */ ]] || FEEDS_BASE="$FEEDS_BASE/"
fi

mkdir -p "$WORK" "$OUT_DIR"
# Beside the work directory, by an absolute path: mkiso.sh removes $WORK when
# it finishes, and "$WORK/../x" stops resolving once $WORK is gone, which
# left every build's copy of the Dock360 tree behind.
SRC="$(cd "$WORK/.." && pwd)/dock360-src.$$"
rm -rf "$SRC"; mkdir -p "$SRC"
trap 'rm -rf "$SRC"' EXIT
# safe.directory: the repository belongs to the person building, not root.
commit="$(git -c safe.directory='*' -C "$DOCK360_REPO" rev-parse --verify "$REF^{commit}")"
git -c safe.directory='*' -C "$DOCK360_REPO" archive "$commit" | tar -x -C "$SRC"
version="$(tr -d ' \n' < "$SRC/src/dock360/editions/openhamshack/PLATFORM_VERSION")"
echo "== Dock360 $REF at $commit (OpenHamShack $version)"

if [[ -n "$APPS" ]]; then
  echo "== checking the store's packages"
  "$PY" -I "$HERE/tools/verify-apps.py" "$SRC" "$APPS"
fi

OUT="$OUT_DIR/openhamshack-$version-amd64.iso"
self="$(git -c safe.directory='*' -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
dirty="$(git -c safe.directory='*' -C "$HERE" status --porcelain 2>/dev/null | head -c1)"
env DOCK360_SRC="$SRC" APPS_DIR="$APPS" FIRST_APPS="$FIRST_APPS" FEEDS_BASE="$FEEDS_BASE" VERSION="$version" \
    WORK="$WORK" OUT="$OUT" bash "$HERE/image/mkiso.sh"

{
  echo "openhamshack $version"
  echo "built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "openhamshack-os $self${dirty:+ (uncommitted changes)}"
  echo "dock360 $REF $commit"
  [[ -n "$APPS" && -f "$APPS/SOURCES" ]] && sed 's/^/app /' "$APPS/SOURCES"
  echo "first-apps $FIRST_APPS"
  echo "feeds-base ${FEEDS_BASE:-(none)}"
} > "${OUT%.iso}.BUILDINFO"
cat "${OUT%.iso}.BUILDINFO"
