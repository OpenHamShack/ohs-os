#!/bin/bash
# Boot the ISO in QEMU+KVM, headless, every way a user would, install it
# unattended to a fresh virtual disk, boot that disk, and check the
# controller from outside (tests/probe.py).
#
#   tests/boot-test.sh <iso> [mode ...]
#
# Modes (default: the first four):
#   uefi-cd    q35 + OVMF (pflash code + a fresh copy of the vars), ISO as a CD
#   bios-cd    pc + SeaBIOS, ISO as a CD
#   uefi-usb   q35 + OVMF, ISO raw on a USB stick (what Rufus DD / dd write)
#   bios-usb   pc + SeaBIOS, the same stick
#   ventoy-uefi, ventoy-bios
#              a Ventoy stick (VENTOY_DIR=<unpacked ventoy-x.y.z>) with the
#              ISO copied onto it; see tests/ventoy-stick.sh
#   <mode>+powercut
#              the same, but the first boot of the installed disk has its
#              power cut (QEMU killed) a few seconds into first boot's install
#              of its first app, and the probe runs on the boot after: a
#              controller unplugged during its first start must finish it.
#              Only meaningful when the ISO has --first-apps; needs FIRST_BOOT=1.
#
# APP=<id> is the app the probe checks (default openhamshack-fakebox, installed
# from the store by the probe). FIRST_BOOT=1 says APP is one of the ISO's
# --first-apps: the probe then waits for first boot's own install and checks
# the app answers, rather than installing it a second time.
# STORE_LISTS="<id> ..." makes the probe check the store lists every one of
# them (the beta ISO's eight), and THEN_INSTALL=<id> (with FIRST_BOOT=1)
# installs that app from the store, offline, after first boot's own.
# WEBSOCKET="<id> <path>" opens that app's WebSocket through the front door
# (port 80) as a signed-in device and needs 101, not the 403 platform
# 2.0.0-beta.4 gave every Remote stream; the app must be installed by then
# (APP or THEN_INSTALL).
#
# Each mode:
#   1. install: firmware + medium + a blank 32 GB virtio disk (/dev/vda), no
#      network at all, SMBIOS product "openhamshack-ci" so the ISO's GRUB picks
#      its unattended entry. Passes when the serial console says
#      "OPENHAMSHACK-INSTALL: done" and the machine powers itself off.
#   2. boot: the same firmware with only the disk, networking restricted
#      (`-nic user,restrict=on`: no route out, only the two forwarded ports),
#      then tests/probe.py: discovery, first-run setup, the store, installing
#      the preloaded app offline.
# Finally the controllers' ids are compared: every install must make its own.
#
# Needs: qemu-system-x86_64, /dev/kvm, ovmf, python3. Runs as an ordinary
# user (in the kvm group). Work files go in WORK (default ~/build/iso/boottest),
# never /tmp: each disk grows to several GB.
set -uo pipefail

ISO="$(readlink -f "${1:?usage: $0 <iso> [mode ...]}")"; shift
MODES=("$@"); [[ ${#MODES[@]} -gt 0 ]] || MODES=(uefi-cd bios-cd uefi-usb bios-usb)
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$HOME/build/iso/boottest}"
OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-2400}"
APP="${APP:-openhamshack-fakebox}"
FIRST_BOOT="${FIRST_BOOT:-0}"
STORE_LISTS="${STORE_LISTS:-}"
THEN_INSTALL="${THEN_INSTALL:-}"
WEBSOCKET="${WEBSOCKET:-}"
MEM="${MEM:-4096}"
mkdir -p "$WORK"
RESULTS="$WORK/results.txt"
: > "$RESULTS"

qemu_base() {
  # -cpu host: the image's userland is ordinary x86-64, but a guest without
  # the host's features runs podman's image unpacking much slower.
  echo qemu-system-x86_64 -enable-kvm -cpu host -m "$MEM" -smp 4 -display none -no-user-config -nodefaults \
    -vga std
}

firmware() {  # <uefi|bios> <dir>
  if [[ "$1" == uefi ]]; then
    [[ -f "$2/vars.fd" ]] || cp "$OVMF_VARS" "$2/vars.fd"
    echo -machine q35 \
      -drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF_CODE" \
      -drive if=pflash,format=raw,unit=1,file="$2/vars.fd"
  else
    echo -machine pc
  fi
}

medium() {  # <cd|usb|ventoy> <dir>
  case "$1" in
    # ide-cd sits on the q35's AHCI or the pc's PIIX IDE alike.
    cd) echo -drive file="$ISO",media=cdrom,readonly=on,if=none,id=cd -device ide-cd,drive=cd,bootindex=0 ;;
    # A raw, read-only USB stick holding the ISO byte for byte: exactly what
    # Rufus in DD mode, Etcher or dd leave on a real one.
    usb) echo -device qemu-xhci,id=xhci \
           -drive file="$ISO",format=raw,readonly=on,if=none,id=stick \
           -device usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0 ;;
    ventoy) echo -device qemu-xhci,id=xhci \
           -drive file="$2/ventoy.img",format=raw,if=none,id=stick \
           -device usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0 ;;
  esac
}

screendump() {  # <monitor socket> <png>: the VM's screen, as QEMU draws it
  python3 -I - "$1" "$2" <<'PY' 2>/dev/null || true
import socket, struct, sys, time, zlib, os
sock, png = sys.argv[1], sys.argv[2]
ppm = png + ".ppm"
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sock)
time.sleep(0.3); s.recv(65536)
s.sendall(f"screendump {ppm}\n".encode()); time.sleep(2); s.close()
data = open(ppm, "rb").read(); os.unlink(ppm)
magic, dims, maxval, px = data.split(b"\n", 3)
w, h = map(int, dims.split())
raw = b"".join(b"\0" + px[y * w * 3:(y + 1) * w * 3] for y in range(h))
chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
open(png, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
PY
}

# QEMU here has no GPU to lend (the build host has no /dev/dri), so the
# guest has no render node, and an app that declares one - QO-100 WB maps
# /dev/dri/renderD128 for Quick Sync - fails to start: "stat
# /dev/dri/renderD128: no such file or directory". A real controller has one
# (the ThinkCentre's UHD 630). For the test the guest loads vgem, the kernel's
# virtual GEM driver, whose render node is enough for the container to start;
# the encoder itself is not exercised by this test. It goes in as systemd
# credentials over SMBIOS (an extra unit and two drop-ins, systemd 256+), so
# the disk under test is not modified and nothing of it is in the image.
# The same app binds /dev/bus/usb whole for its tuners, which exists only
# with a USB controller, so the booted disk gets one (every real PC has one;
# -nodefaults leaves QEMU without).
render_node() {
  local unit dropin
  unit=$(printf '%s\n' "[Unit]" "Description=Boot test only: vgem's render node, standing in for a GPU" \
    "DefaultDependencies=no" "ConditionPathExists=!/etc/initrd-release" \
    "[Service]" "Type=oneshot" "RemainAfterExit=yes" "ExecStart=/usr/sbin/modprobe vgem" | base64 -w0)
  dropin=$(printf '%s\n' "[Unit]" "Wants=ohs-test-render.service" "After=ohs-test-render.service" | base64 -w0)
  echo -smbios "type=11,value=io.systemd.credential.binary:systemd.extra-unit.ohs-test-render.service=$unit" \
    -smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.openhamshack-firstboot.service=$dropin" \
    -smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.dock360-api.service=$dropin"
}

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }

boot_disk() {  # <fw> <dir> <serial log>: boot the installed disk, daemonized; prints QEMU's pid
  local p_admin p_front
  p_admin=$(free_port); p_front=$(free_port)
  echo "$p_admin $p_front" > "$2/ports"
  # shellcheck disable=SC2046
  $(qemu_base) $(firmware "$1" "$2") $(render_node) -device qemu-xhci \
    -drive file="$2/disk.qcow2",if=none,id=hd,format=qcow2 -device virtio-blk-pci,drive=hd,bootindex=1 \
    -nic user,model=virtio-net-pci,restrict=on,hostfwd=tcp:127.0.0.1:"$p_admin"-:8800,hostfwd=tcp:127.0.0.1:"$p_front"-:80 \
    -serial file:"$3" -daemonize -pidfile "$2/qemu.pid" > "$2/qemu-boot.out" 2>&1
  cat "$2/qemu.pid" 2>/dev/null
}

stop_vm() {  # <pid>: by PID only - other sessions' VMs run on this host too
  kill "$1" 2>/dev/null; sleep 2; kill -9 "$1" 2>/dev/null
}

# The power cut: boot, wait until first boot is loading its first app's
# image (the long step, and where the first interim ISO was caught), let it
# run a few seconds more, then kill QEMU outright - no shutdown, no sync,
# whatever is in the guest's page cache is lost, as with a pulled plug.
# Prints CUT, MISSED (first boot finished before the cut) or NOSTART.
power_cut() {  # <fw> <dir>
  local pid start=$SECONDS
  pid="$(boot_disk "$1" "$2" "$2/boot-cut.log")"
  [[ -n "$pid" ]] || { echo NOSTART; return; }
  while (( SECONDS - start < 900 )); do
    if grep -q "openhamshack-firstboot.*done$" "$2/boot-cut.log" 2>/dev/null; then
      stop_vm "$pid"; echo MISSED; return
    fi
    if grep -q "openhamshack-firstboot.*Loading the image" "$2/boot-cut.log" 2>/dev/null; then
      sleep "${CUT_AFTER:-5}"
      kill -9 "$pid" 2>/dev/null; sleep 1
      echo CUT; return
    fi
    sleep 1
  done
  stop_vm "$pid"; echo MISSED
}

run_mode() {
  local mode="$1" fw media dir pid p_admin p_front rc cut="" probe_args=()
  if [[ "$mode" == *+powercut ]]; then cut=1; mode="${mode%+powercut}"; fi
  fw="${mode%%-*}"; media="${mode#*-}"
  if [[ "$fw" == ventoy ]]; then fw="${mode#ventoy-}"; media=ventoy; fi
  [[ -n "$cut" ]] && mode="$mode+powercut"
  dir="$WORK/$mode"
  rm -rf "$dir"; mkdir -p "$dir"
  echo "=== $mode: $fw firmware, ISO as $media"
  if [[ "$media" == ventoy ]]; then
    "$HERE/ventoy-stick.sh" "$ISO" "$dir/ventoy.img" || { echo "$mode install=SKIP (no Ventoy stick)" >> "$RESULTS"; return; }
  fi
  qemu-img create -q -f qcow2 "$dir/disk.qcow2" 32G

  # 1. Install. No network device at all: the installer needs none.
  local start=$SECONDS
  # shellcheck disable=SC2046
  timeout "$INSTALL_TIMEOUT" $(qemu_base) $(firmware "$fw" "$dir") $(medium "$media" "$dir") \
    -drive file="$dir/disk.qcow2",if=none,id=hd,format=qcow2 -device virtio-blk-pci,drive=hd,bootindex=1 \
    -smbios type=1,product=openhamshack-ci -nic none -no-reboot \
    -serial file:"$dir/install.log" -monitor unix:"$dir/monitor.sock",server,nowait \
    > "$dir/qemu-install.out" 2>&1 &
  local qpid=$!
  # Until QEMU ends, a picture of the screen every minute: firmware, Ventoy
  # and a GRUB that fails before reading grub.cfg say nothing on the serial
  # port, and a timed-out run is otherwise a blank log.
  while kill -0 "$qpid" 2>/dev/null; do
    sleep 60
    screendump "$dir/monitor.sock" "$dir/install-screen.png"
  done
  wait "$qpid"
  rc=$?
  if grep -q "OPENHAMSHACK-INSTALL: done" "$dir/install.log" && [[ $rc -eq 0 ]]; then
    echo "  install PASS ($((SECONDS - start)) s)"
  else
    echo "  install FAIL (qemu rc=$rc after $((SECONDS - start)) s); serial log tail:"
    tail -15 "$dir/install.log" | sed 's/^/    /'
    [[ -f "$dir/install-screen.png" ]] && echo "    last screen: $dir/install-screen.png"
    echo "$mode install=FAIL boot=- probe=-" >> "$RESULTS"
    return
  fi

  # 2. Boot the installed disk, no medium, no route out - after cutting the
  # power on its first boot, for +powercut.
  local cutres=""
  if [[ -n "$cut" ]]; then
    cutres="$(power_cut "$fw" "$dir")"
    echo "  power cut during first boot's install: $cutres"
    grep -E "openhamshack-firstboot" "$dir/boot-cut.log" | tail -3 | sed 's/^/  cut serial: /'
  fi
  pid="$(boot_disk "$fw" "$dir" "$dir/boot.log")"
  read -r p_admin p_front < "$dir/ports"
  if [[ -z "$pid" ]]; then
    echo "  boot FAIL: qemu did not start"; cat "$dir/qemu-boot.out"
    echo "$mode install=PASS boot=FAIL probe=-" >> "$RESULTS"; return
  fi
  start=$SECONDS
  [[ "$FIRST_BOOT" == 1 ]] && probe_args+=(--first-boot)
  [[ -n "$STORE_LISTS" ]] && probe_args+=(--store-lists "$STORE_LISTS")
  [[ -n "$THEN_INSTALL" ]] && probe_args+=(--then-install "$THEN_INSTALL")
  [[ -n "$WEBSOCKET" ]] && probe_args+=(--websocket "$WEBSOCKET")
  probe_args+=(--pages "$dir/pages")
  python3 -I "$HERE/probe.py" --admin "http://127.0.0.1:$p_admin" --front "http://127.0.0.1:$p_front" \
    --app "$APP" "${probe_args[@]}" --out "$dir/probe.json" 2>&1 | sed 's/^/  /'
  rc=${PIPESTATUS[0]}
  grep -E "openhamshack-firstboot|Welcome to|login:" "$dir/boot.log" | head -5 | sed 's/^/  serial: /'
  [[ -n "$cut" ]] && grep -E "openhamshack-firstboot: " "$dir/boot.log" | sed 's/^/  resumed: /'
  stop_vm "$pid"
  local booted=FAIL
  grep -q "OpenHamShack controller" "$dir/boot.log" && booted=PASS
  echo "  probe: $rc failure(s) ($((SECONDS - start)) s)"
  local extra=""
  if [[ -n "$cut" ]]; then
    # The cut only proves something if it landed mid-install, and the boot
    # after only passes if first boot itself says it finished.
    local resumed=FAIL
    grep -q "openhamshack-firstboot: done" "$dir/boot.log" && resumed=PASS
    extra=" powercut=$([[ "$cutres" == CUT ]] && echo PASS || echo "FAIL($cutres)") resumed=$resumed"
  fi
  echo "$mode install=PASS boot=$booted probe=$([[ $rc -eq 0 ]] && echo PASS || echo "FAIL($rc)")$extra" >> "$RESULTS"
}

for mode in "${MODES[@]}"; do
  run_mode "$mode"
done

# Every install must have made its own controller id (and with it its own
# database and secrets): one shared id would mean the image carried them.
ids=$(cat "$WORK"/*/probe.json 2>/dev/null | python3 -c '
import json, sys
text = sys.stdin.read()
dec = json.JSONDecoder(); i = 0; ids = []
while i < len(text):
    while i < len(text) and text[i].isspace(): i += 1
    if i >= len(text): break
    obj, i = dec.raw_decode(text, i); ids.append(obj.get("controllerId"))
print(" ".join(str(x) for x in ids))')
n_all=$(wc -w <<<"$ids"); n_unique=$(tr ' ' '\n' <<<"$ids" | grep -v '^None$' | sort -u | grep -c .)
echo "controller ids: $ids" >> "$RESULTS"
if [[ "$n_all" -gt 1 ]]; then
  [[ "$n_all" -eq "$n_unique" ]] && echo "ids unique=PASS ($n_all installs)" >> "$RESULTS" || echo "ids unique=FAIL" >> "$RESULTS"
fi
echo; echo "=== results"; cat "$RESULTS"
! grep -q "FAIL" "$RESULTS"
