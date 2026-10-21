# Installing OpenHamShack

OpenHamShack runs on any 64-bit Intel or AMD PC: a mini PC such as an Intel
N100/N150 box is plenty. The installer **erases the disk you choose**, so use
a PC (or a disk) you can give to it.

You need:

* the ISO file, `openhamshack-<version>-amd64.iso`, and its `.sha256`;
* a USB stick of 8 GB or more, or a blank DVD;
* a screen with a mouse or touchscreen for the installer (a keyboard is not
  needed);
* a network cable to the PC (Wi-Fi can be set up afterwards).

## 1. Check the download

Linux or macOS, in the folder holding both files:

```
sha256sum -c openhamshack-<version>-amd64.iso.sha256
```

Windows (PowerShell):

```
Get-FileHash .\openhamshack-<version>-amd64.iso -Algorithm SHA256
```

The value must match the one in the `.sha256` file. If it does not,
download it again.

## 2. Make the USB stick or DVD

Pick one.

### Rufus (Windows)

1. Open Rufus and choose your USB stick under **Device**.
2. **SELECT** the ISO.
3. When Rufus asks how to write it, choose **Write in DD Image mode**. (ISO
   mode also works for UEFI PCs, but DD mode is the one that boots both
   kinds of PC.)
4. **START**. Everything on the stick is erased.

### Balena Etcher (Windows, macOS, Linux)

Flash from file, choose the ISO, choose the stick, Flash.

### dd (Linux)

Find the stick's name with `lsblk` (for example `/dev/sdb`; make sure it is
the stick, not your own disk), then:

```
sudo dd if=openhamshack-<version>-amd64.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

### Ventoy

Copy the ISO onto a Ventoy stick like any other file. At Ventoy's menu,
choose it, then **Boot in normal mode**.

### DVD

Burn the ISO as a disc image ("Burn disc image" in Windows, or `xorriso` /
Brasero on Linux). Do not copy it as a file.

## 3. Start the PC from it

1. Plug in the stick (or insert the DVD) and switch the PC on.
2. Open the boot menu. The key depends on the maker; it is usually **F12**,
   **F11**, **F10**, **F8** or **Esc**, shown briefly as the PC starts.
3. Choose the USB stick or DVD. If it is listed twice, either works: one
   is UEFI and one is legacy BIOS (CSM). UEFI is preferred.

**Secure Boot must be off.** The beta's boot loader is not signed by
Microsoft, and a PC with Secure Boot on refuses it (often with a "Secure
Boot violation" message or by skipping the stick). Turn it off in the
firmware setup (often **Del** or **F2** at power-on), under Boot or
Security, then try again.

Both firmware types work: modern UEFI PCs and older ones with legacy BIOS
only.

## 4. Install

The OpenHamShack menu appears for five seconds and starts **Install
OpenHamShack** by itself. If the screen stays blank, start again and pick
**Install OpenHamShack (safe graphics)**.

1. Tap the disk to install on. The USB stick or DVD you started from is
   never offered, nor are other USB drives.
2. Tap **Install**, then tap it again to confirm. **Everything on that disk
   is erased.**
3. The installer checks the image, writes it, and reads it back to make
   sure the disk holds exactly what was written. This takes a few minutes.
4. When it says it is installed, remove the USB stick or DVD and tap
   **Restart**.

Ignore the last menu entry, **Unattended install to /dev/vda**: it is for
automatic testing in virtual machines and does nothing on a real PC.

## 5. First start

The first start takes a few minutes longer than later ones: OpenHamShack
makes this controller's own identity and sets up its store, and installs
the QO-100 WB app. No internet connection is needed for any of it.

Then, from a phone, tablet or computer on the same network, open:

* **http://openhamshack.local/** - the station, for everyone you let in; or
* **http://openhamshack.local:8800/** - administration.

If `openhamshack.local` does not open (some Android versions and Windows
networks do not resolve `.local` names), use the address shown on the
controller's own screen or your router's list of devices.

The first person to open the administration page creates the
administrator account, so do this straight away, on your own network.
Choose a strong password; there is no default one.

## Afterwards

* **Apps**: more apps are in the store (Store, in the administration
  pages). They install without an internet connection.
* **Updates**: the controller checks for signed platform and app updates
  once you set the update addresses on Station updates.
* **Reinstalling**: start from the stick again. The disk is erased, so
  back up the station first.
