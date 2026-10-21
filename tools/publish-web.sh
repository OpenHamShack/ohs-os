#!/usr/bin/env bash
# Upload the update feeds (or, after the handover, the website) to
# openhamshack.com on 20i, in one command, with no agent.
#
#   tools/publish-web.sh feeds [--dry-run] [--remove PATH]...
#   tools/publish-web.sh site  [--dry-run] [--remove PATH]... [--remove-holding] --docroot-is-ours
#   tools/publish-web.sh key-month [--days 30]   one upload key with an end date
#   tools/publish-web.sh key-revoke              remove it now
#
#   feeds  ~/feeds/www/feeds (publish-feeds.sh's output) -> public_html/feeds
#   site   ~/build/ohs-site (the built website)            -> public_html,
#          never touching feeds/. The NB session handed the docroot over on
#          09/10 with a holding page and /feeds/.htaccess (no-cache on the
#          feeds, octet-stream types, no index); --docroot-is-ours stays as a
#          deliberate "yes, replace the live site" for the first full upload.
#          Never add an HTTPS redirect to .htaccess: the CDN reaches the
#          origin over plain HTTP, so it loops (NB session found this).
#          --remove-holding also deletes what is left of that holding page
#          (tokens.css, icons/, logo/ and its root files) once the site no
#          longer has a file of the same name; never anything under feeds/.
#
# Run it on ohs-dev or on shack. The upload itself has to happen on shack,
# which is where the Control360 lab-session helper is (ohs-dev holds no
# Control360 credential, on purpose), so on ohs-dev it copies the local
# folder and itself to shack:~/build/ohs-web/ and runs again there.
#
# The route is openhamshack-nb tools/publish.sh's, which NB has run for real
# (HANDOVER.md "How to publish"). With a month key in place (key-month,
# below) an upload just uses it. Without one, each run
# mints a temporary ed25519 keypair, adds the public half to 20i package
# PACKAGE_ID through the Control360 API (a lab session stands in for an
# operator login), uses it, then removes the key and the lab session in a
# trap - whether the run worked or not. The session's cookie and CSRF are
# never echoed, logged or put on a command line.
#
# Order, so a station never fetches a feed before what it points at exists:
#   1. back up what will change (~/backups/ohs-<target>-<date>/, on shack)
#   2. the payload: packages and app archives (anything not a feed file)
#   3. pages, index.html last (site only)
#   4. each feed's .sig, then the feed itself
#   5. removals - only the paths named with --remove (or --remove-holding),
#      never rsync --delete: a station part-way through an update may still
#      be fetching an old one
#   6. every uploaded file checked over HTTPS with a ?v= cache-buster
#      (StackCDN serves stale copies at the edge). Up to 64 MiB it is
#      re-downloaded and its sha256 compared. Above that (the ISO is 1.3 GB,
#      which would outrun the old fixed 300 s download) the sha256 is taken at
#      the origin over SSH, and HTTPS proves the same object is served: a
#      ranged read of its last MiB must answer 206 with the local size as
#      the total and the local bytes. A host without sha256sum, or a server
#      that ignores Range, falls back to the full download with a timeout
#      scaled to the size.
set -euo pipefail
prog=$(basename "$0")
here=$(cd "$(dirname "$0")" && pwd)

target=${1:-}
shift || true
dry_run=0; on_shack=0; ours=0; remove_paths=(); days=30; remove_holding=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1; shift ;;
        --remove) remove_paths+=("${2:?--remove needs a path}"); shift 2 ;;
        --docroot-is-ours) ours=1; shift ;;
        --remove-holding) remove_holding=1; shift ;;
        --on-shack) on_shack=1; shift ;;
        --days) days=${2:?--days needs a number}; shift 2 ;;
        *) echo "usage: $prog feeds|site|key-month|key-revoke [--dry-run] [--remove PATH]... [--remove-holding] [--docroot-is-ours] [--days N]" >&2; exit 2 ;;
    esac
done

case "$target" in
    key-month|key-revoke) default_local=/nonexistent; remote_subdir=""; url_base="" ;;
    feeds) default_local=$HOME/feeds/www/feeds; remote_subdir=public_html/feeds; url_base=https://openhamshack.com/feeds ;;
    site)  default_local=$HOME/build/ohs-site;  remote_subdir=public_html;       url_base=https://openhamshack.com
           [ "$ours" = 1 ] || { echo "$prog: the docroot is the NB session's until it hands over; add --docroot-is-ours once it has" >&2; exit 2; } ;;
    *) echo "usage: $prog feeds|site|key-month|key-revoke [--dry-run] [--remove PATH]..." >&2; exit 2 ;;
esac
[ "$remove_holding" = 0 ] || [ "$target" = site ] || { echo "$prog: --remove-holding is for the site only" >&2; exit 2; }
url_base=${PUBLISH_URL_BASE:-$url_base}
local_dir=${PUBLISH_LOCAL_DIR:-$default_local}
log() { echo "$prog: $*"; }

lab_session_helper=${PUBLISH_LAB_SESSION_HELPER:-$HOME/.claude/jobs/e0b9840c/tmp/lab-session.sh}

# --- on ohs-dev: hand over to shack ---------------------------------------
if [ "$on_shack" = 0 ] && [ ! -f "$lab_session_helper" ]; then
    stage="build/ohs-web/$target"
    ssh shack "mkdir -p ~/$stage"
    if [ -n "$remote_subdir" ]; then
        [ -d "$local_dir" ] || { log "$local_dir does not exist"; exit 1; }
        log "copying $local_dir to shack:~/$stage"
        # --delete here is only the staging copy on shack, never the website.
        rsync -a --delete "$local_dir/" "shack:$stage/"
    fi
    rsync -a "$here/publish-web.sh" shack:build/ohs-web/publish-web.sh
    args=("$target" --on-shack --days "$days")
    [ "$dry_run" = 1 ] && args+=(--dry-run)
    [ "$ours" = 1 ] && args+=(--docroot-is-ours)
    [ "$remove_holding" = 1 ] && args+=(--remove-holding)
    for p in "${remove_paths[@]}"; do args+=(--remove "$p"); done
    exec ssh shack "PUBLISH_LOCAL_DIR=\$HOME/$stage bash build/ohs-web/publish-web.sh $(printf '%q ' "${args[@]}")"
fi

# --- on shack ------------------------------------------------------------------
[ -f "$lab_session_helper" ] || { log "no lab-session helper at $lab_session_helper (run on shack)"; exit 1; }
remote_host=${PUBLISH_REMOTE_HOST:user@ssh.example-host}
api_base=${PUBLISH_API_BASE:-https://control360.co.uk/api/v1}
package_id=${PUBLISH_PACKAGE_ID:-PACKAGE_ID}
confirm_header=${PUBLISH_CONFIRM_HEADER:-openhamshack.com}
control_a_host=${PUBLISH_CONTROL_A_HOST:-control-a}
backup_dir=${PUBLISH_BACKUP_ROOT:-$HOME/backups}/ohs-${target}-$(date -u +%Y%m%d-%H%M%S)

# The month key (Brian, 09/10 evening: "a lot of updates in the next
# month"). His earlier rule was no standing key at all; this relaxes it to
# one key with an end date. It lives here on shack only - never copied to
# ohs-dev, a repo or another session - and a timer removes it from 20i on
# its end date whether or not anything was published. While it is valid an
# upload needs no Control360 session and no half-hour wait; once it lapses
# uploads go back to the temporary-key route below.
STANDING=${PUBLISH_STANDING_DIR:-$HOME/.config/openhamshack/20i}
standing_key=$STANDING/id_ed25519
standing_meta=$STANDING/meta   # handle=<20i handle>, expires=<epoch seconds>
meta() { sed -n "s/^$1=//p" "$standing_meta" 2>/dev/null; }
standing_valid() {  # present, and good for at least another hour
    [ -f "$standing_key" ] && [ -n "$(meta handle)" ] && [ "$(meta expires)" -gt $(( $(date +%s) + 3600 )) ] 2>/dev/null
}

tmp=$(mktemp -d -p "$HOME/build")
chmod 700 "$tmp"
key="$tmp/id_ed25519"
handle=""; cookie=""; csrf=""; token_sha=""; key_added=0

lab_open() {
    [ -z "$cookie" ] || return 0
    local out
    out=$(C360_HOST="${C360_HOST:-control-a}" bash "$lab_session_helper" owner)
    cookie=$(sed -n 's/^COOKIE=//p' <<< "$out")
    csrf=$(sed -n 's/^CSRF=//p' <<< "$out")
    [ -n "$cookie" ] && [ -n "$csrf" ] || { log "the lab-session helper did not print COOKIE=/CSRF="; exit 1; }
    token_sha=$(printf '%s' "$cookie" | sha256sum | cut -d' ' -f1)
}
api_add_key() {  # handle pubkey-file reason
    local body
    body=$(python3 -c 'import json,sys; print(json.dumps({"handle": sys.argv[1], "publicKey": open(sys.argv[2]).read().strip()}))' "$1" "$2")
    # The body goes on stdin: the cookie headers stay the only sensitive argv.
    curl -sS -f -X POST \
        -H "Cookie: c360_session=${cookie}" -H "X-C360-Csrf: ${csrf}" \
        -H "X-C360-Reason: $prog $3" -H "X-C360-Confirm: ${confirm_header}" \
        -H "Content-Type: application/json" --data @- \
        "${api_base}/dns/hosting/packages/${package_id}/ssh/keys" <<< "$body" >/dev/null
}
api_del_key() {  # handle reason
    curl -sS -f -o /dev/null -X DELETE \
        -H "Cookie: c360_session=${cookie}" -H "X-C360-Csrf: ${csrf}" \
        -H "X-C360-Reason: $prog $2" -H "X-C360-Confirm: ${confirm_header}" \
        "${api_base}/dns/hosting/packages/${package_id}/ssh/keys/$1"
}
cleanup() {
    local rc=$?
    if [ "$key_added" = 1 ] && [ -n "$handle" ]; then
        api_del_key "$handle" cleanup || log "could not delete the temporary ssh key (handle ${handle}) - remove it by hand"
    fi
    if [ -n "$token_sha" ]; then
        # This session only, by its token's hash - never "revoke every lab
        # session", which would end other sessions' work too.
        ssh "$control_a_host" \
            "sudo -u postgres psql -qtAX -d c360 -c \"DELETE FROM core.sessions WHERE token_sha256 = '${token_sha}'\"" \
            >/dev/null || log "could not delete the lab session - it expires on its own"
    fi
    rm -rf "$tmp"
    exit "$rc"
}
trap cleanup EXIT

wait_accepted() {  # ssh options... ; sets ssh_cmd
    # A fresh key can take about half an hour before 20i's SSH host accepts
    # it (NB session, 09/10: refused at 4 minutes, accepted at 31). A script
    # waiting costs nothing.
    local i
    for i in $(seq 1 ${PUBLISH_KEY_WAIT_TRIES:-80}); do
        "${ssh_cmd[@]}" "$remote_host" true 2>/dev/null && { log "key accepted"; return 0; }
        [ "$i" = 1 ] && log "waiting for 20i to accept the key (often ~30 minutes)"
        sleep 30
    done
    log "20i never accepted the key (40 minutes)"
    return 1
}
revoke_standing() {
    [ -f "$standing_meta" ] || { rm -f "$standing_key" "$standing_key.pub"; return 0; }
    lab_open
    if api_del_key "$(meta handle)" "month key ends"; then
        log "removed the month key $(meta handle) from 20i"
        rm -f "$standing_key" "$standing_key.pub" "$standing_meta"
    else
        log "could not remove the month key $(meta handle) from 20i; kept the local copy so a retry can"
        return 1
    fi
    systemctl --user stop ohs-20i-key-revoke.timer >/dev/null 2>&1 || true
}

case "$target" in
    key-revoke)
        revoke_standing
        exit $? ;;
    key-month)
        if standing_valid; then
            log "a month key is already in place until $(date -d @"$(meta expires)" '+%F %H:%M')"
            exit 0
        fi
        [ -f "$standing_meta" ] && revoke_standing
        install -d -m 700 "$STANDING"
        rm -f "$standing_key" "$standing_key.pub"
        expires=$(( $(date +%s) + days * 86400 ))
        handle_std="ohs-month-until-$(date -d @"$expires" +%Y%m%d)"
        ssh-keygen -q -t ed25519 -N "" -f "$standing_key" -C "openhamshack.com upload key until $(date -d @"$expires" +%F)"
        lab_open
        api_add_key "$handle_std" "$standing_key.pub" "month key (${days} days)"
        printf 'handle=%s\nexpires=%s\n' "$handle_std" "$expires" > "$standing_meta"
        chmod 600 "$standing_meta"
        # The end date is enforced here, not trusted to memory: a user timer
        # on shack runs key-revoke then (Persistent, so a reboot past the
        # date revokes at the next start).
        systemctl --user stop ohs-20i-key-revoke.timer >/dev/null 2>&1 || true
        systemd-run --user --quiet --unit=ohs-20i-key-revoke --timer-property=Persistent=true \
            --on-calendar="$(date -d @"$expires" '+%F %H:%M:%S')" \
            bash "$HOME/build/ohs-web/publish-web.sh" key-revoke --on-shack \
            || log "WARNING: could not schedule the removal; run key-revoke by $(date -d @"$expires" +%F)"
        log "month key $handle_std added; it ends $(date -d @"$expires" '+%F %H:%M') and is removed then"
        ssh_cmd=(ssh -i "$standing_key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20)
        wait_accepted || exit 1
        exit 0 ;;
esac

[ -d "$local_dir" ] || { log "$local_dir does not exist"; exit 1; }
if standing_valid; then
    log "using the month key (until $(date -d @"$(meta expires)" +%F))"
    ssh_cmd=(ssh -i "$standing_key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20)
    "${ssh_cmd[@]}" "$remote_host" true 2>/dev/null || wait_accepted || exit 1
else
    [ -f "$standing_meta" ] && { log "the month key has lapsed; removing it"; revoke_standing || true; }
    # No month key: a temporary one for this run only, removed in the trap.
    ssh-keygen -q -t ed25519 -N "" -f "$key" -C "$prog $target $(date -u +%FT%TZ)"
    lab_open
    handle="ohs-${target}-$(date -u +%Y%m%d%H%M%S)-$$"
    api_add_key "$handle" "$key.pub" "${target} upload"
    key_added=1
    ssh_cmd=(ssh -i "$key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20)
    wait_accepted || { log "nothing uploaded"; exit 1; }
fi
rsync_ssh="${ssh_cmd[*]}"
remote() { "${ssh_cmd[@]}" "$remote_host" "$@"; }
lines_of() { [ "$#" -eq 0 ] || printf '%s\n' "$@"; }

# The holding page the NB session put up on 09/10 (its source is
# ~/.claude/jobs/90cad700/tmp/ohs-site/ on shack): every file it has outside
# feeds/, bar index.html (the site replaces it) and .htaccess (the site
# carries its own, and a dotfile is config, not leftovers). A name the site
# itself still ships is skipped below, so the site's own favicons and
# robots.txt stay; what goes is what nothing links to any more. An explicit
# list rather than "whatever the site lacks": anything else on the docroot -
# a file another session put there - is not this script's to delete.
holding_files=(
    tokens.css
    icons/atv.svg icons/datv.svg icons/digital-modes.svg icons/flex.svg icons/icom.svg
    icons/platform.svg icons/qo100-nb.svg icons/qo100-wb.svg icons/radio.svg icons/remote.svg
    logo/lockup-horizontal-on-dark.svg logo/lockup-horizontal-on-light.svg
    apple-touch-icon.png icon-192.png icon-512.png favicon.ico favicon.svg robots.txt
)
holding_dirs=(icons logo)
if [ "$remove_holding" = 1 ]; then
    for p in "${holding_files[@]}"; do
        [ -e "$local_dir/$p" ] || remove_paths+=("$p")
    done
fi
# The site owns the docroot except feeds/, which publish-feeds.sh fills and
# stations read mid-update; no removal of the site's may reach it.
if [ "$target" = site ]; then
    for p in "${remove_paths[@]}"; do
        case "$p" in feeds|feeds/*|./feeds*) log "refusing to remove $p: feeds/ is not the site's"; exit 2 ;; esac
    done
fi

remote mkdir -p "$remote_subdir"
excl=(); [ "$target" = site ] && excl=(--exclude=/feeds/)
# Listed into a file first so a failed rsync stops the run, rather than
# reading as "nothing to change".
rsync -rcn "${excl[@]}" --out-format='%n' -e "$rsync_ssh" "$local_dir/" \
    "${remote_host}:${remote_subdir}/" > "$tmp/changes"
mapfile -t all_files < <(grep -v '/$' "$tmp/changes" | sort || true)
is_feed='(^|/)[^/]+\.json(\.sig)?$'
mapfile -t feeds_sig < <(lines_of "${all_files[@]}" | grep -E '\.json\.sig$' | grep -E "$is_feed" || true)
mapfile -t feeds_json < <(lines_of "${all_files[@]}" | grep -E '\.json$' || true)
mapfile -t pages < <(lines_of "${all_files[@]}" | grep -E '\.html$' | grep -vE '(^|/)index\.html$' || true)
mapfile -t index_pages < <(lines_of "${all_files[@]}" | grep -E '(^|/)index\.html$' || true)
mapfile -t payload < <(lines_of "${all_files[@]}" | grep -vE '\.html$' | grep -vE "$is_feed" || true)
# In the feeds folder every .json is a feed; in the site a .json is just a file.
if [ "$target" = site ]; then
    feeds_sig=(); feeds_json=()
    mapfile -t payload < <(lines_of "${all_files[@]}" | grep -vE '\.html$' || true)
fi

verify_full_max=${PUBLISH_VERIFY_FULL_MAX:-67108864}  # 64 MiB
size_of() { stat -c %s "$local_dir/$1"; }

if [ "$dry_run" = 1 ]; then
    log "dry run: ${#all_files[@]} files would change under ${remote_host}:${remote_subdir}/"
    for f in "${all_files[@]}"; do
        if [ "$(size_of "$f")" -gt "$verify_full_max" ]; then
            echo "  would upload: $f ($(size_of "$f") bytes; verified by origin sha256 + ranged HTTPS read)"
        else
            echo "  would upload: $f"
        fi
    done
    for p in "${remove_paths[@]}"; do log "  would remove: $p"; done
    log "dry run only: nothing uploaded or removed"
    exit 0
fi
[ "${#all_files[@]}" -gt 0 ] || [ "${#remove_paths[@]}" -gt 0 ] || { log "nothing to do: the site already matches $local_dir"; exit 0; }

mkdir -p "$backup_dir"
{ lines_of "${all_files[@]}"; lines_of "${remove_paths[@]}"; } | sort -u > "$tmp/backup-list"
rsync -az --ignore-missing-args --files-from="$tmp/backup-list" -e "$rsync_ssh" \
    "${remote_host}:${remote_subdir}/" "$backup_dir/" || true
log "backed up what is about to change to $backup_dir"

push() {  # push FILE... (relative to local_dir), in one rsync
    [ "$#" -gt 0 ] || return 0
    rsync -rz --chmod=D755,F644 --files-from=<(printf '%s\n' "$@") -e "$rsync_ssh" \
        "$local_dir/" "${remote_host}:${remote_subdir}/"
}
push "${payload[@]}"
push "${pages[@]}"
push "${index_pages[@]}"
push "${feeds_sig[@]}"
push "${feeds_json[@]}"
log "uploaded ${#all_files[@]} files"

removed=()
for p in "${remove_paths[@]}"; do
    [ -n "$p" ] || continue
    case "$p" in /*|*..*) log "refusing to remove $p (absolute, or leaves the folder)"; continue ;; esac
    remote rm -f -- "$(printf '%q' "${remote_subdir}/${p}")"
    removed+=("$p")
    log "removed $p"
done
if [ "$remove_holding" = 1 ]; then
    # rmdir, not rm -r: a folder that still holds anything (the site's own
    # files, or someone else's) stays.
    for d in "${holding_dirs[@]}"; do
        [ -e "$local_dir/$d" ] && continue
        remote rmdir -- "$(printf '%q' "${remote_subdir}/${d}")" 2>/dev/null && log "removed the empty folder $d/" || true
    done
fi

cache_buster() { od -An -N8 -tx1 /dev/urandom | tr -d ' \n'; }
full_check() {  # FILE: download it all and compare; seconds allowed grow with the size
    local f=$1 want got secs
    # 120 s, plus one second per 256 KiB (a slow 2 Mbit/s still finishes).
    secs=$(( 120 + $(size_of "$f") / 262144 ))
    want=$(sha256sum "$local_dir/$f" | cut -d' ' -f1)
    got=$(curl -sSfL -m "$secs" "${url_base}/${f}?v=$(cache_buster)" | sha256sum | cut -d' ' -f1)
    [ "$want" = "$got" ]
}
tail_check() {  # FILE: 206, the local size as Content-Range's total, the local last MiB
    local f=$1 size start code total
    size=$(size_of "$f"); start=$(( size > 1048576 ? size - 1048576 : 0 ))
    # --max-filesize stops a server that ignores Range from sending all of
    # a 1.3 GB file here; that answer is "no range", and the caller falls back.
    code=$(curl -sSL -m 120 -r "${start}-$(( size - 1 ))" --max-filesize 2097152 \
        -D "$tmp/range-headers" -o "$tmp/range-body" -w '%{http_code}' \
        "${url_base}/${f}?v=$(cache_buster)" 2>/dev/null) || code=000
    [ "$code" = 206 ] || { echo norange; return; }
    total=$(tr -d '\r' < "$tmp/range-headers" | sed -n 's#^[Cc]ontent-[Rr]ange: bytes [0-9]*-[0-9]*/\([0-9]*\)$#\1#p' | tail -1)
    if [ "$total" = "$size" ] && cmp -s "$tmp/range-body" <(tail -c +$(( start + 1 )) "$local_dir/$f"); then
        echo ok
    else
        echo "bad (served size ${total:-unknown}, local $size)"
    fi
}

fail=0; big=(); n_full=0
for f in "${all_files[@]}"; do
    case "${f##*/}" in .*) continue ;; esac  # dotfiles are never served
    if [ "$(size_of "$f")" -gt "$verify_full_max" ]; then big+=("$f"); continue; fi
    n_full=$(( n_full + 1 ))
    full_check "$f" || { log "verify FAILED: ${url_base}/${f}"; fail=1; }
done
if [ "${#big[@]}" -gt 0 ]; then
    # One SSH round trip for every large file's hash at the origin. A file
    # it cannot hash simply has no line, and gets the full download below.
    origin=$(remote "cd $(printf '%q' "$remote_subdir") && sha256sum -- $(printf '%q ' "${big[@]}")" 2>/dev/null || true)
    for f in "${big[@]}"; do
        want=$(sha256sum "$local_dir/$f" | cut -d' ' -f1)
        got=$(awk -v f="$f" '{h=$1; sub(/^[^ ]+ [ *]/, ""); if ($0 == f) print h}' <<< "$origin")
        how="origin sha256 + ranged HTTPS read"
        if [ -z "$got" ]; then
            log "no sha256 from the origin for $f; downloading it whole ($(size_of "$f") bytes)"
            how="full download"
            full_check "$f" || { log "verify FAILED: ${url_base}/${f}"; fail=1; }
            continue
        fi
        [ "$want" = "$got" ] || { log "verify FAILED: ${f} at the origin has sha256 $got, not $want"; fail=1; continue; }
        served=$(tail_check "$f")
        case "$served" in
            ok) ;;
            norange)
                log "${url_base}/${f} ignored Range; downloading it whole"
                how="origin sha256 + full download"
                full_check "$f" || { log "verify FAILED: ${url_base}/${f}"; fail=1; continue; } ;;
            *) log "verify FAILED: ${url_base}/${f} is not what was uploaded: $served"; fail=1; continue ;;
        esac
        log "verified $f ($(size_of "$f") bytes) by $how"
    done
fi
for p in "${removed[@]}"; do
    # A cache-buster reaches the origin, so a removed file should be gone.
    code=$(curl -sS -o /dev/null -m 60 -w '%{http_code}' "${url_base}/${p}?v=$(cache_buster)" 2>/dev/null) || code=000
    [ "$code" = 404 ] || log "note: ${url_base}/${p} still answers $code after removal"
done
[ "$fail" = 0 ] || { log "some files did not verify (above); check before telling anyone"; exit 1; }
log "PASS: ${#all_files[@]} files verified at ${url_base}/ (${n_full} re-downloaded, ${#big[@]} large by size and checksum)"
