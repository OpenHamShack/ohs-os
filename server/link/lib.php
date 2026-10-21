<?php
// The link-test rendezvous and relay on openhamshack.com: two copies of
// OpenHamShack Packet Radio find each other by callsign and pass AX.25
// frames and telemetry both ways. ohs-libs shack_packet/weblink.py is the
// client; "Web protocol 1" in its LINK-TEST.md is the contract.
//
// Why it exists (Brian, 20/10/2026): the 0.2.1 link test needed a relay on
// the controller box, reachable only on the tailnet, and Ray (GM0OTB) could
// not reach it without a policy change. Every station can reach
// openhamshack.com over HTTPS, so the rendezvous moved here.
//
// The site is 20i shared hosting: PHP 8.0, no long-running processes, no
// sockets held open between requests. So everything is a request:
//   hello  - "I am CALL and I want to test with PEER" (plus a random token
//            this run made up; every later request carries it)
//   send   - a batch of messages for the other station
//   poll   - a long poll: the messages waiting for me after a cursor,
//            held up to POLL_MAX_S seconds if there are none yet
//   bye    - leaving
// and the state between requests is files under ~/ohs-link (outside the web
// root), locked with flock:
//   pairs/<id>/state.json    the two stations' slots (token hash, hello)
//   pairs/<id>/q-<CALL>      messages waiting for CALL, one JSON line each;
//                            a cursor is a byte offset into it
//   pairs/<id>/seen-<CALL>   touched whenever CALL is heard from, so the
//                            other side can tell it has gone quiet
//   logs/<date>-<id>.jsonl   every message and event, for the operator
//   rate/                    per-address hello counts (HMAC, daily salt)
//
// Decisions:
// - **A pair is both stations asking for each other.** The pair's id comes
//   from the two normalised calls, sorted, so GM4BRI asking for GM0OTB and
//   GM0OTB asking for GM4BRI land in one folder and nobody else's request
//   can. A station asking for someone who is not asking back just waits.
//   SSIDs count (GM4BRI-1 is not GM4BRI-2); -0 is the bare call.
// - **The token is the credential.** 32 hex characters the client makes up
//   per run; only its SHA-256 is stored. A hello with a different token
//   replaces that call's slot (the same operator restarting), empties its
//   queue and tells the other station, which sees the old run leave and the
//   new one join - never two runs answering for one callsign at once.
//   Anybody can claim a callsign here, as anybody can on the air; what the
//   token stops is a third party injecting frames into a run under way.
// - **Files, not SQLite.** A queue is an append-only file and a cursor its
//   byte offset, so a long poll is a filesize() check every 50 ms - cheap
//   on shared storage - and nothing has to be deleted mid-run.
// - **Limits** so one bad client cannot fill the account: a request body of
//   MAX_BODY, MAX_MSGS a batch, a message line of MAX_LINE, a queue of
//   MAX_QUEUE (the run must start again), REQ_PER_MIN requests a station a
//   minute, HELLO_PER_HOUR hellos an address an hour, MAX_PAIRS pairs at
//   once; pairs idle for PAIR_IDLE_S and logs older than KEEP_DAYS go.
// - **Fails closed.** Any error is an answer with a reason; the client
//   treats a station it cannot reach as a lost return path, which drops
//   its links without transmitting (shack_packet returnpath.py).
declare(strict_types=1);

const LINK_PROTOCOL = 1;
const LINK_MAX_BODY = 262144;
const LINK_MAX_MSGS = 400;
const LINK_MAX_LINE = 65536;
const LINK_MAX_ENV = 16384;
const LINK_MAX_QUEUE = 16777216;
const LINK_POLL_MAX_S = 20;
const LINK_POLL_STEP_US = 50000;
const LINK_SEEN_EVERY_S = 4;
const LINK_REQ_PER_MIN = 900;
const LINK_HELLO_PER_HOUR = 60;
const LINK_MAX_PAIRS = 200;
const LINK_PAIR_IDLE_S = 3600;
const LINK_KEEP_DAYS = 30;
const LINK_CALL = '/^([A-Z0-9]{1,6})(?:-([0-9]|1[0-5]))?$/';

function link_answer(int $status, array $body): void
{
    http_response_code($status);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($body, JSON_UNESCAPED_SLASHES), "\n";
}

function link_fail(int $status, string $reason, string $detail = ''): void
{
    $body = ['ok' => false, 'error' => $reason];
    if ($detail !== '') {
        $body['detail'] = $detail;
    }
    link_answer($status, $body);
}

/** GM4BRI-0 -> GM4BRI, gm4bri-1 -> GM4BRI-1; null if it is not an AX.25
 *  address with a letter and a digit in it. */
function link_call($text): ?string
{
    if (!is_string($text)) {
        return null;
    }
    $t = strtoupper(trim($text));
    if (!preg_match(LINK_CALL, $t, $m)) {
        return null;
    }
    if (!preg_match('/[A-Z]/', $m[1]) || !preg_match('/[0-9]/', $m[1])) {
        return null;
    }
    $ssid = isset($m[2]) ? (int)$m[2] : 0;
    return $ssid === 0 ? $m[1] : $m[1] . '-' . $ssid;
}

function link_pair_id(string $a, string $b): string
{
    $calls = [$a, $b];
    sort($calls, SORT_STRING);
    return substr(hash('sha256', 'ohs-link|' . implode('|', $calls)), 0, 24);
}

function link_now(?float $now): float
{
    return $now ?? microtime(true);
}

/** The day's salt for address hashing, made on first use, gone with the day. */
function link_salt(string $root, float $now): string
{
    $day = gmdate('Ymd', (int)$now);
    $f = "$root/rate/salt-$day";
    if (!is_file($f)) {
        @file_put_contents($f, bin2hex(random_bytes(16)), LOCK_EX);
        @chmod($f, 0600);
    }
    return (string)@file_get_contents($f);
}

/** Count one event in a bucket file; false once over `most`. */
function link_count(string $file, int $most): bool
{
    $h = @fopen($file, 'c+');
    if ($h === false) {
        return true;            // never refuse a run because the counter broke
    }
    flock($h, LOCK_EX);
    $n = (int)stream_get_contents($h) + 1;
    ftruncate($h, 0);
    rewind($h);
    fwrite($h, (string)$n);
    flock($h, LOCK_UN);
    fclose($h);
    return $n <= $most;
}

function link_log(string $root, string $id, float $now, array $entry): void
{
    $line = json_encode(['relay_wall' => round($now, 6)] + $entry, JSON_UNESCAPED_SLASHES) . "\n";
    $f = "$root/logs/" . gmdate('Y-m-d', (int)$now) . "-$id.jsonl";
    @file_put_contents($f, $line, FILE_APPEND | LOCK_EX);
    @chmod($f, 0600);
}

/** Append messages to a queue under its own lock; false if it is full. */
function link_append(string $file, array $lines): bool
{
    $h = fopen($file, 'ab');
    if ($h === false) {
        return false;
    }
    flock($h, LOCK_EX);
    clearstatcache(true, $file);
    $size = (int)filesize($file);
    $data = implode('', $lines);
    if ($size + strlen($data) > LINK_MAX_QUEUE) {
        flock($h, LOCK_UN);
        fclose($h);
        return false;
    }
    fwrite($h, $data);
    fflush($h);
    flock($h, LOCK_UN);
    fclose($h);
    return true;
}

/** Whole lines from `cursor` on (at most ~MAX_BODY of them) and the new cursor. */
function link_read(string $file, int $cursor): array
{
    $h = @fopen($file, 'rb');
    if ($h === false) {
        return [[], $cursor];
    }
    flock($h, LOCK_SH);
    fseek($h, $cursor);
    $chunk = (string)fread($h, LINK_MAX_BODY);
    flock($h, LOCK_UN);
    fclose($h);
    $end = strrpos($chunk, "\n");
    if ($end === false) {
        return [[], $cursor];
    }
    $msgs = [];
    foreach (explode("\n", substr($chunk, 0, $end)) as $line) {
        $m = json_decode($line, true);
        if (is_array($m)) {
            $msgs[] = $m;
        }
    }
    return [$msgs, $cursor + $end + 1];
}

/** Run `fn(state)` with the pair locked; it returns [state or null to leave
 *  it, result]. */
function link_locked(string $dir, callable $fn)
{
    $h = fopen("$dir/lock", 'c');
    flock($h, LOCK_EX);
    $raw = @file_get_contents("$dir/state.json");
    $state = $raw ? json_decode($raw, true) : null;
    if (!is_array($state)) {
        $state = ['slots' => []];
    }
    [$new, $result] = $fn($state);
    if ($new !== null) {
        $tmp = "$dir/state.json.tmp";
        file_put_contents($tmp, json_encode($new, JSON_UNESCAPED_SLASHES));
        chmod($tmp, 0600);
        rename($tmp, "$dir/state.json");
    }
    flock($h, LOCK_UN);
    fclose($h);
    return $result;
}

function link_seen_age(string $dir, string $call, float $now): ?float
{
    clearstatcache(true, "$dir/seen-$call");
    $t = @filemtime("$dir/seen-$call");
    return $t === false ? null : max(0.0, round($now - $t, 1));
}

/** What a station is told about the other one. */
function link_peer_view(string $dir, array $state, string $peer, float $now): array
{
    $slot = $state['slots'][$peer] ?? null;
    if ($slot === null || !empty($slot['left'])) {
        return ['state' => 'absent'];
    }
    return ['state' => 'here', 'gen' => substr($slot['token'], 0, 8), 'hello' => $slot['hello'],
            'seen_s' => link_seen_age($dir, $peer, $now)];
}

/** Pairs nobody has touched for PAIR_IDLE_S and logs past KEEP_DAYS. Cheap:
 *  run from hello only. */
function link_prune(string $root, float $now): int
{
    $gone = 0;
    foreach (glob("$root/pairs/*", GLOB_ONLYDIR) ?: [] as $dir) {
        $t = 0;
        foreach (glob("$dir/*") ?: [] as $f) {
            $t = max($t, (int)@filemtime($f));
        }
        if ($now - $t > LINK_PAIR_IDLE_S) {
            foreach (glob("$dir/*") ?: [] as $f) {
                @unlink($f);
            }
            @rmdir($dir);
            $gone++;
        }
    }
    foreach (glob("$root/logs/*.jsonl") ?: [] as $f) {
        if ($now - (int)@filemtime($f) > LINK_KEEP_DAYS * 86400) {
            @unlink($f);
        }
    }
    foreach (glob("$root/rate/*") ?: [] as $f) {
        if ($now - (int)@filemtime($f) > 2 * 86400) {
            @unlink($f);
        }
    }
    return $gone;
}

/** The station's own slot, checked: [dir, id, call, peer] or an answer sent. */
function link_station(string $root, array $req): ?array
{
    $call = link_call($req['call'] ?? null);
    $peer = link_call($req['peer'] ?? null);
    $token = $req['token'] ?? null;
    if ($call === null || $peer === null || !is_string($token) || !preg_match('/^[0-9a-f]{32}$/', $token)) {
        link_fail(400, 'bad-request');
        return null;
    }
    $id = link_pair_id($call, $peer);
    $dir = "$root/pairs/$id";
    $raw = @file_get_contents("$dir/state.json");
    $state = $raw ? json_decode($raw, true) : null;
    $slot = is_array($state) ? ($state['slots'][$call] ?? null) : null;
    if ($slot === null || !hash_equals((string)$slot['token'], hash('sha256', $token))) {
        // Another run of this callsign took the slot, or it never had one.
        link_fail(409, 'superseded');
        return null;
    }
    if (!empty($slot['left'])) {
        link_fail(409, 'left');
        return null;
    }
    // Per station, per minute; a new run starts its own count.
    if (!link_count("$dir/rate-$call-" . (int)(link_now(null) / 60), LINK_REQ_PER_MIN)) {
        link_fail(429, 'too-fast');
        return null;
    }
    return [$dir, $id, $call, $peer];
}

function link_hello(string $root, array $req, float $now, string $addr): void
{
    if (($req['v'] ?? null) !== LINK_PROTOCOL) {
        link_fail(400, 'protocol', 'this relay speaks protocol ' . LINK_PROTOCOL);
        return;
    }
    $call = link_call($req['call'] ?? null);
    $peer = link_call($req['peer'] ?? null);
    $token = $req['token'] ?? null;
    if ($call === null || $peer === null) {
        link_fail(400, 'bad-call');
        return;
    }
    if ($call === $peer) {
        link_fail(400, 'same-call');
        return;
    }
    if (!is_string($token) || !preg_match('/^[0-9a-f]{32}$/', $token)) {
        link_fail(400, 'bad-token');
        return;
    }
    $env = $req['env'] ?? [];
    if (!is_array($env) || strlen(json_encode($env)) > LINK_MAX_ENV) {
        link_fail(400, 'bad-env');
        return;
    }
    $version = is_string($req['version'] ?? null) ? substr($req['version'], 0, 40) : '';
    $salt = link_salt($root, $now);
    $who = substr(hash_hmac('sha256', $addr, $salt), 0, 16);
    if (!link_count("$root/rate/hello-$who-" . (int)($now / 3600), LINK_HELLO_PER_HOUR)) {
        link_fail(429, 'too-many-hellos');
        return;
    }
    link_prune($root, $now);
    $id = link_pair_id($call, $peer);
    $dir = "$root/pairs/$id";
    if (!is_dir($dir)) {
        if (count(glob("$root/pairs/*", GLOB_ONLYDIR) ?: []) >= LINK_MAX_PAIRS) {
            link_fail(503, 'busy');
            return;
        }
        @mkdir($dir, 0700, true);
    }
    $hello = ['call' => $call, 'peer' => $peer, 'version' => $version, 'env' => (object)$env,
              'wall' => is_numeric($req['wall'] ?? null) ? (float)$req['wall'] : null];
    $result = link_locked($dir, function (array $state) use ($dir, $call, $peer, $token, $hello, $now) {
        $old = $state['slots'][$call] ?? null;
        $state['slots'][$call] = ['token' => hash('sha256', $token), 'hello' => $hello, 'joined' => $now,
                                  'left' => false];
        // A fresh queue: nothing meant for the run this one replaces.
        file_put_contents("$dir/q-$call", '');
        chmod("$dir/q-$call", 0600);
        touch("$dir/seen-$call");
        $other = $state['slots'][$peer] ?? null;
        $tell = [];
        if ($old !== null && empty($old['left'])) {
            $tell[] = json_encode(['t' => 'peer', 'state' => 'left', 'why' => 'replaced', 'relay_wall' => $now]) . "\n";
        }
        if ($other !== null && empty($other['left'])) {
            $tell[] = json_encode(['t' => 'peer', 'state' => 'joined', 'gen' => substr(hash('sha256', $token), 0, 8),
                                   'hello' => $hello, 'relay_wall' => $now], JSON_UNESCAPED_SLASHES) . "\n";
            link_append("$dir/q-$peer", $tell);
        }
        return [$state, ['peer' => link_peer_view($dir, $state, $peer, $now), 'replaced' => $old !== null]];
    });
    link_log($root, $id, $now, ['from' => 'relay', 'event' => 'join', 'call' => $call, 'peer' => $peer,
                                'version' => $version, 'replaced' => $result['replaced'], 'env' => (object)$env]);
    $calls = [$call, $peer];
    sort($calls, SORT_STRING);
    link_answer(200, ['ok' => true, 'v' => LINK_PROTOCOL, 'pair' => $id, 'call' => $call, 'peer_call' => $peer,
                      'first' => $calls[0], 'cursor' => 0, 'peer' => $result['peer'], 'relay_wall' => $now,
                      'poll_max_s' => LINK_POLL_MAX_S]);
}

function link_send(string $root, array $req, float $now): void
{
    $st = link_station($root, $req);
    if ($st === null) {
        return;
    }
    [$dir, $id, $call, $peer] = $st;
    touch("$dir/seen-$call");
    $msgs = $req['msgs'] ?? [];
    if (!is_array($msgs) || count($msgs) > LINK_MAX_MSGS) {
        link_fail(400, 'bad-batch');
        return;
    }
    $lines = [];
    $log = '';
    foreach ($msgs as $m) {
        if (!is_array($m) || !is_string($m['t'] ?? null)) {
            link_fail(400, 'bad-message');
            return;
        }
        // The relay's own kinds are never accepted from a station.
        if (in_array($m['t'], ['peer', 'welcome', 'error'], true)) {
            link_fail(400, 'bad-message', $m['t']);
            return;
        }
        $line = json_encode($m, JSON_UNESCAPED_SLASHES);
        if (strlen($line) > LINK_MAX_LINE) {
            link_fail(413, 'message-too-big');
            return;
        }
        $lines[] = $line . "\n";
        $lag = is_numeric($m['wall'] ?? null) ? round($now - (float)$m['wall'], 4) : null;
        $log .= json_encode(['relay_wall' => round($now, 6), 'from' => $call, 'lag_s' => $lag, 'msg' => $m],
                            JSON_UNESCAPED_SLASHES) . "\n";
    }
    $peerHere = false;
    $raw = @file_get_contents("$dir/state.json");
    $state = $raw ? json_decode($raw, true) : null;
    $other = is_array($state) ? ($state['slots'][$peer] ?? null) : null;
    $peerHere = $other !== null && empty($other['left']);
    if ($lines && $peerHere && !link_append("$dir/q-$peer", $lines)) {
        link_fail(507, 'queue-full');
        return;
    }
    if ($log !== '') {
        $f = "$root/logs/" . gmdate('Y-m-d', (int)$now) . "-$id.jsonl";
        @file_put_contents($f, $log, FILE_APPEND | LOCK_EX);
        @chmod($f, 0600);
    }
    link_answer(200, ['ok' => true, 'n' => count($lines), 'delivered' => $peerHere, 'relay_wall' => $now]);
}

function link_poll(string $root, array $req, ?float $fixedNow): void
{
    $st = link_station($root, $req);
    if ($st === null) {
        return;
    }
    [$dir, $id, $call, $peer] = $st;
    $cursor = $req['cursor'] ?? 0;
    $wait = $req['wait'] ?? 0;
    if (!is_int($cursor) || $cursor < 0 || !is_numeric($wait)) {
        link_fail(400, 'bad-request');
        return;
    }
    $wait = max(0.0, min((float)LINK_POLL_MAX_S, (float)$wait));
    @set_time_limit((int)$wait + 15);
    $q = "$dir/q-$call";
    $start = microtime(true);
    $touched = 0.0;
    $tokenHash = null;
    while (true) {
        $t = microtime(true);
        if ($t - $touched >= LINK_SEEN_EVERY_S) {
            touch("$dir/seen-$call");
            $touched = $t;
            // Replaced by a newer run of this callsign while waiting: say so
            // rather than hand its messages to the old one.
            $raw = @file_get_contents("$dir/state.json");
            $state = $raw ? json_decode($raw, true) : null;
            $slot = is_array($state) ? ($state['slots'][$call] ?? null) : null;
            $tokenHash = $tokenHash ?? ($slot['token'] ?? null);
            if ($slot === null || $slot['token'] !== $tokenHash || !empty($slot['left'])) {
                link_fail(409, 'superseded');
                return;
            }
        }
        clearstatcache(true, $q);
        $size = @filesize($q);
        if ($size === false) {
            link_fail(409, 'superseded');
            return;
        }
        if ($size < $cursor) {
            link_fail(400, 'bad-cursor');
            return;
        }
        if ($size > $cursor || $t - $start >= $wait || connection_aborted()) {
            break;
        }
        usleep(LINK_POLL_STEP_US);
    }
    [$msgs, $next] = link_read($q, $cursor);
    $now = link_now($fixedNow);
    $raw = @file_get_contents("$dir/state.json");
    $state = $raw ? json_decode($raw, true) : ['slots' => []];
    link_answer(200, ['ok' => true, 'msgs' => $msgs, 'cursor' => $next,
                      'peer' => link_peer_view($dir, $state, $peer, $now), 'relay_wall' => $now]);
}

function link_bye(string $root, array $req, float $now): void
{
    $st = link_station($root, $req);
    if ($st === null) {
        return;
    }
    [$dir, $id, $call, $peer] = $st;
    $why = is_string($req['why'] ?? null) ? substr($req['why'], 0, 40) : 'bye';
    link_locked($dir, function (array $state) use ($dir, $call, $peer, $now, $why) {
        $state['slots'][$call]['left'] = true;
        $other = $state['slots'][$peer] ?? null;
        if ($other !== null && empty($other['left'])) {
            link_append("$dir/q-$peer", [json_encode(['t' => 'peer', 'state' => 'left', 'why' => $why,
                                                      'relay_wall' => $now]) . "\n"]);
        }
        return [$state, null];
    });
    link_log($root, $id, $now, ['from' => 'relay', 'event' => 'leave', 'call' => $call, 'why' => $why]);
    link_answer(200, ['ok' => true, 'relay_wall' => $now]);
}

/** The endpoint: one POST of JSON with `op`. */
function link_main(string $root, ?float $fixedNow): void
{
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        header('Allow: POST');
        link_fail(405, 'post-only');
        return;
    }
    $len = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($len <= 0 || $len > LINK_MAX_BODY) {
        link_fail(413, 'body-size');
        return;
    }
    $req = json_decode((string)file_get_contents('php://input', false, null, 0, LINK_MAX_BODY), true);
    if (!is_array($req) || !is_string($req['op'] ?? null)) {
        link_fail(400, 'bad-json');
        return;
    }
    foreach (['pairs', 'logs', 'rate'] as $d) {
        if (!is_dir("$root/$d")) {
            @mkdir("$root/$d", 0700, true);
        }
    }
    $now = link_now($fixedNow);
    switch ($req['op']) {
        case 'hello':
            link_hello($root, $req, $now, (string)($_SERVER['REMOTE_ADDR'] ?? ''));
            return;
        case 'send':
            link_send($root, $req, $now);
            return;
        case 'poll':
            link_poll($root, $req, $fixedNow);
            return;
        case 'bye':
            link_bye($root, $req, $now);
            return;
        default:
            link_fail(400, 'bad-op');
    }
}
