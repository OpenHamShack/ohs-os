#!/usr/bin/env bash
# Put the link-test relay (server/link) on openhamshack.com, prove it, and
# measure it from here.
#
#   tools/publish-link.sh [--dry-run] [--check-only]
#
#   server/link/lib.php, admin.php  -> ~/ohs-link/bin/        (outside the web root)
#   server/link/htaccess-store      -> ~/ohs-link/.htaccess
#   server/link/link.php            -> public_html/api/link/link.php
#   server/link/htaccess-api-link   -> public_html/api/link/.htaccess
#
# Modelled on publish-diag.sh, and its own tool for the same reason:
# publish-web.sh site checks each file by fetching it, and a POST-only
# endpoint answers a GET with 405. It touches only the paths above. To be
# sure of that rather than believe it, it lists the whole docroot (path,
# size, mtime) before and after, and fails if anything outside
# public_html/api/link changed or went - the site, /feeds/, /desktop/,
# /downloads/, /source/ and api/diag included.
#
# Then it checks the live endpoint (GET 405; nothing else in api/link
# served) and runs a real pairing between two throwaway callsigns
# (Q0TSTA/Q0TSTB: no station has a Q prefix), relaying messages both ways
# and reporting the round trips seen from this host: a send's POST, and a
# message from one station's send to the other's long poll returning. The
# run is logged by the relay like any other and its pair goes when idle.
#
# Runs on shack (the month key is there: publish-web.sh key-month).
set -euo pipefail
prog=$(basename "$0")
here=$(cd "$(dirname "$0")" && pwd)
dry_run=0; check_only=0
for a in "$@"; do
    case "$a" in
        --dry-run) dry_run=1 ;;
        --check-only) check_only=1 ;;
        *) echo "usage: $prog [--dry-run] [--check-only]" >&2; exit 2 ;;
    esac
done
log() { echo "$prog: $*"; }
src=${PUBLISH_LINK_SRC:-$here/../server/link}
STANDING=${PUBLISH_STANDING_DIR:-$HOME/.config/openhamshack/20i}
remote_host=${PUBLISH_REMOTE_HOST:user@ssh.example-host}
url=${PUBLISH_LINK_URL:-https://openhamshack.com/api/link/link.php}
work=${PUBLISH_LINK_WORK:-$HOME/build/ohs-link-publish}
mkdir -p "$work"

meta() { sed -n "s/^$1=//p" "$STANDING/meta" 2>/dev/null; }
expires=$(meta expires)
if [ ! -f "$STANDING/id_ed25519" ] || [ -z "$expires" ] || [ "$expires" -le $(( $(date +%s) + 600 )) ]; then
    log "no valid month key in $STANDING: run publish-web.sh key-month first"
    exit 1
fi
ssh_cmd=(ssh -i "$STANDING/id_ed25519" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes
         -o ConnectTimeout=20)
remote() { "${ssh_cmd[@]}" "$remote_host" "$@"; }

snapshot() {   # every file under public_html: path, size, mtime
    remote 'cd public_html && find . -type f -printf "%p\t%s\t%T@\n" | sort'
}

if [ "$check_only" = 0 ]; then
    for f in lib.php admin.php link.php htaccess-api-link htaccess-store; do
        [ -f "$src/$f" ] || { log "missing $src/$f"; exit 1; }
    done
    snapshot > "$work/docroot-before.tsv"
    log "docroot before: $(wc -l < "$work/docroot-before.tsv") files ($work/docroot-before.tsv)"
    if [ "$dry_run" = 1 ]; then
        log "dry run: would upload lib.php, admin.php -> ~/ohs-link/bin/; link.php -> public_html/api/link/"
        grep -c '^\./api/link/' "$work/docroot-before.tsv" | sed "s/^/$prog: already in api\/link: /" || true
        exit 0
    fi
    # Staging first, linted by the host's own php (8.0), then the library,
    # then the endpoint, so the endpoint never requires a file not there.
    # Web folders 755 (Apache must read .htaccess: publish-diag.sh's first
    # deploy); ~/ohs-link 700.
    remote 'mkdir -p public_html/api/link && chmod 755 public_html/api public_html/api/link
        umask 077; mkdir -p ohs-link/bin ohs-link/pairs ohs-link/logs ohs-link/rate ohs-link/.staging && chmod 700 ohs-link'
    rsync -rc --chmod=F600 -e "${ssh_cmd[*]}" "$src/lib.php" "$src/admin.php" "$src/link.php" \
        "$remote_host:ohs-link/.staging/"
    remote 'cd ohs-link/.staging && for f in lib.php admin.php link.php; do php -l "$f" >/dev/null || { echo "lint failed: $f"; exit 1; }; done'
    log "linted with $(remote 'php -r "echo PHP_VERSION;"')"
    remote 'set -e; mv -f ohs-link/.staging/lib.php ohs-link/bin/lib.php; mv -f ohs-link/.staging/admin.php ohs-link/bin/admin.php'
    rsync -c --chmod=F644 -e "${ssh_cmd[*]}" "$src/htaccess-store" "$remote_host:ohs-link/.htaccess"
    rsync -c --chmod=F644 -e "${ssh_cmd[*]}" "$src/htaccess-api-link" "$remote_host:public_html/api/link/.htaccess"
    remote 'chmod 644 ohs-link/.staging/link.php && mv -f ohs-link/.staging/link.php public_html/api/link/link.php && rmdir ohs-link/.staging'
    snapshot > "$work/docroot-after.tsv"
    # Anything outside api/link that changed or went is a failure.
    outside() { grep -v '^\./api/link/' "$1" || true; }
    if ! diff <(outside "$work/docroot-before.tsv") <(outside "$work/docroot-after.tsv") > "$work/docroot.diff"; then
        log "FAIL: files outside api/link changed during the upload (someone else publishing?):"
        head -20 "$work/docroot.diff"
        exit 1
    fi
    log "docroot outside api/link unchanged ($(outside "$work/docroot-after.tsv" | wc -l) files)"
    log "uploaded; checking $url"
fi

cb() { od -An -N8 -tx1 /dev/urandom | tr -d ' \n'; }
code() { curl -sS -o /dev/null -m 30 -w '%{http_code}' "$@" 2>/dev/null || echo 000; }
fail=0
# A folder made a moment ago reads as unreadable for about a minute on
# 20i (publish-diag.sh): 403 until then.
for _ in $(seq 1 18); do
    [ "$(code "$url?v=$(cb)")" = 403 ] || break
    sleep 10
done
got=$(code "$url?v=$(cb)")
[ "$got" = 405 ] || { log "FAIL: GET $url answered $got, not 405"; fail=1; }
base=${url%/link.php}
for p in .htaccess lib.php admin.php; do
    got=$(code "$base/$p?v=$(cb)")
    case "$got" in 403|404) ;; *) log "FAIL: $base/$p answered $got"; fail=1 ;; esac
done
[ "$fail" = 0 ] || exit 1
log "PASS: $url answers POST only; nothing else in api/link is served"

# A real pairing, timed from here.
python3 - "$url" <<'EOF'
import http.client, json, secrets, ssl, statistics, sys, threading, time, urllib.parse
url = sys.argv[1]
u = urllib.parse.urlsplit(url)
def conn():
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=40, context=ssl.create_default_context())
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=40)
def op(c, **body):
    c.request("POST", u.path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); out = json.loads(r.read())
    if r.status != 200: raise SystemExit(f"FAIL: {body['op']} answered {r.status} {out}")
    return out
A, B = "Q0TSTA", "Q0TSTB"
ta, tb = secrets.token_hex(16), secrets.token_hex(16)
ca, cb, cp = conn(), conn(), conn()
op(ca, op="hello", v=1, call=A, peer=B, token=ta, version="publish-link", env={"check": True})
hb = op(cb, op="hello", v=1, call=B, peer=A, token=tb, version="publish-link", env={"check": True})
assert hb["peer"]["state"] == "here", hb
cur = op(cp, op="poll", call=B, peer=A, token=tb, cursor=0, wait=0)["cursor"]
posts, relays = [], []
for i in range(10):
    got = {}
    def wait():
        t = time.monotonic()
        got["out"] = op(cp, op="poll", call=B, peer=A, token=tb, cursor=cur, wait=15)
        got["at"] = time.monotonic()
    th = threading.Thread(target=wait); th.start()
    time.sleep(0.4)                      # the poll is waiting at the server
    t0 = time.monotonic()
    op(ca, op="send", call=A, peer=B, token=ta, msgs=[{"t": "tele", "kind": "check", "n": i, "wall": time.time()}])
    posts.append(time.monotonic() - t0)
    th.join(30)
    msgs = got["out"]["msgs"]
    assert [m.get("n") for m in msgs] == [i], msgs
    cur = got["out"]["cursor"]
    relays.append(got["at"] - t0)
op(ca, op="bye", call=A, peer=B, token=ta, why="publish-check")
op(cb, op="bye", call=B, peer=A, token=tb, why="publish-check")
ms = lambda xs: f"median {statistics.median(xs)*1000:.0f} ms, min {min(xs)*1000:.0f}, max {max(xs)*1000:.0f}"
print(f"publish-link.sh: send POST round trip: {ms(posts)}")
print(f"publish-link.sh: one station's send to the other's long poll answering: {ms(relays)}")
print("publish-link.sh: PASS: two stations paired and relayed 10 messages")
EOF
