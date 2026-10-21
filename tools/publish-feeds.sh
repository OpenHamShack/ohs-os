#!/bin/bash
# Publish the OpenHamShack update feeds: a platform release built from a
# Dock360 ref, and the app store's signed packages, each listed in a signed
# feed (the format: docs/openhamshack-feeds.md in the Dock360 edition).
#
#   tools/publish-feeds.sh [--dock360 ~/dock360] [--ref hub/feature/ohs-edition]
#       [--apps ~/build/apps] [--out ~/feeds/www] [--channel beta]
#       [--platform-version V] [--notes TEXT] [--no-platform] [--no-catalogue]
#       [--allow-removal] [--keys ~/.config/openhamshack/keys]
#
# Output: <out>/feeds/ laid out exactly as openhamshack.com/feeds/ will be -
#   platform-<channel>.json(.sig)  catalogue.json(.sig)
#   packages/dock360-<engine>.d360upd   apps/<id>-<version>.d360mod
# so the LAN mirror on ohs-dev and the website are the same tree, copied
# (`rsync -a <out>/feeds/ <site>/feeds/`). This script never uploads.
#
# Keys (read here, on the machine that holds them, never copied):
#   ohs-master.key  signs the platform package and the platform feed
#   ohs-modules.key signs the catalogue feed (the app packages are already
#                   signed with it by tools/package-app.sh)
# A modules key cannot sign a platform feed a station will accept, so the
# key used day to day for apps can never move a station to new software.
#
# The platform version. A station is told the *platform* version (2.0.0-beta.N,
# the edition's PLATFORM_VERSION file), and the feed offers only versions above
# the station's own. No feature commit touches that file (~/.claude/CLAUDE.md,
# "Releases and VERSION": two sessions bumping it per feature set it over each
# other). Instead this script numbers each publish: the newest of the ref's
# PLATFORM_VERSION and every version already published (the ohs-platform/* tags),
# plus one - or --platform-version to choose. The engine VERSION is restamped
# too, because the updater refuses to install a release at the engine version
# already running (tools/feedgen.py engine-version says how it is chosen).
#
# Committed trees only. The two stamped files are committed on top of the ref
# in a throw-away detached worktree and tagged ohs-platform/<version> in the
# Dock360 repo (local; push the tag with the branch when you push), so every
# published package names a commit that holds exactly what shipped, and the
# next publish can compare against it.
#
# What a release removes. On 20 September a release cut from one branch
# silently dropped a fix that lived only on another. Before packaging, this
# compares the ref with the source of the previous publish (the tag's parent):
# commits in the previous release that the ref does not contain are listed,
# and the publish stops unless --allow-removal says that is intended.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOCK360="$HOME/dock360"; REF="hub/feature/ohs-edition"; APPS="$HOME/build/apps"
OUT="$HOME/feeds/www"; CHANNEL="beta"; PLATFORM_VERSION=""; NOTES=""
DO_PLATFORM=1; DO_CATALOGUE=1; ALLOW_REMOVAL=0
KEYS="$HOME/.config/openhamshack/keys"
PY="${DOCK360_PY:-$HOME/build/iso/venv/bin/python}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dock360) DOCK360="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --apps) APPS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --platform-version) PLATFORM_VERSION="$2"; shift 2 ;;
    --notes) NOTES="$2"; shift 2 ;;
    --no-platform) DO_PLATFORM=0; shift ;;
    --no-catalogue) DO_CATALOGUE=0; shift ;;
    --allow-removal) ALLOW_REMOVAL=1; shift ;;
    --keys) KEYS="$2"; shift 2 ;;
    *) sed -n '2,12p' "$0" >&2; exit 2 ;;
  esac
done
[[ "$CHANNEL" == beta || "$CHANNEL" == stable ]] || { echo "channel is beta or stable" >&2; exit 2; }
[[ -x "$PY" ]] || { echo "need a Python with Dock360's requirements (DOCK360_PY; see docs/BUILD.md)" >&2; exit 2; }
MASTER="$KEYS/ohs-master.key"; MODULES="$KEYS/ohs-modules.key"
(( DO_PLATFORM == 0 )) || [[ -r "$MASTER" ]] || { echo "cannot read $MASTER" >&2; exit 2; }
(( DO_CATALOGUE == 0 )) || [[ -r "$MODULES" ]] || { echo "cannot read $MODULES" >&2; exit 2; }

say() { echo "== $*"; }
mkdir -p "$OUT/feeds"
OUT="$(cd "$OUT" && pwd)"
commit="$(git -C "$DOCK360" rev-parse --verify "$REF^{commit}")"
short="$(git -C "$DOCK360" rev-parse --short "$commit")"

# The Dock360 tree the package is built from, and whose code and public keys
# check the result. A detached worktree of the commit: never the repo's own
# checkout, which another session may be editing.
WORK="$(mktemp -d "${BUILD_DIR:-$HOME/build}/publish-feeds.XXXXXX")"
cleanup() {
  git -C "$DOCK360" worktree remove --force "$WORK/dock360" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
git -C "$DOCK360" worktree add -q --detach "$WORK/dock360" "$commit"
SRC="$WORK/dock360"
feedgen() { PYTHONPATH="$SRC/src" "$PY" "$HERE/feedgen.py" "$@"; }
dock360ctl() { (cd "$WORK" && PYTHONPATH="$SRC/src" "$PY" -m dock360.cli "$@"); }

if (( DO_PLATFORM )); then
  edition_file=src/dock360/editions/openhamshack/PLATFORM_VERSION
  at_ref="$(cat "$SRC/$edition_file")"
  mapfile -t published < <(git -C "$DOCK360" tag -l 'ohs-platform/*' | sed 's#^ohs-platform/##')
  version="${PLATFORM_VERSION:-$(feedgen next-version "$at_ref" "${published[@]}")}"
  engine="$(feedgen engine-version "$(cat "$SRC/VERSION")")"
  say "platform $version (engine $engine) from $REF @ $short"
  git -C "$DOCK360" rev-parse -q --verify "refs/tags/ohs-platform/$version" >/dev/null \
    && { echo "ohs-platform/$version is already published" >&2; exit 1; }

  # What the previous publish carried that this one would not.
  prev="$(feedgen newest "${published[@]}")"
  if [[ -n "$prev" ]]; then
    prev_src="$(git -C "$DOCK360" rev-parse "ohs-platform/$prev^{commit}")"
    # A publish tag sits on the stamp commit; its parent is what was built.
    [[ "$(git -C "$DOCK360" log -1 --format=%s "$prev_src")" == "OpenHamShack platform "* ]] \
      && prev_src="$(git -C "$DOCK360" rev-parse "$prev_src^")"
    say "compared with $prev (source $(git -C "$DOCK360" rev-parse --short "$prev_src"))"
    git -C "$DOCK360" log --oneline "$prev_src..$commit" | sed 's/^/   adds    /' | head -40
    dropped="$(git -C "$DOCK360" log --oneline "$commit..$prev_src")"
    if [[ -n "$dropped" ]]; then
      echo "$dropped" | sed 's/^/   REMOVES /'
      (( ALLOW_REMOVAL )) || { echo "the ref lacks commits $prev shipped; merge them, or --allow-removal" >&2; exit 1; }
    fi
    git -C "$DOCK360" diff --stat "$prev_src" "$commit" | tail -1 | sed 's/^/   /'
  fi

  printf '%s\n' "$version" > "$SRC/$edition_file"
  printf '%s\n' "$engine" > "$SRC/VERSION"
  git -C "$SRC" add VERSION "$edition_file"
  git -C "$SRC" -c user.name="$(git -C "$DOCK360" config user.name || echo publish-feeds)" \
    -c user.email="$(git -C "$DOCK360" config user.email || echo publish-feeds@localhost)" \
    commit -q -m "OpenHamShack platform $version (engine $engine)" \
    -m "Stamped by openhamshack-os tools/publish-feeds.sh from $REF ($commit). Only VERSION and PLATFORM_VERSION differ from that commit; publish numbers are not feature commits."
  pkg="$WORK/dock360-$engine.d360upd"
  dock360ctl release package "$SRC" -o "$pkg" --key "$MASTER" \
    --notes "${NOTES:-OpenHamShack $version from $REF @ $short}"
  feedgen platform "$OUT" --channel "$CHANNEL" --version "$version" --engine "$engine" --package "$pkg" \
    --notes "${NOTES:-OpenHamShack $version}"
  dock360ctl release sign-feed "$OUT/feeds/platform-$CHANNEL.json" --key "$MASTER"
fi

if (( DO_CATALOGUE )); then
  say "catalogue from $APPS"
  feedgen catalogue "$OUT" "$SRC" "$APPS"
  dock360ctl release sign-feed "$OUT/feeds/catalogue.json" --key "$MODULES"
fi

# The files are served read-only by a web server running as this user.
find "$OUT/feeds" -type d -exec chmod 755 {} + ; find "$OUT/feeds" -type f -exec chmod 644 {} +
say "check, as a station reads them"
feedgen check "$OUT" "$SRC" --channel "$CHANNEL"
# Tagged last, once the feed is written and reads back: a publish that failed
# part-way leaves no tag claiming a version nobody was offered.
if (( DO_PLATFORM )); then
  git -C "$DOCK360" tag -a "ohs-platform/$version" -m "OpenHamShack platform $version, $CHANNEL channel, from $REF" \
    "$(git -C "$SRC" rev-parse HEAD)"
  say "tagged ohs-platform/$version in $DOCK360 (local; not pushed)"
fi
