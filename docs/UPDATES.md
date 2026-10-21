# Updating a controller in place

An installed OpenHamShack controller updates itself from two signed feeds:
one for the **platform** (Dock360 with the OpenHamShack edition) and one for
the **app store**. Nobody needs to reinstall from the ISO to pick up a fix,
as long as the fix is in the Dock360 release or in an app (see
[what an update cannot carry](#what-an-update-cannot-carry)).

The feed format and the station side are in the Dock360 edition
(`docs/openhamshack-feeds.md`, `src/dock360/shack/feeds.py`). This page
covers publishing them, where they are, and the limits.

## The addresses

### Today: the LAN mirror on ohs-dev (office LAN only)

On the controller, open **Station updates** (the OpenHamShack section of the
admin interface, `http://<controller>:8800/shack/updates`; it is not behind
"Show advanced pages") and enter:

| Field | Value |
|---|---|
| Platform feed | `http://192.0.2.140:8780/feeds/platform-{channel}.json` |
| App store feed | `http://192.0.2.140:8780/feeds/catalogue.json` |
| Channel | **Beta** |

Type `{channel}` literally, braces included: the controller replaces it with
its channel. **Save**, then **Check now**. The platform section then offers
the newest release above the controller's own version, with **Install**; the
app section lists the store's apps with **Update** where it applies.

Plain HTTP is fine: the feeds and every package are signed, by keys the
controller already trusts (the platform by the OpenHamShack master key, apps
by the modules key), and a package whose SHA-256 differs from its feed is
refused before anything is installed. Nothing installs while the station is
on the air or an app says it is busy.

These addresses only work on the office LAN (192.0.2.0/24). The M720q at
192.0.2.98 is on it.

### Later: the website

| Field | Value |
|---|---|
| Platform feed | `https://openhamshack.com/feeds/platform-{channel}.json` |
| App store feed | `https://openhamshack.com/feeds/catalogue.json` |
| Channel | Beta |

ISOs are built with these preset (`build.sh --feeds-base`, below), so a new
install needs no typing. Until the site is live such a controller shows
"Could not fetch the platform feed" on Station updates and is otherwise
unaffected. A controller that already has addresses (like the M720q with the
LAN ones) keeps them: change them on Station updates when the site is live.
Because the site's `feeds/` folder is the same tree as the LAN mirror's, the
next check after the switch just carries on - the sequence numbers continue.

## What a platform update does

**Install** on Station updates downloads the package, checks its SHA-256
against the feed and its signature against the controller's keys, and runs
Dock360's updater (the same one as an upload on the Releases page): it
installs the new release beside the running one (`/opt/dock360/releases/
<engine version>`, its own virtualenv; the pip step needs the internet for
anything not vendored), snapshots the database, runs migrations, copies
Dock360's own systemd units, switches, restarts and polls `/healthz`. If the
new release does not answer within two minutes it rolls back on its own.
Apps keep running in their containers throughout; the interface is away for
a few seconds.

**Rolling back by hand:** turn on **Settings > Show advanced pages**, open
**Releases**, and press **Switch to this** beside the previous version (each
release is listed by its engine version; the previous one stays on disk).
From a shell, `sudo dock360ctl release rollback <engine version> --wait`
does the same. Database migrations are not reversed by either.

**After installing 2.0.0-beta.3 on the M720q, do one more step** (until the
updater is fixed): open **Host status**, press **Rebuild units and proxy
config**, then restart QO-100 WB (its page in Apps, **Restart**). The update
publishes the app on the front door, but it does not rewrite the app's unit:
the updater runs `dock360ctl migrate` as root, where rewriting units fails
quietly. So the app never gets the `dock360-host` entry it needs to fetch the
station profile, and keeps saying it is waiting for the controller. The
rebuild runs as the service account and does rewrite it. Reported to the
edition's owner; once fixed, a later release makes this step unnecessary.

Tested on 09/10/2026 in QEMU:

- **beta.1 -> beta.2 and rollback** (the out4 ISO, the M720q's build):
  after a UEFI install, first boot installed QO-100 WB. With the LAN
  addresses above, Check now offered 2.0.0-beta.2. Install came back on
  2.0.0-beta.2 within about 15 s, with QO-100 WB still running. **Switch to
  this** went back to 2.0.0-beta.1 and then forward to beta.2 again, with
  the app running throughout.
- **beta.1 -> beta.3 with the preset** (an ISO built like out4 with
  `--feeds-base http://192.0.2.140:8780/feeds/`): first boot preset both
  addresses and the beta channel. After setup, GM4BRI was saved on the
  Station page. Check now offered 2.0.0-beta.3, and Install came back on it
  in 24 s. QO-100 WB went from the generic front-door page to the app itself
  at `/qo100wb/` on port 80. Its `/api/status` still had no profile (a DNS
  failure for `dock360-host`) until the rebuild and restart above. After
  those it showed GM4BRI, IO75RL, GB within 10 s.

## Publishing (on ohs-dev, where the keys are)

```
tools/publish-feeds.sh --notes "What changed, in a line"
```

Defaults: Dock360 `~/dock360` at `hub/feature/ohs-edition`, apps from
`~/build/apps/*.d360mod`, output `~/feeds/www`, channel beta, keys from
`~/.config/openhamshack/keys`. `--no-platform` or `--no-catalogue` publishes
one feed; `--out DIR` writes somewhere else (for the website, then copy
`DIR/feeds/` to the site's `feeds/` - this script never uploads).

It writes, under `<out>/feeds/`, exactly what the site will hold:

```
platform-beta.json  platform-beta.json.sig    (master key)
catalogue.json      catalogue.json.sig        (modules key)
packages/dock360-<engine>.d360upd             (signed, master key)
apps/<id>-<version>.d360mod                   (signed by package-app.sh)
```

and reads the result back with the controller's own feed reader against the
edition's public keys before it finishes. Feeds expire after 28 days, so
re-run it (with `--no-platform` if nothing changed) at least every four weeks
or controllers start saying the feed has expired.

### How the platform version moves

A controller is told the **platform** version (2.0.0-beta.N, the edition's
`PLATFORM_VERSION` file) and offered only releases above its own. Feature
commits never touch that file - two sessions bumping it per feature would set
it over each other, as `~/.claude/CLAUDE.md` explains for VERSION.

Each publish numbers itself: the newest of the ref's `PLATFORM_VERSION` and
every version already published (`ohs-platform/*` tags in `~/dock360`), with
its last number raised by one. `--platform-version` overrides. Edit
`PLATFORM_VERSION` on the branch only to start a new line (2.1.0-beta.1, or
2.0.0 itself).

The engine `VERSION` is restamped too, as `<patch + 1>-ohs.<UTC minute>`
(0.12.26-ohs.202610091259): Dock360's updater refuses to install the engine
version already running, so the edition republished at 0.12.25 could never be
applied. `tools/feedgen.py` explains the choice.

Both files are committed on top of the ref in a throwaway worktree and that
commit is tagged `ohs-platform/<version>` (local - push the tags when you push
the branch). The tag is what shipped. Before packaging, the script lists what
the ref adds since the previous publish and **stops if the ref lacks commits
the previous publish carried** (the 20 September failure: a release cut from
one branch silently dropped a fix that only lived on another), unless
`--allow-removal`.

`ohs-platform/2.0.0-beta.1` marks ac7d2b2, what the out4 ISO and the M720q
carry, as the first baseline.

### The mirror

A systemd user service on ohs-dev (lingering is on) serves `~/feeds/www`
read-only on `192.0.2.140:8780`: no listings, GET/HEAD only, no dotfiles.
`tools/feed-server.py` and `tools/openhamshack-feeds.service` (install steps
in the unit's header). ohs-dev's firewall accepts inbound by default (nftables,
only Tailscale's chain), so no rule was added.

```
systemctl --user status openhamshack-feeds
journalctl --user -u openhamshack-feeds -f
```

## Presetting new installs: `build.sh --feeds-base`

```
sudo ./build.sh ... --feeds-base https://openhamshack.com/feeds/   # the default
sudo ./build.sh ... --feeds-base http://192.0.2.140:8780/feeds/ # a LAN test build
sudo ./build.sh ... --feeds-base ""                                # leave them empty
```

First boot (`openhamshack-firstboot`) fills in `<base>platform-{channel}.json`,
`<base>catalogue.json` and channel beta, using the same settings the page
writes, each only if it has never been set, and only once (a marker in
`/var/lib/openhamshack/`), so an administrator's choice is never overwritten,
even after a power cut during first boot. The base is in `BUILDINFO`.

## What an update cannot carry

A platform update replaces the Dock360 release: `/opt/dock360/releases/<v>`
(the code, the edition, its templates, the trusted public keys, the vendored
wheels), Dock360's own systemd units (`dock360-*.service`/`.timer`), and the
database schema. The Caddy configuration and app units Dock360 generates at
run time follow it. An app update replaces the app. **Nothing else on the
disk changes.** In particular, none of these:

- **The installer and boot chain:** GRUB, its menu, the kernel command line,
  the partition layout.
- **Files the image writes into `/etc`:** `logind.conf.d` (the hibernate key
  and power button), the networkd files, `/etc/openhamshack/firstboot.conf`,
  `/etc/dock360/` as `install.sh` wrote it, the `/usr/local/bin/dock360ctl`
  wrapper.
- **The image's own units and scripts:** `openhamshack-firstboot`,
  `openhamshack-podman-pause`, `openhamshack-growroot`.
- **Packages not in the image.** **Host updates** (Debian's apt) upgrades the
  packages already installed, including the kernel and Caddy; it never adds
  one. A fix that needs a new package (as the NTP client did) needs a new
  image, or a way to carry it (below).
- **Users, groups and permissions** the image set up (the kiosk group, the
  render group).

### Today's fixes, sorted

| Fix | Where it lives | Reaches the M720q by |
|---|---|---|
| Installer Restart/Power off no longer hang once the stick is out (3aa7ebb) | installer, on the ISO | nothing to reach: it only matters while installing, so only new installs |
| NTP client (systemd-timesyncd, d7a49de) | image package | already there: the M720q runs openhamshack-os eaa8810, which has it |
| Hibernate key ignored, power button a clean power-off (logind, b62ce39) | image `/etc` | already there (eaa8810) |
| First boot survives a power cut (2011e39) | image script | already there; only ever matters at first boot |
| podman-pause unit | image unit | already there (since the first image) |
| Feeds preset at first boot (this change) | image script + firstboot.conf | never - new installs only; type the addresses once instead |
| Launcher menu clipping, window chrome, store window, Overview's store text | Dock360 edition | already there (ac7d2b2); a platform update for any other controller |
| Advanced pages off by default, root shell off by default | Dock360 edition | already there (ac7d2b2); elsewhere a platform update (defaults only: a setting an admin changed stays) |
| Clock check on Overview and apt | Dock360 edition | already there (ac7d2b2) |
| Browser transmit identity (person + device) | Dock360 edition | already there (ac7d2b2) |
| ProtectKernelTunables removed from dock360-api.service | Dock360's own systemd unit | already there (ac7d2b2); elsewhere a platform update (the updater copies Dock360's units) |
| Station window and first-run station step; apps on the front door; the `dock360-host` entry for the profile (f75062c) | Dock360 edition | 2.0.0-beta.3, plus the rebuild step above for the profile |
| QO-100 WB app fixes | the app package | the app store feed |

The M720q was installed from the out4 ISO (Dock360 ac7d2b2, openhamshack-os
eaa8810), so it already has every fix above that is done. What it is still
waiting for (the station profile reaching the app, the app on the front door,
the QO-100 WB app changes) is release-level or app-level: no reinstall needed.

The first publish, **2.0.0-beta.2, is ac7d2b2 itself** at a higher version,
so it changes nothing on the M720q. **2.0.0-beta.3 (f75062c) is the one it
needs**: the Station window, apps on the front door, and the profile fix
(see the rebuild step above).

### Could a platform update carry small image-level changes?

Yes, and it would be worth having before the beta ships widely - but it is
not trivial, and it belongs to Dock360's updater (the edition branch), not to
this repo, so it is a recommendation rather than built here.

The updater already runs as root and already runs code from the signed
release (it migrates the database and copies units before switching), so a
step that runs the release's **host fixups** does not widen what a master-key
signature can do. The shape:

- the edition ships `editions/openhamshack/host-fixups/NNN-<name>` scripts;
- the updater runs each one not yet recorded in
  `/var/lib/dock360/host-fixups.done`, as root, after migrations and before
  the switch, and a failure fails the update (which then rolls back);
- each fixup is idempotent and only adds or edits (a `logind.conf.d` drop-in,
  a unit, a one-line `/etc` change) - rollback does not undo it, so it must be
  harmless to the previous release too.

For anything larger - new Debian packages, the image's own units and
scripts - the cleaner long-term answer is an `openhamshack-system` .deb
installed in the image, carrying those files and depending on any new
packages, served from a signed apt repository beside the feeds. **Host
updates** would then carry image-level changes the way Debian carries its
own, with apt's own signatures and Depends doing the work. GRUB, the kernel
command line and the partition layout stay image-only either way.
