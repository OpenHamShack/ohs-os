<?php
// ~/ohs-link/bin/admin.php, over SSH only (tools/publish-link.sh runs it):
//   php admin.php stats         pairs live, logs kept, bytes used
//   php admin.php prune         idle pairs and old logs now, not at the next hello
//   php admin.php logs [N]      the newest N session logs (name, size), for fetching
declare(strict_types=1);
$root = getenv('OHS_LINK_ROOT') ?: dirname(__DIR__);
require __DIR__ . '/lib.php';
$cmd = $argv[1] ?? 'stats';
$now = microtime(true);
if ($cmd === 'prune') {
    echo "pruned ", link_prune($root, $now), " idle pairs\n";
} elseif ($cmd === 'logs') {
    $files = glob("$root/logs/*.jsonl") ?: [];
    usort($files, fn($a, $b) => filemtime($b) <=> filemtime($a));
    foreach (array_slice($files, 0, (int)($argv[2] ?? 20)) as $f) {
        printf("%s\t%d\n", basename($f), filesize($f));
    }
} elseif ($cmd === 'stats') {
    $bytes = 0;
    foreach (['pairs/*/*', 'logs/*'] as $g) {
        foreach (glob("$root/$g") ?: [] as $f) {
            $bytes += (int)filesize($f);
        }
    }
    printf("pairs %d\nlogs %d\nbytes %d\n", count(glob("$root/pairs/*", GLOB_ONLYDIR) ?: []),
        count(glob("$root/logs/*.jsonl") ?: []), $bytes);
} else {
    fwrite(STDERR, "usage: admin.php stats|prune|logs [N]\n");
    exit(2);
}
