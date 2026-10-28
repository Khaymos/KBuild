#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: build-live-initramfs.sh must run as root (the ISO builder does this automatically)." >&2
    exit 1
fi

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROOTFS="$(realpath -e -- "${ROOTFS:-$TOP/rootfs}")"
INITRD="${INITRD:?Set INITRD to the output cpio.gz path}"
INITRD="$(realpath -m -- "$INITRD")"

for tool in bash cpio cp find grep gzip ls mkdir mknod readelf realpath sed; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required host tool missing: $tool" >&2
        exit 1
    }
done

[[ -d "$ROOTFS" ]] || {
    echo "ERROR: root filesystem directory is missing: $ROOTFS" >&2
    exit 1
}
[[ ! -e "$INITRD" ]] || {
    echo "ERROR: refusing to overwrite existing initramfs: $INITRD" >&2
    exit 1
}
[[ ! -e "$INITRD.new" ]] || {
    echo "ERROR: temporary initramfs output already exists: $INITRD.new" >&2
    exit 1
}

for path in /usr/bin/bash /usr/bin/mount /usr/bin/umount /usr/bin/mkdir /usr/bin/sleep /usr/sbin/switch_root; do
    [[ -e "$ROOTFS$path" ]] || {
        echo "ERROR: required target executable missing: $ROOTFS$path" >&2
        exit 1
    }
done

mkdir -p "$(dirname -- "$INITRD")"
mkdir -p "$TOP/build"
WORK="$(mktemp -d "$TOP/build/.live-initramfs.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
INITROOT="$WORK/root"
mkdir -p "$INITROOT"/{dev,proc,sys,run,sysroot,tmp,usr/bin,usr/sbin,usr/lib,lib}
ln -s usr/bin "$INITROOT/bin"
ln -s usr/sbin "$INITROOT/sbin"
ln -s usr/lib "$INITROOT/lib64"

# Copy a target executable and its real file, preserving symlink names. Every
# ELF dependency is then queued and copied recursively from this target rootfs.
declare -a ELF_QUEUE=()
declare -A ELF_QUEUED=()
declare -A ELF_SCANNED=()

copy_entry() {
    local root_path="$1"
    local source="$ROOTFS$root_path"
    local canonical relative destination

    [[ "$root_path" == /* ]] || {
        echo "ERROR: internal path must be absolute: $root_path" >&2
        return 1
    }
    [[ -e "$source" || -L "$source" ]] || {
        echo "ERROR: target runtime file is missing: $source" >&2
        return 1
    }

    canonical="$(realpath -e -- "$source")"
    [[ "$canonical" == "$ROOTFS/"* ]] || {
        echo "ERROR: target runtime path escapes the rootfs: $source -> $canonical" >&2
        return 1
    }
    relative="${canonical#"$ROOTFS"/}"

    mkdir -p "$INITROOT$(dirname -- "$root_path")"
    cp -a -- "$source" "$INITROOT$root_path"

    destination="$INITROOT/$relative"
    mkdir -p "$(dirname -- "$destination")"
    cp -a -- "$canonical" "$destination"

    if [[ -z "${ELF_QUEUED[$canonical]+set}" ]]; then
        ELF_QUEUED["$canonical"]=1
        ELF_QUEUE+=("$canonical")
    fi
}

find_library() {
    local name="$1" directory candidate
    for directory in /usr/lib /lib /usr/lib64 /lib64 /usr/local/lib; do
        candidate="$ROOTFS$directory/$name"
        if [[ -e "$candidate" || -L "$candidate" ]]; then
            printf '%s\n' "$directory/$name"
            return 0
        fi
    done
    return 1
}

for path in /usr/bin/bash /usr/bin/mount /usr/bin/umount /usr/bin/mkdir /usr/bin/sleep /usr/sbin/switch_root; do
    copy_entry "$path"
done

queue_index=0
while (( queue_index < ${#ELF_QUEUE[@]} )); do
    elf="${ELF_QUEUE[$queue_index]}"
    queue_index=$((queue_index + 1))
    [[ -z "${ELF_SCANNED[$elf]+set}" ]] || continue
    ELF_SCANNED["$elf"]=1

    if ! readelf -h "$elf" >/dev/null 2>&1; then
        continue
    fi

    interpreter="$(readelf -l "$elf" 2>/dev/null | sed -n 's/.*Requesting program interpreter: \(.*\)]/\1/p')"
    if [[ -n "$interpreter" ]]; then
        copy_entry "$interpreter"
    fi

    while IFS= read -r library; do
        [[ -n "$library" ]] || continue
        library_path="$(find_library "$library")" || {
            echo "ERROR: could not resolve target dependency '$library' required by $elf" >&2
            exit 1
        }
        copy_entry "$library_path"
    done < <(readelf -d "$elf" 2>/dev/null | sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p')
done

cat > "$INITROOT/init" <<'INIT'
#!/bin/bash
# Small live initramfs: find the ISO9660 medium, mount its SquashFS root, and
# switch into an OverlayFS root. Storage/filesystem drivers are built into the
# kernel so this early userspace does not need modprobe or a full /lib/modules.
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=linux

rescue_shell() {
    echo "KhaymOS live initramfs: ERROR: $*" >/dev/console 2>&1 || true
    echo "An emergency shell will remain available for diagnosis." >/dev/console 2>&1 || true
    while true; do
        /bin/bash
        echo "Emergency shell returned; keeping init alive." >/dev/console 2>&1 || true
        sleep 1
    done
}

mkdir -p /dev /proc /sys /run/live-media /run/live-lower /run/live-cow /sysroot
mount -t devtmpfs devtmpfs /dev || rescue_shell "cannot mount devtmpfs"
mkdir -p /dev/pts /dev/shm
mount -t proc proc /proc || rescue_shell "cannot mount proc"
mount -t sysfs sysfs /sys || rescue_shell "cannot mount sysfs"

# Wait up to 30 seconds for USB/SATA/optical devices to appear. The ISO must be
# written as a raw/hybrid image; a copied ISO file on a random FAT partition is
# intentionally not guessed here.
shopt -s nullglob
MEDIA_FOUND=0
for attempt in {1..30}; do
    for device in /dev/sr* /dev/sd* /dev/hd* /dev/vd* /dev/xvd* /dev/nvme*n* /dev/mmcblk*; do
        [[ -b "$device" ]] || continue
        if mount -t iso9660 -o ro "$device" /run/live-media >/dev/null 2>&1; then
            if [[ -s /run/live-media/live/khaymos-rootfs.squashfs ]]; then
                MEDIA_FOUND=1
                break 2
            fi
            umount /run/live-media >/dev/null 2>&1 || true
        fi
    done
    sleep 1
done
(( MEDIA_FOUND == 1 )) || rescue_shell "could not find an ISO9660 medium containing /live/khaymos-rootfs.squashfs"

mount -t tmpfs -o mode=0755,size=35% tmpfs /run/live-cow || rescue_shell "cannot create writable OverlayFS storage"
mkdir -p /run/live-cow/upper /run/live-cow/work
mount -t squashfs -o loop,ro /run/live-media/live/khaymos-rootfs.squashfs /run/live-lower ||
    rescue_shell "cannot mount live SquashFS root"

mount -t overlay overlay \
    -o lowerdir=/run/live-lower,upperdir=/run/live-cow/upper,workdir=/run/live-cow/work \
    /sysroot || rescue_shell "cannot create writable OverlayFS root"

mkdir -p /sysroot/dev /sysroot/proc /sysroot/sys /sysroot/run/live-media \
    /sysroot/run/live-lower /sysroot/run/live-cow /sysroot/tmp

# These mounts must survive switch_root. Keep the ISO, loop-backed SquashFS,
# and OverlayFS upper storage mounted below the new root's /run.
mount --move /run/live-media /sysroot/run/live-media || rescue_shell "cannot preserve live media mount"
mount --move /run/live-lower /sysroot/run/live-lower || rescue_shell "cannot preserve SquashFS lower mount"
mount --move /run/live-cow /sysroot/run/live-cow || rescue_shell "cannot preserve writable overlay storage"
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /sysroot/tmp || rescue_shell "cannot mount temporary filesystem"
mount --move /dev /sysroot/dev || rescue_shell "cannot move /dev into live root"
mount --move /proc /sysroot/proc || rescue_shell "cannot move /proc into live root"
mount --move /sys /sysroot/sys || rescue_shell "cannot move /sys into live root"

if [[ -r /sysroot/etc/hostname ]]; then
    # /proc now lives inside the new root and is the live kernel's procfs.
    live_hostname=khaymos
    IFS= read -r live_hostname < /sysroot/etc/hostname || true
    printf '%s\n' "$live_hostname" > /sysroot/proc/sys/kernel/hostname 2>/dev/null || true
fi

echo
echo "KhaymOS Linux 0.1 | SquashFS live root mounted; starting OpenRC..."
exec /usr/sbin/switch_root /sysroot /sbin/init
INIT
chmod 0755 "$INITROOT/init"

mknod -m 0600 "$INITROOT/dev/console" c 5 1
mknod -m 0666 "$INITROOT/dev/null" c 1 3
mknod -m 0666 "$INITROOT/dev/tty" c 5 0

INITRD_TMP="$INITRD.new"
(
    cd "$INITROOT"
    find . -print0 | cpio --null -o --format=newc | gzip -9 > "$INITRD_TMP"
)
gzip -t "$INITRD_TMP"
if ! gzip -dc "$INITRD_TMP" | cpio -it --quiet | grep -qx './init'; then
    echo "ERROR: generated initramfs does not contain /init" >&2
    exit 1
fi
mv -- "$INITRD_TMP" "$INITRD"

printf 'Built compact live initramfs: '
ls -lh "$INITRD"

echo "Included runtime executables: bash, mount, umount, mkdir, sleep, switch_root"
echo "Resolved ELF files: ${#ELF_SCANNED[@]}"
