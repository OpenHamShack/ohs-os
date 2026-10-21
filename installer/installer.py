#!/usr/bin/env python3
"""OpenHamShack installer. Writes the controller image carried on the ISO to
an internal disk.

Two ways to drive it:

* the touch page (default): serves one page on 127.0.0.1:8080, which the
  installer's kiosk browser shows full screen. Nothing touches a disk until
  the operator has picked it and tapped the install button twice.
* unattended (`--auto`), for CI and scripted installs: the kernel command
  line names the disk (`shack.install=auto shack.disk=/dev/vda`), progress
  goes to the console, and the machine powers off when it is done.

Stdlib only. Started from the archived Classic appliance's installer/installer.py at
50742ac.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PAYLOAD = Path(os.environ.get("SHACK_PAYLOAD", "/opt/openhamshack-installer/payload"))
PAGE = ROOT / "index.html"
CHUNK = 4 * 1024 * 1024

state_lock = threading.Lock()
state: dict = {"phase": "idle", "done": 0, "total": 0, "message": "", "target": None, "touched": False}


def set_state(**kw) -> None:
    with state_lock:
        state.update(kw)


def manifest() -> dict:
    return json.loads((PAYLOAD / "manifest.json").read_text())


def boot_disk() -> str | None:
    """The disk this installer is running from, which must never be offered.
    Booted from the live ISO, / is an overlay; the stick is whatever live-boot
    mounted at /run/live/medium (under Ventoy, a device-mapper node on it)."""
    src = ""
    for mount in ("/run/live/medium", "/"):
        src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", mount], capture_output=True, text=True).stdout.strip()
        if src.startswith("/dev/"):
            break
    if not src:
        return None
    parent = subprocess.run(["lsblk", "-n", "-d", "-o", "PKNAME", src], capture_output=True, text=True).stdout.strip()
    return f"/dev/{parent}" if parent else None


def disks(lsblk_json: str | None = None, booted_from: str | None = None) -> list[dict]:
    """Whole disks that are safe to offer: not the stick we booted from, not
    removable, not read-only, and big enough for the image."""
    if lsblk_json is None:
        lsblk_json = subprocess.run(
            ["lsblk", "-J", "-b", "-d", "-o", "NAME,PATH,SIZE,MODEL,TYPE,RM,RO,TRAN"],
            capture_output=True, text=True, check=True).stdout
        booted_from = boot_disk()
    need = manifest()["raw_bytes"] if (PAYLOAD / "manifest.json").exists() else 0
    out = []
    for d in json.loads(lsblk_json)["blockdevices"]:
        if d.get("type") != "disk" or d.get("path") == booted_from:
            continue
        if d.get("rm") in (True, "1") or d.get("ro") in (True, "1") or d.get("tran") == "usb":
            continue
        if int(d.get("size") or 0) < need:
            continue
        out.append({"path": d["path"], "model": (d.get("model") or "").strip() or d["name"],
                    "size": int(d["size"])})
    return out


def sha256_file(path: Path, phase: str, total: int) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            set_state(phase=phase, done=done, total=total)
    return h.hexdigest()


def remove_stale_boot_entries() -> None:
    """The old OS's NVRAM entry points at a loader that is about to vanish.
    Removing it lets the firmware fall back to the image's \\EFI\\BOOT path."""
    try:
        listing = subprocess.run(["efibootmgr"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in listing.splitlines():
        if line.startswith("Boot") and "*" in line[:9] and ("\\EFI\\debian" in line or "\\EFI\\ubuntu" in line):
            subprocess.run(["efibootmgr", "-q", "-b", line[4:8], "-B"], check=False)


def power_now(verb: str) -> None:
    """Reboot or power off straight away, without an orderly shutdown.

    The live system's root is a squashfs on the USB stick or DVD, and the
    screen tells the user to take the stick out before tapping Restart. An
    orderly `systemctl reboot` then blocks for ever reading units and
    binaries from a medium that has gone (first install on a real PC,
    ThinkCentre M720q from Ventoy, 09/10/2026: black screen, no reboot).
    QEMU never showed it because the boot test never removes the medium.
    Nothing needs an orderly stop: the target disk was written through a raw
    descriptor and fsync'd and is not mounted, and the live system keeps no
    state. So sync, then reboot(2) directly (`--force --force`)."""
    time.sleep(0.5)  # let the HTTP answer reach the page first
    os.sync()
    subprocess.run(["systemctl", verb, "--force", "--force"], check=False)


def install(target: str) -> None:
    try:
        m = manifest()
        img = PAYLOAD / m["file"]
        set_state(phase="checking", done=0, total=img.stat().st_size, message="", target=target)
        if sha256_file(img, "checking", img.stat().st_size) != m["zst_sha256"]:
            raise RuntimeError("The image on this USB stick or DVD is damaged. Write it again.")

        raw = int(m["raw_bytes"])
        set_state(phase="writing", done=0, total=raw)
        dec = subprocess.Popen(["zstd", "-dc", str(img)], stdout=subprocess.PIPE)
        written = 0
        fd = os.open(target, os.O_WRONLY)
        try:
            while chunk := dec.stdout.read(CHUNK):
                if not written:
                    set_state(touched=True)
                view = memoryview(chunk)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                written += len(chunk)
                set_state(done=written)
            os.fsync(fd)
        finally:
            os.close(fd)
        if dec.wait() != 0 or written != raw:
            raise RuntimeError(f"Writing stopped early ({written} of {raw} bytes).")

        # Read back from the disk itself, not the page cache that just wrote it.
        try:
            Path("/proc/sys/vm/drop_caches").write_text("3\n")
        except OSError:
            pass  # not root (tests); the readback then may come from cache
        set_state(phase="verifying", done=0, total=raw)
        h = hashlib.sha256()
        done = 0
        with open(target, "rb") as f:
            while done < raw:
                chunk = f.read(min(CHUNK, raw - done))
                if not chunk:
                    break
                h.update(chunk)
                done += len(chunk)
                set_state(done=done)
        if h.hexdigest() != m["raw_sha256"]:
            raise RuntimeError("The disk did not read back as written. Try again, or try another disk.")

        remove_stale_boot_entries()
        set_state(phase="done", message="")
    except Exception as exc:  # shown on the touchscreen; there is no console
        set_state(phase="failed", message=str(exc))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            with state_lock:
                body = dict(state)
            body["disks"] = disks() if body["phase"] in ("idle", "failed") else []
            try:
                body["label"] = manifest().get("label", "OpenHamShack")
            except (OSError, ValueError):
                body["label"] = None
                body["phase"] = "failed"
                body["message"] = "No OpenHamShack image found on this USB stick."
            self._json(200, body)
            return
        if path in ("/", "/index.html"):
            data = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if path == "/api/install":
            target = str(payload.get("target") or "")
            if target not in {d["path"] for d in disks()}:
                self._json(400, {"error": "not an offered disk"})
                return
            with state_lock:
                if state["phase"] not in ("idle", "failed"):
                    self._json(409, {"error": "already running"})
                    return
                state.update(phase="checking", done=0, total=0, message="", target=target, touched=False)
            threading.Thread(target=install, args=(target,), daemon=True).start()
            self._json(200, {"ok": True})
            return
        if path in ("/api/reboot", "/api/poweroff"):
            self._json(200, {"ok": True})
            threading.Thread(target=power_now, args=("reboot" if path == "/api/reboot" else "poweroff",),
                             daemon=True).start()
            return
        self.send_error(404)


# ---- Unattended install (begin) ----
# The line a watcher (tests/boot-test.sh, a CI log) looks for; everything
# after the colon is free text.
MARK = "OPENHAMSHACK-INSTALL:"


def cmdline_options(text: str | None = None) -> dict[str, str]:
    """The shack.* settings from the kernel command line."""
    if text is None:
        text = Path("/proc/cmdline").read_text()
    out = {}
    for word in text.split():
        key, sep, value = word.partition("=")
        if key.startswith("shack.") and sep:
            out[key] = value
    return out


def auto(text: str | None = None, out=print, power=None) -> int:
    """Install to the disk the command line names, with nobody watching.

    The disk must be named: an unattended installer that picked one itself
    would, on the wrong PC, erase someone's only disk without a word. It must
    also be a disk the touch page would offer (not the boot medium, not
    removable, big enough), so the command line cannot reach past the rules
    the operator's page lives by. Afterwards the machine powers off by
    default rather than rebooting: a PC that boots this medium first would
    otherwise install again, for ever.
    """
    opts = cmdline_options(text)
    target = opts.get("shack.disk", "")
    after = opts.get("shack.after", "poweroff")
    if after not in ("poweroff", "reboot", "none"):
        out(f"{MARK} failed: shack.after={after} is not poweroff, reboot or none")
        return 2
    if not target:
        out(f"{MARK} failed: shack.install=auto needs shack.disk=/dev/<disk>; nothing was written")
        return 2
    offered = {d["path"]: d for d in disks()}
    if target not in offered:
        out(f"{MARK} failed: {target} is not a disk this installer may write "
            f"(offered: {', '.join(offered) or 'none'}); nothing was written")
        return 2
    out(f"{MARK} installing to {target} ({offered[target]['model']}, {offered[target]['size']} bytes)")
    worker = threading.Thread(target=install, args=(target,), daemon=True)
    worker.start()
    last = None
    while worker.is_alive():
        worker.join(5)
        with state_lock:
            now = (state["phase"], int(100 * state["done"] / state["total"]) if state["total"] else 0)
        if now != last:
            out(f"{MARK} {now[0]} {now[1]}%")
            last = now
    with state_lock:
        phase, message = state["phase"], state["message"]
    if phase != "done":
        out(f"{MARK} failed: {message}")
        return 1
    out(f"{MARK} done")
    if after != "none":
        (power or power_now)(after)
    return 0
# ---- Unattended install (end) ----


def main() -> None:
    if "--auto" in sys.argv[1:]:
        sys.exit(auto(out=lambda line: print(line, flush=True)))
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
