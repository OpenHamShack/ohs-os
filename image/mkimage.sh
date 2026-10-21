#!/bin/bash
# The OpenHamShack controller's disk image, or the installer's live root.
#
#   ROLE=system    (default) the installed controller: Debian Trixie with the
#                  Dock360 OpenHamShack edition, written to a GPT disk image
#                  that boots both UEFI and legacy BIOS.
#   ROLE=installer the live system the ISO boots: the touch installer and the
#                  unattended one, kept as a directory (KEEP_STAGE) for
#                  mkiso.sh to squash. No Dock360 in it.
#
# Run as root on Debian with the network up; build.sh is the usual way in.
# Everything that needs the network happens here, at build time: the
# installed controller has to work on a hilltop with no route out, so its
# packages, Python environment and preloaded apps are all already on disk.
#
# Started from the archived Classic appliance's mkimage.sh at 50742ac; the 0.9.x daemon,
# longmynd, Try-OpenHamShack and tailnet-identity options are gone (the controller
# runs Dock360, and apps bring their own radios' software in containers).
set -euo pipefail

ROLE="${ROLE:-system}"
SUITE="${SUITE:-trixie}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
IMG="${IMG:-openhamshack.img}"
# Room for the OS (~2.5 GB with Dock360, podman and the venv) and the
# preloaded app packages twice over (the copy here and the catalogue's, until
# first boot removes ours) plus their unpacked images. growroot takes the
# root to the end of whatever disk the image lands on, so this only has to be
# enough for first boot.
SIZE_MB="${SIZE_MB:-6144}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOP="$(cd "$HERE/.." && pwd)"
# Scratch space for the root filesystem. Not /tmp: Debian 13's /tmp is RAM,
# shared by every session on a build host, and a root is gigabytes.
WORK_DIR="${WORK_DIR:-/var/tmp}"
# A clean tree of Dock360 (git archive of feature/ohs-edition, never a
# working tree) whose install.sh is run inside the image. ROLE=system only.
DOCK360_SRC="${DOCK360_SRC:-}"
# Signed .d360mod packages to preload into the store (see build.sh --apps).
APPS_DIR="${APPS_DIR:-}"
# Store apps installed at first boot, if their package was preloaded.
FIRST_APPS="${FIRST_APPS:-shack-qo100wb}"
# Where a new controller looks for updates (build.sh --feeds-base); preset at
# first boot by openhamshack-feeds-preset. Empty leaves the feeds unset.
FEEDS_BASE="${FEEDS_BASE-https://openhamshack.com/feeds/}"
# Optional per-site network: systemd-networkd *.network files and
# wpa_supplicant-<iface>.conf files, copied in as they are. Wi-Fi credentials
# live only in that directory, never here.
NETWORK_DIR="${NETWORK_DIR:-}"
# Optional authorized_keys for a `shack` login with sudo. Without it the
# controller has no shell login at all: root is locked and the web interface
# is how it is run.
SSH_KEYS="${SSH_KEYS:-}"
# ROLE=installer: where the ISO's payload directory appears once live-boot
# has mounted the medium (a link, so the image is not in the squashfs too).
INSTALLER_PAYLOAD_LINK="${INSTALLER_PAYLOAD_LINK:-/run/live/medium/openhamshack}"
EXTRA_PACKAGES="${EXTRA_PACKAGES:-}"
KEEP_STAGE="${KEEP_STAGE:-}"
# Labels differ between the installer and the controller: both mount by
# label, and an installed controller booted with the stick still in must
# not mount the stick's partitions as its own.
ROOT_LABEL="${ROOT_LABEL:-shackroot}"
EFI_LABEL="${EFI_LABEL:-SHACKEFI}"

case "$ROLE" in
  system) [[ -n "$DOCK360_SRC" && -f "$DOCK360_SRC/install.sh" ]] || { echo "ROLE=system needs DOCK360_SRC (a git archive of the edition)" >&2; exit 1; } ;;
  installer) [[ -n "$KEEP_STAGE" ]] || { echo "ROLE=installer needs KEEP_STAGE" >&2; exit 1; } ;;
  *) echo "ROLE must be system or installer" >&2; exit 1 ;;
esac
if [[ "$(id -u)" -ne 0 ]]; then
  echo "needs root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
# Check for the build tools rather than apt-get them: installing packages on
# a working host as a side effect of building an image is not this script's
# business. grub-install itself runs inside the image.
missing=""
for tool in debootstrap parted mkfs.vfat mkfs.ext4 rsync losetup; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
if [[ -n "$missing" ]]; then
  echo "missing:$missing (apt-get install debootstrap parted dosfstools e2fsprogs rsync)" >&2
  exit 1
fi

mkdir -p "$WORK_DIR"
STAGE="$(mktemp -d "$WORK_DIR/stage.XXXXXX")"
MNT="$(mktemp -d "$WORK_DIR/mnt.XXXXXX")"
LOOP=""
teardown() {
  for d in dev/pts dev proc sys run; do umount -l "$STAGE/$d" 2>/dev/null || true; done
  for d in dev/pts dev proc sys boot/efi; do umount -l "$MNT/$d" 2>/dev/null || true; done
  umount -l "$MNT" 2>/dev/null || true
  if [[ -n "$LOOP" ]]; then losetup -d "$LOOP" 2>/dev/null || true; fi
  rm -rf "$STAGE" "$MNT"
}
trap teardown EXIT

debootstrap --variant=minbase "$SUITE" "$STAGE" "$MIRROR"
# mktemp -d made STAGE 0700, and rsync -a "$STAGE/" later copies that mode
# onto the image's /. Every non-root service then fails at boot with
# 200/CHDIR while root logins look fine (paid for on the appliance).
chmod 755 "$STAGE"
cat > "$STAGE/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main contrib non-free non-free-firmware
deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware
EOF

# Package scripts expect /proc and /dev, and must not start daemons on the
# build host.
#
# /dev is a private one with only the ordinary nodes, not the host's bound
# in (as the appliance's script did). Package scripts run
# systemd-tmpfiles, which sets the owner of device nodes to groups as the
# chroot numbers them: through a bound /dev it re-owned the build host's
# own /dev/kvm to the chroot's kvm gid, which on ohs-dev is another group,
# and every QEMU boot test after the build failed with "Permission denied".
mount -t tmpfs -o mode=755 tmpfs "$STAGE/dev"
for node in null:1:3 zero:1:5 full:1:7 random:1:8 urandom:1:9 tty:5:0; do
  IFS=: read -r name major minor <<<"$node"
  mknod -m 666 "$STAGE/dev/$name" c "$major" "$minor"
done
mkdir -p "$STAGE/dev/pts" "$STAGE/dev/shm"
mount -t devpts -o newinstance,ptmxmode=0666,mode=620 devpts "$STAGE/dev/pts"
ln -s pts/ptmx "$STAGE/dev/ptmx"
ln -s /proc/self/fd "$STAGE/dev/fd"
for d in proc sys; do mount --bind "/$d" "$STAGE/$d"; done
mount -t tmpfs tmpfs "$STAGE/run"
printf '#!/bin/sh\nexit 101\n' > "$STAGE/usr/sbin/policy-rc.d"
chmod 755 "$STAGE/usr/sbin/policy-rc.d"

chroot "$STAGE" apt-get update
# Common to both: a kernel that boots most x64 PCs, with the firmware their
# graphics, Wi-Fi and audio commonly need.
chroot "$STAGE" apt-get install -y --no-install-recommends \
  linux-image-amd64 systemd systemd-sysv systemd-resolved udev dbus libpam-systemd \
  firmware-intel-graphics firmware-amd-graphics firmware-misc-nonfree firmware-iwlwifi firmware-realtek \
  firmware-sof-signed wireless-regdb \
  python3 ca-certificates iproute2 iputils-ping curl zstd efibootmgr usbutils \
  e2fsprogs fdisk util-linux kmod less

# The appliance's habit of no passwords, kept: root is locked, nothing logs in
# on the console, and the image carries no credential anyone could publish.
chroot "$STAGE" passwd -l root
printf 'openhamshack\n' > "$STAGE/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 openhamshack\n' > "$STAGE/etc/hosts"
install -D -m 644 "$HERE/logind-openhamshack.conf" "$STAGE/etc/systemd/logind.conf.d/50-openhamshack.conf"

if [[ "$ROLE" == "system" ]]; then
  chroot "$STAGE" apt-get install -y --no-install-recommends \
    grub-efi-amd64 grub-pc-bin intel-media-va-driver-non-free \
    openssh-server sudo wpasupplicant iw avahi-daemon avahi-utils libnss-mdns \
    unattended-upgrades dbus-user-session systemd-timesyncd

  # ---- Dock360, OpenHamShack edition (begin) ----
  # install.sh runs here, unmodified, exactly as on any Debian host: its
  # packages, Caddy's repository and the release's virtualenv all come over
  # the network now, so the controller needs none later. systemctl in a
  # chroot enables units and ignores start/restart, which is what is wanted:
  # nothing runs until the controller boots.
  #
  # What install.sh makes that must not ship: the first administrator (it
  # always creates one, with a printed password), the station screen's
  # device token, and the database both live in. All three are removed below
  # and remade on the controller: first-run setup (/setup) asks the owner for
  # the administrator, and openhamshack-firstboot mints the token, so no two
  # controllers share a credential and none is published with the ISO.
  # Caddy from Debian (trixie-backports, 2.11) before install.sh looks for
  # it. install.sh adds Caddy's own cloudsmith repository when caddy is
  # missing, and on 09/10/2026 that repository answered every request with
  # "402 Payment Required", failing the whole install. Backports is close to
  # the version cloudsmith served (Debian's trixie 2.6 predates directives
  # the edition's routes may use) and gets Debian's security updates.
  echo "deb $MIRROR $SUITE-backports main" > "$STAGE/etc/apt/sources.list.d/backports.list"
  chroot "$STAGE" apt-get update
  chroot "$STAGE" apt-get install -y --no-install-recommends -t "$SUITE-backports" caddy
  install -d -m 700 "$STAGE/root/dock360-src"
  cp -a "$DOCK360_SRC/." "$STAGE/root/dock360-src/"
  head -c 32 /dev/urandom | base64 > "$STAGE/root/throwaway-admin"
  chmod 600 "$STAGE/root/throwaway-admin"
  chroot "$STAGE" env EDITION=openhamshack INSTALL_CADDY=yes \
    ADMIN_PASSWORD_FILE=/root/throwaway-admin bash /root/dock360-src/install.sh
  rm -rf "$STAGE/root/dock360-src" "$STAGE/root/throwaway-admin"
  # loginctl cannot reach logind in a chroot (install.sh warns and goes on);
  # this file is all enable-linger writes, and without it no app starts at
  # boot.
  install -d "$STAGE/var/lib/systemd/linger"
  touch "$STAGE/var/lib/systemd/linger/dock360"
  # Remade per controller: see above. secrets/ holds whatever the CLI made
  # lazily (none expected, but a key made here would be on every controller).
  rm -f "$STAGE"/var/lib/dock360/dock360.db* "$STAGE/etc/openhamshack/local-screen.token"
  find "$STAGE/var/lib/dock360/secrets" "$STAGE/var/lib/dock360/tmp" -mindepth 1 -delete
  find "$STAGE/var/log/dock360" -mindepth 1 -delete
  # ---- Dock360, OpenHamShack edition (end) ----

  # ---- Preloaded store and first boot (begin) ----
  # Signed app packages with their images, uploaded into the store at first
  # boot (they need the controller's database, which is made there) and then
  # removed from here, since the catalogue keeps its own copy.
  install -d -m 755 "$STAGE/var/lib/openhamshack/preload"
  if [[ -n "$APPS_DIR" ]]; then
    for pkg in "$APPS_DIR"/*.d360mod; do
      [[ -e "$pkg" ]] && install -m 644 "$pkg" "$STAGE/var/lib/openhamshack/preload/"
    done
  fi
  printf 'FIRST_APPS="%s"\nFEEDS_BASE="%s"\n' "$FIRST_APPS" "$FEEDS_BASE" > "$STAGE/etc/openhamshack/firstboot.conf"
  install -m 755 "$HERE/openhamshack-feeds-preset" "$STAGE/usr/local/sbin/openhamshack-feeds-preset"
  install -m 755 "$HERE/openhamshack-firstboot" "$STAGE/usr/local/sbin/openhamshack-firstboot"
  install -m 644 "$HERE/openhamshack-firstboot.service" "$STAGE/etc/systemd/system/"
  install -m 755 "$HERE/openhamshack-podman-pause" "$STAGE/usr/local/sbin/openhamshack-podman-pause"
  install -m 644 "$HERE/openhamshack-podman-pause.service" "$STAGE/etc/systemd/system/"
  chroot "$STAGE" systemctl enable openhamshack-firstboot.service openhamshack-podman-pause.service
  # ---- Preloaded store and first boot (end) ----

  cat > "$STAGE/etc/systemd/network/20-wired.network" <<EOF
[Match]
Name=en* eth*

[Network]
DHCP=yes
# The controller is found as openhamshack.local; resolved must not answer
# mDNS itself or it and avahi fight over port 5353.
MulticastDNS=no
EOF
  install -d "$STAGE/etc/systemd/resolved.conf.d"
  printf '[Resolve]\nMulticastDNS=no\nLLMNR=no\n' > "$STAGE/etc/systemd/resolved.conf.d/50-openhamshack.conf"
  cat > "$STAGE/etc/motd" <<EOF

OpenHamShack controller. Open http://openhamshack.local/ (or port 8800 for
the administration pages) from any browser on this network.

EOF
  cat > "$STAGE/etc/issue" <<'EOF'
OpenHamShack controller \n - http://\4/ (administration on port 8800)

EOF
  cat > "$STAGE/etc/fstab" <<EOF
LABEL=$ROOT_LABEL / ext4 defaults 0 1
LABEL=$EFI_LABEL /boot/efi vfat umask=0077 0 1
EOF
  install -m 755 "$HERE/openhamshack-growroot" "$STAGE/usr/local/sbin/openhamshack-growroot"
  install -m 644 "$HERE/openhamshack-growroot.service" "$STAGE/etc/systemd/system/"
  # systemd-timesyncd: minbase has no NTP client, so the controller kept
  # whatever its RTC said - the first real PC (ThinkCentre M720q, 09/10/2026)
  # booted in April. Feed signatures, TOTP and logbook times all depend on
  # the clock. Offline controllers still keep the RTC's time; timesyncd's
  # clock file at least stops it going backwards across reboots.
  chroot "$STAGE" systemctl enable systemd-networkd systemd-resolved avahi-daemon ssh systemd-timesyncd \
    openhamshack-growroot.service
  if [[ -n "$NETWORK_DIR" ]]; then
    for f in "$NETWORK_DIR"/*.network; do
      [[ -e "$f" ]] && install -m 644 "$f" "$STAGE/etc/systemd/network/"
    done
    for f in "$NETWORK_DIR"/wpa_supplicant-*.conf; do
      [[ -e "$f" ]] || continue
      install -m 600 "$f" "$STAGE/etc/wpa_supplicant/"
      iface="$(basename "$f" .conf)"
      chroot "$STAGE" systemctl enable "wpa_supplicant@${iface#wpa_supplicant-}.service"
    done
  fi
  # Kernel messages and a login prompt on the first serial port as well as the
  # screen: harmless on a PC without one, and how a headless test (or an
  # owner with a USB-serial lead) sees what the controller is doing.
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"/' \
    "$STAGE/etc/default/grub"
  sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/' "$STAGE/etc/default/grub"
  install -d "$STAGE/etc/default/grub.d"
  printf 'GRUB_DISTRIBUTOR="OpenHamShack"\n' > "$STAGE/etc/default/grub.d/50-openhamshack.cfg"

  # SSH by key only, host keys made by each controller at its first start
  # (keys made here would be the same private keys on every controller).
  # Debian's own sshd-keygen only runs on a systemd "first boot", which an
  # image is not guaranteed to be; this runs before every sshd start and makes
  # only what is missing.
  install -d "$STAGE/etc/ssh/sshd_config.d"
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' \
    > "$STAGE/etc/ssh/sshd_config.d/10-openhamshack.conf"
  cat > "$STAGE/etc/systemd/system/openhamshack-hostkeys.service" <<'UNIT'
[Unit]
Description=Make this controller's own SSH host keys
Before=ssh.service ssh.socket

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=ssh.service ssh.socket
UNIT
  chroot "$STAGE" systemctl enable openhamshack-hostkeys.service
  rm -f "$STAGE"/etc/ssh/ssh_host_*
  if [[ -n "$SSH_KEYS" ]]; then
    chroot "$STAGE" useradd -m -s /bin/bash -G sudo shack
    chroot "$STAGE" passwd -l shack
    install -d -m 700 "$STAGE/home/shack/.ssh"
    install -m 600 "$SSH_KEYS" "$STAGE/home/shack/.ssh/authorized_keys"
    chroot "$STAGE" chown -R shack:shack /home/shack/.ssh
    echo "shack ALL=(ALL) NOPASSWD: ALL" > "$STAGE/etc/sudoers.d/shack-owner"
    chmod 440 "$STAGE/etc/sudoers.d/shack-owner"
  else
    # Nobody could log in without a key, and a listening sshd on every
    # controller is attack surface for nothing.
    chroot "$STAGE" systemctl disable ssh.service openhamshack-hostkeys.service
  fi
  # Debian security updates on Debian's own timers for now. The appliance's
  # never-on-air rule belongs to Dock360's busy gate (host upgrade), which
  # does not drive unattended-upgrades yet.
fi

if [[ "$ROLE" == "installer" ]]; then
  chroot "$STAGE" apt-get install -y --no-install-recommends \
    live-boot sway chromium fonts-dejavu-core
  # The kiosk session's user; no password, nothing to log in to.
  chroot "$STAGE" useradd -m -s /usr/sbin/nologin -G video,render,input shack
  chroot "$STAGE" passwd -l shack
  install -d "$STAGE/opt/openhamshack-installer" "$STAGE/etc/openhamshack"
  install -m 755 "$TOP/installer/installer.py" "$STAGE/opt/openhamshack-installer/installer.py"
  install -m 644 "$TOP/installer/index.html" "$STAGE/opt/openhamshack-installer/index.html"
  install -m 644 "$TOP/installer/sway.config" "$STAGE/etc/openhamshack/installer-sway.config"
  install -m 644 "$TOP/installer/openhamshack-installer.service" \
    "$TOP/installer/openhamshack-display.service" \
    "$TOP/installer/openhamshack-autoinstall.service" "$STAGE/etc/systemd/system/"
  ln -sfn "$INSTALLER_PAYLOAD_LINK" "$STAGE/opt/openhamshack-installer/payload"
  chroot "$STAGE" systemctl enable openhamshack-installer.service openhamshack-display.service \
    openhamshack-autoinstall.service
  # The disk-image fstab would name partitions a live system does not have;
  # a missing /boot/efi drops boot into emergency mode. live-boot mounts /.
  echo "# live system: the root is mounted by live-boot" > "$STAGE/etc/fstab"
fi

if [[ -n "$EXTRA_PACKAGES" ]]; then
  # shellcheck disable=SC2086
  chroot "$STAGE" apt-get install -y --no-install-recommends $EXTRA_PACKAGES
fi
chroot "$STAGE" apt-get clean
rm -rf "$STAGE/var/cache/apt/archives/"*.deb "$STAGE/var/lib/apt/lists/"*_dists_* "$STAGE/root/.cache"
rm -f "$STAGE/usr/sbin/policy-rc.d"

# ---- Package list (begin) ----
# Every Debian package with its exact version and source package, kept on
# the controller and beside the ISO, so a pointer to snapshot.debian.org for
# their source is exact.
chroot "$STAGE" dpkg-query -W -f='${Package} ${Version} ${source:Package} ${source:Version}\n' \
  | sort > "$STAGE/usr/share/doc/openhamshack-packages.txt"
cp "$STAGE/usr/share/doc/openhamshack-packages.txt" "${IMG%.img}.packages.txt"
# ---- Package list (end) ----
for d in dev/pts dev proc sys run; do umount -l "$STAGE/$d"; done
# resolv.conf becomes the resolved stub only now: during the build the chroot
# needed the host's resolver to reach the mirror.
ln -sf /run/systemd/resolve/stub-resolv.conf "$STAGE/etc/resolv.conf"
# Each controller its own machine-id, made at first boot: networkd derives the
# DHCP client identifier from it, so two controllers from one image would
# fight over one address. An empty file makes a "first boot", which also
# starts systemd-firstboot - waiting on the console for answers on a machine
# with no keyboard - so that is masked; the image already sets what it asks.
: > "$STAGE/etc/machine-id"
rm -f "$STAGE/var/lib/dbus/machine-id"
ln -s /etc/machine-id "$STAGE/var/lib/dbus/machine-id"
ln -sf /dev/null "$STAGE/etc/systemd/system/systemd-firstboot.service"

if [[ -n "$KEEP_STAGE" ]]; then
  rm -rf "$KEEP_STAGE"
  mkdir -p "$KEEP_STAGE"
  rsync -aHAX "$STAGE/" "$KEEP_STAGE/"
  echo "root filesystem kept in $KEEP_STAGE"
  exit 0
fi

# GPT with three partitions, so one image boots either firmware:
#   1  1 MiB  BIOS boot (EF02): where i386-pc GRUB puts its core image, since
#             GPT leaves no gap after the MBR the way MS-DOS tables did
#   2  256 MiB EFI system partition: \EFI\BOOT\BOOTX64.EFI (removable path,
#             so no NVRAM entry is needed and a moved disk still boots)
#   3  root, ext4, grown to the disk at first boot
rm -f "$IMG"
apps_mb=0
[[ -n "$APPS_DIR" ]] && apps_mb=$(( $(du -cm "$APPS_DIR"/*.d360mod 2>/dev/null | tail -1 | cut -f1) * 3 ))
truncate -s "$(( SIZE_MB + apps_mb ))M" "$IMG"
parted -s "$IMG" mklabel gpt
parted -s "$IMG" mkpart BIOS 1MiB 2MiB
parted -s "$IMG" set 1 bios_grub on
parted -s "$IMG" mkpart ESP fat32 2MiB 258MiB
parted -s "$IMG" set 2 esp on
parted -s "$IMG" mkpart ROOT ext4 258MiB 100%
LOOP="$(losetup --find --show --partscan "$IMG")"
udevadm settle 2>/dev/null || sleep 1
mkfs.vfat -F 32 -n "$EFI_LABEL" "${LOOP}p2"
mkfs.ext4 -q -F -L "$ROOT_LABEL" "${LOOP}p3"
mount "${LOOP}p3" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "${LOOP}p2" "$MNT/boot/efi"
# -HAX keeps hard links, ACLs and the file capabilities ping and
# newuidmap rely on.
rsync -aHAX "$STAGE/" "$MNT/"
for d in dev dev/pts proc sys; do mount --bind "/$d" "$MNT/$d"; done
chroot "$MNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --no-nvram
chroot "$MNT" grub-install --target=i386-pc --modules="part_gpt ext2" "$LOOP"
chroot "$MNT" update-grub
sync
echo "wrote $IMG"
