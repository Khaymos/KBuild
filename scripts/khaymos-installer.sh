#!/bin/bash
set -Eeuo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin

fail() {
    printf 'KhaymOS installer: ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ $EUID -ne 0 ]]; then
    fail "run as root from the live KhaymOS session"
fi
if [[ ! -t 0 ]]; then
    fail "an interactive terminal is required"
fi

for tool in lsblk sfdisk blockdev mkfs.ext4 blkid mount umount mountpoint tar chroot passwd grub-install find; do
    command -v "$tool" >/dev/null 2>&1 || fail "required command is missing: $tool"
done

BOOT_MODE=BIOS
if [[ -d /sys/firmware/efi ]]; then
    BOOT_MODE=UEFI
    command -v mkfs.fat >/dev/null 2>&1 || fail "UEFI installation requires mkfs.fat from dosfstools"
    [[ -r /usr/lib/grub/x86_64-efi/modinfo.sh ]] ||
        fail "GRUB x86_64-efi modules are missing under /usr/lib/grub/x86_64-efi"
else
    [[ -r /usr/lib/grub/i386-pc/boot.img ]] ||
        fail "GRUB BIOS modules are missing under /usr/lib/grub/i386-pc"
fi

readonly MNT=/mnt/khaymos-installer

cleanup() {
    local path
    set +e
    # Unmount nested bind mounts and the ESP before the installed root.
    for path in "$MNT/run" "$MNT/sys" "$MNT/proc" "$MNT/dev/pts" "$MNT/dev" "$MNT/boot/efi" "$MNT"; do
        mountpoint -q "$path" && umount "$path"
    done
}
trap cleanup EXIT

cat <<BANNER
KhaymOS Installer v2
====================
Detected firmware mode: $BOOT_MODE
BIOS layout: MBR partition table with one ext4 root partition.
UEFI layout: GPT, 512 MiB FAT32 EFI System Partition, and ext4 root.
The selected disk and all its existing partitions will be erased.

UEFI installs use the standard removable-media fallback loader path and do
not write firmware NVRAM entries. Secure Boot is not supported by this build
and must be disabled in firmware.

Review the disk model and size carefully before continuing.
BANNER

echo
echo "Available disks:"
lsblk -d -o NAME,SIZE,MODEL,TRAN,TYPE

echo
read -r -p 'Enter the FULL disk path to erase (for example /dev/vda): ' DISK
[[ "$DISK" == /dev/* && -b "$DISK" ]] ||
    fail "not a block device: $DISK"

DISK_TYPE="$(lsblk -dnro TYPE -- "$DISK" | head -n 1)"
[[ "$DISK_TYPE" == disk ]] ||
    fail "select a whole disk whose lsblk type is 'disk', not a partition or optical drive"

case "$DISK" in
    /dev/loop*|/dev/sr*|/dev/ram*|/dev/zram*)
        fail "refusing this device type: $DISK"
        ;;
esac

SIZE_BYTES="$(lsblk -bndo SIZE -- "$DISK" | head -n 1)"
[[ "$SIZE_BYTES" =~ ^[0-9]+$ ]] || fail "could not determine disk size"
MIN_BYTES=$((8 * 1024 * 1024 * 1024))
(( SIZE_BYTES >= MIN_BYTES )) ||
    fail "disk must be at least 8 GiB; detected $((SIZE_BYTES / 1024 / 1024 / 1024)) GiB"

if lsblk -nrpo MOUNTPOINTS -- "$DISK" | grep '[^[:space:]]' >/dev/null; then
    fail "one or more partitions on $DISK are mounted or in use; refusing to erase it"
fi

if mountpoint -q "$MNT"; then
    fail "$MNT is already a mountpoint"
fi
if [[ -d "$MNT" ]] && [[ -n "$(find "$MNT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "$MNT already contains files; inspect it before retrying"
fi

echo
echo "SELECTED DISK: $DISK"
echo "SIZE:          $((SIZE_BYTES / 1024 / 1024 / 1024)) GiB"
echo "FIRMWARE MODE: $BOOT_MODE"
echo "WARNING: every partition and all data on this disk will be destroyed."
echo "Current partition table, if any:"
sfdisk --dump "$DISK" 2>/dev/null || true
echo
read -r -p "To permanently erase this disk, type exactly: ERASE $DISK : " CONFIRM
[[ "$CONFIRM" == "ERASE $DISK" ]] || fail "confirmation did not match; no changes made"

ROOT_PART_NUMBER=1
ESP_PART_NUMBER=
PARTITION_MODULE=part_msdos

echo
if [[ "$BOOT_MODE" == UEFI ]]; then
    echo "=== Creating GPT partition table for UEFI ==="
    DISK_SECTORS="$(blockdev --getsz "$DISK")"
    [[ "$DISK_SECTORS" =~ ^[0-9]+$ ]] || fail "could not determine disk sector count"

    EFI_START_SECTOR=2048
    EFI_SIZE_SECTORS=$((512 * 1024 * 1024 / 512))
    ROOT_START_SECTOR=$((EFI_START_SECTOR + EFI_SIZE_SECTORS))
    (( DISK_SECTORS > ROOT_START_SECTOR + 2048 )) ||
        fail "disk is too small for the EFI System Partition and root partition"

    sfdisk --wipe always --wipe-partitions always "$DISK" <<PARTITIONS
label: gpt
unit: sectors
first-lba: 2048
start=$EFI_START_SECTOR, size=$EFI_SIZE_SECTORS, type=U, name="EFI System"
start=$ROOT_START_SECTOR, type=L, name="KhaymOS Root"
PARTITIONS

    ESP_PART_NUMBER=1
    ROOT_PART_NUMBER=2
    PARTITION_MODULE=part_gpt
else
    echo "=== Creating legacy BIOS/MBR partition table ==="
    sfdisk --wipe always --wipe-partitions always "$DISK" <<'PARTITIONS'
label: dos
unit: sectors

start=2048, type=83, bootable
PARTITIONS
fi

if [[ "$DISK" =~ [0-9]$ ]]; then
    PART="${DISK}p${ROOT_PART_NUMBER}"
    if [[ -n "$ESP_PART_NUMBER" ]]; then
        ESP_PART="${DISK}p${ESP_PART_NUMBER}"
    else
        ESP_PART=
    fi
else
    PART="${DISK}${ROOT_PART_NUMBER}"
    if [[ -n "$ESP_PART_NUMBER" ]]; then
        ESP_PART="${DISK}${ESP_PART_NUMBER}"
    else
        ESP_PART=
    fi
fi

if command -v partx >/dev/null 2>&1; then
    partx -u "$DISK" || true
fi
if command -v udevadm >/dev/null 2>&1; then
    udevadm settle || true
fi

for attempt in {1..10}; do
    [[ -b "$PART" ]] && break
    sleep 1
done
[[ -b "$PART" ]] || fail "root partition device did not appear: $PART"
if [[ "$BOOT_MODE" == UEFI ]]; then
    [[ -b "$ESP_PART" ]] || fail "EFI System Partition device did not appear: $ESP_PART"
fi

echo
echo "=== Formatting root partition ==="
mkfs.ext4 -F -m 1 -L KHAYMOS_ROOT "$PART"

ESP_UUID=
if [[ "$BOOT_MODE" == UEFI ]]; then
    echo
    echo "=== Formatting EFI System Partition ==="
    mkfs.fat -F 32 -n KHAYMOS_EFI "$ESP_PART"
    ESP_UUID="$(blkid -s UUID -o value "$ESP_PART")"
    [[ "$ESP_UUID" =~ ^[[:xdigit:]-]+$ ]] || fail "could not obtain the EFI partition UUID"
fi

mkdir -p "$MNT"
mount "$PART" "$MNT"

echo
echo "=== Copying the live KhaymOS system to disk ==="
tar --numeric-owner --one-file-system \
    --exclude='./dev' \
    --exclude='./proc' \
    --exclude='./sys' \
    --exclude='./run' \
    --exclude='./tmp' \
    --exclude='./mnt' \
    --exclude='./media' \
    -cpf - -C / . |
    tar --numeric-owner -xpf - -C "$MNT"

mkdir -p "$MNT/dev/pts" "$MNT/dev/shm" "$MNT/proc" "$MNT/sys" \
    "$MNT/run" "$MNT/tmp" "$MNT/mnt" "$MNT/media" "$MNT/boot/grub" "$MNT/etc"

if [[ "$BOOT_MODE" == UEFI ]]; then
    mkdir -p "$MNT/boot/efi"
    mount "$ESP_PART" "$MNT/boot/efi"
fi

echo
read -r -p 'Hostname for the installed system [khaymos]: ' NEW_HOSTNAME
if [[ -z "$NEW_HOSTNAME" ]]; then
    NEW_HOSTNAME=khaymos
fi
[[ "$NEW_HOSTNAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] ||
    fail "invalid hostname: $NEW_HOSTNAME"
[[ "$NEW_HOSTNAME" != *- && "$NEW_HOSTNAME" != *. ]] ||
    fail "hostname may not end in a dot or hyphen"
printf '%s\n' "$NEW_HOSTNAME" > "$MNT/etc/hostname"

ROOT_UUID="$(blkid -s UUID -o value "$PART")"
[[ "$ROOT_UUID" =~ ^[[:xdigit:]-]+$ ]] || fail "could not obtain the root filesystem UUID"
ROOT_PARTUUID="$(blkid -s PARTUUID -o value "$PART")"
[[ "$ROOT_PARTUUID" =~ ^[[:xdigit:]-]+$ ]] ||
    fail "could not obtain a partition UUID for the root partition"

if [[ "$BOOT_MODE" == UEFI ]]; then
    cat > "$MNT/etc/fstab" <<FSTAB
# KhaymOS Installer v2 (UEFI/GPT)
UUID=$ROOT_UUID / ext4 defaults,noatime 0 1
UUID=$ESP_UUID /boot/efi vfat defaults,umask=0077 0 2
FSTAB
else
    cat > "$MNT/etc/fstab" <<FSTAB
# KhaymOS Installer v2 (legacy BIOS/MBR)
UUID=$ROOT_UUID / ext4 defaults,noatime 0 1
FSTAB
fi

# Direct-root boot has no initramfs, so mount pseudo-filesystems before OpenRC.
cat > "$MNT/sbin/khaymos-init" <<'KHAYMOS_INIT'
#!/bin/bash
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=linux

mkdir -p /dev /proc /sys /run /tmp

if ! mountpoint -q /dev; then
    mount -t devtmpfs devtmpfs /dev || echo "[WARN] Could not mount devtmpfs"
fi
mkdir -p /dev/pts /dev/shm
if ! mountpoint -q /dev/pts; then
    mount -t devpts devpts /dev/pts || echo "[WARN] Could not mount devpts"
fi
if ! mountpoint -q /proc; then
    mount -t proc proc /proc || echo "[WARN] Could not mount proc"
fi
if ! mountpoint -q /sys; then
    mount -t sysfs sysfs /sys || echo "[WARN] Could not mount sysfs"
fi
if ! mountpoint -q /run; then
    mount -t tmpfs -o nosuid,nodev,mode=0755 tmpfs /run ||
        echo "[WARN] Could not mount /run tmpfs"
fi
if ! mountpoint -q /tmp; then
    mount -t tmpfs -o nosuid,nodev,mode=1777 tmpfs /tmp ||
        echo "[WARN] Could not mount /tmp tmpfs"
fi

if [ -r /etc/hostname ]; then
    cat /etc/hostname > /proc/sys/kernel/hostname ||
        echo "[WARN] Could not set hostname"
fi

echo
echo "KhaymOS Linux 0.1 | Starting OpenRC from installed root..."
exec /sbin/init
KHAYMOS_INIT
chmod 0755 "$MNT/sbin/khaymos-init"

# Each installed machine gets its own D-Bus machine ID, not the live ISO's ID.
MACHINE_ID="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$MACHINE_ID" =~ ^[[:xdigit:]]{32}$ ]] || fail "could not generate a machine ID"
printf '%s\n' "$MACHINE_ID" > "$MNT/etc/machine-id"
chmod 0444 "$MNT/etc/machine-id"
ln -sfn /etc/machine-id "$MNT/var/lib/dbus/machine-id"

echo
echo "=== Set the installed root password ==="
chroot "$MNT" /usr/bin/passwd root

echo
echo "=== Preparing chroot for GRUB installation ==="
for path in dev dev/pts proc sys run; do
    mount --bind "/$path" "$MNT/$path"
done

if [[ "$BOOT_MODE" == UEFI ]]; then
    echo
    echo "=== Installing GRUB for x86_64 UEFI ==="
    chroot "$MNT" /usr/sbin/grub-install \
        --target=x86_64-efi \
        --efi-directory=/boot/efi \
        --boot-directory=/boot \
        --removable \
        --no-nvram \
        --recheck

    test -s "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" ||
        fail "GRUB did not create the UEFI removable-media fallback loader"
else
    echo
    echo "=== Installing GRUB for legacy BIOS ==="
    chroot "$MNT" /usr/sbin/grub-install --target=i386-pc --recheck "$DISK"
fi

cat > "$MNT/boot/grub/grub.cfg" <<GRUB
set default=0
set timeout=5

insmod $PARTITION_MODULE
insmod ext2
insmod search_fs_uuid
search --no-floppy --fs-uuid --set=root $ROOT_UUID

menuentry 'KhaymOS Linux 0.1' {
    linux /boot/vmlinuz-khaymos-7.2.10 root=PARTUUID=$ROOT_PARTUUID rootfstype=ext4 rootwait rw init=/sbin/khaymos-init
}
GRUB

test -s "$MNT/boot/vmlinuz-khaymos-7.2.10" ||
    fail "installed kernel is missing"
test -s "$MNT/boot/grub/grub.cfg" ||
    fail "GRUB configuration was not written"

if [[ "$BOOT_MODE" == UEFI ]]; then
    test -s "$MNT/boot/efi/EFI/KhaymOS/grubx64.efi" ||
        fail "GRUB UEFI loader is missing"
    test -s "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" ||
        fail "GRUB UEFI fallback loader is missing"
else
    test -s "$MNT/boot/grub/i386-pc/core.img" ||
        fail "GRUB BIOS core image was not installed"
fi

echo
echo "=== Installation completed ==="
echo "Disk:          $DISK"
echo "Firmware mode: $BOOT_MODE"
echo "Root:          $PART"
echo "Root UUID:     $ROOT_UUID"
echo "Root PARTUUID: $ROOT_PARTUUID"
if [[ "$BOOT_MODE" == UEFI ]]; then
    echo "EFI partition: $ESP_PART"
    echo "EFI UUID:      $ESP_UUID"
    echo "EFI fallback:  EFI/BOOT/BOOTX64.EFI"
    echo "NVRAM entry:   not modified (fallback boot path installed)"
fi
echo "Hostname:      $NEW_HOSTNAME"
echo
echo "Power off or shut down the VM, detach the live ISO, and boot from the disk."
echo "The installation disk was not rebooted automatically."
