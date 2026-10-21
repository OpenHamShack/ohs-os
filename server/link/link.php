<?php
// openhamshack.com/api/link/link.php: the link-test rendezvous and relay
// (POST only, JSON with `op`). Everything else lives outside the web root
// in ~/ohs-link (bin/lib.php says why); this file only finds it.
// OHS_LINK_ROOT and OHS_LINK_NOW are for the tests (tools/test_link_server.py):
// the host sets neither, and a request cannot set them (headers reach
// getenv() only as HTTP_*).
declare(strict_types=1);
$root = getenv('OHS_LINK_ROOT') ?: dirname(__DIR__, 3) . '/ohs-link';
require $root . '/bin/lib.php';
$now = getenv('OHS_LINK_NOW');
link_main($root, $now ? (float)$now : null);
