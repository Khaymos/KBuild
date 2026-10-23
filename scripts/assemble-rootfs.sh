#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

TOP="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SYSROOT="$TOP/sysroot"
ROOTFS="${ROOTFS:-$TOP/rootfs}"

if [[ ! -d "$SYSROOT/usr/bin" ]]; then
    echo "ERROR: KBuild sysroot is missing." >&2
    exit 1
fi

if [[ -e "$ROOTFS" ]]; then
    echo "ERROR: $ROOTFS already exists; refusing to overwrite it." >&2
    echo "Inspect or rename that directory before starting a fresh assembly." >&2
    exit 1
fi

for path in \
    "$SYSROOT/usr/bin/bash" \
    "$SYSROOT/usr/bin/false" \
    "$SYSROOT/usr/bin/passwd" \
    "$SYSROOT/usr/bin/login" \
    "$SYSROOT/usr/sbin/openrc-init" \
    "$SYSROOT/usr/sbin/openrc-run" \
    "$SYSROOT/usr/sbin/agetty" \
    "$SYSROOT/usr/lib/ld-linux-x86-64.so.2" \
    "$SYSROOT/boot/vmlinuz-khaymos-7.2.10" \
    "$SYSROOT/lib/modules/7.2.10"
do
    if [[ ! -e "$path" ]]; then
        echo "ERROR: required deployed path is missing: $path" >&2
        exit 1
    fi
done

echo "=== Copying deployed packages ==="
mkdir -p "$ROOTFS"
cp -a --no-preserve=ownership "$SYSROOT/." "$ROOTFS/"

# KBuild bookkeeping belongs to the development sysroot, not the OS.
rm -f "$ROOTFS/.kbuild-ownership.json" "$ROOTFS/.khaymos-target"

echo
echo "=== Preparing runtime directories ==="
for directory in dev proc sys run; do
    mkdir -p "$ROOTFS/$directory"
    find "$ROOTFS/$directory" -mindepth 1 -maxdepth 1 \
        -exec rm -rf -- {} +
done

mkdir -p \
    "$ROOTFS/usr/bin" \
    "$ROOTFS/usr/sbin" \
    "$ROOTFS/usr/lib" \
    "$ROOTFS/etc" \
    "$ROOTFS/etc/knpm" \
    "$ROOTFS/etc/conf.d" \
    "$ROOTFS/etc/init.d" \
    "$ROOTFS/etc/runlevels/default" \
    "$ROOTFS/dev/pts" \
    "$ROOTFS/dev/shm" \
    "$ROOTFS/home" \
    "$ROOTFS/root" \
    "$ROOTFS/run" \
    "$ROOTFS/run/dbus" \
    "$ROOTFS/run/dhcpcd" \
    "$ROOTFS/tmp" \
    "$ROOTFS/var/empty" \
    "$ROOTFS/var/lib/dhcpcd" \
    "$ROOTFS/var/lib/dbus" \
    "$ROOTFS/var/lib/elogind" \
    "$ROOTFS/var/lib/polkit-1" \
    "$ROOTFS/var/log" \
    "$ROOTFS/var/mail" \
    "$ROOTFS/var/spool" \
    "$ROOTFS/var/tmp" \
    "$ROOTFS/var/cache/rc"

echo
echo "=== Creating standard command paths ==="

# /bin is empty in the development sysroot.
if [[ -d "$ROOTFS/bin" && ! -L "$ROOTFS/bin" ]]; then
    rmdir "$ROOTFS/bin"
fi
ln -sfn usr/bin "$ROOTFS/bin"

# Preserve /sbin and /lib: they already contain files and kernel modules.
# Provide the conventional loader path expected by x86-64 executables.
if [[ -e "$ROOTFS/lib64" || -L "$ROOTFS/lib64" ]]; then
    echo "ERROR: unexpected pre-existing rootfs/lib64; inspect it." >&2
    exit 1
fi
ln -s usr/lib "$ROOTFS/lib64"

if [[ ! -e "$ROOTFS/usr/bin/sh" && ! -L "$ROOTFS/usr/bin/sh" ]]; then
    ln -s bash "$ROOTFS/usr/bin/sh"
fi

# Keep the existing /sbin/ldconfig and /sbin/sln files intact.
link_if_missing() {
    local path="$1"
    local target="$2"
    if [[ ! -e "$path" && ! -L "$path" ]]; then
        ln -s "$target" "$path"
    fi
}

link_if_missing "$ROOTFS/sbin/init" "../usr/sbin/openrc-init"
link_if_missing "$ROOTFS/sbin/openrc-run" "../usr/sbin/openrc-run"
link_if_missing "$ROOTFS/sbin/agetty" "../usr/sbin/agetty"

for tool in modprobe depmod; do
    if [[ -x "$ROOTFS/usr/bin/$tool" ]]; then
        link_if_missing "$ROOTFS/sbin/$tool" "../usr/bin/$tool"
    fi
done

echo
echo "=== Creating system account databases ==="

for file in passwd group shadow gshadow; do
    if [[ -e "$ROOTFS/etc/$file" || -L "$ROOTFS/etc/$file" ]]; then
        echo "ERROR: refusing to overwrite existing /etc/$file" >&2
        exit 1
    fi
done

cat > "$ROOTFS/etc/passwd" <<'PASSWD'
root:x:0:0:root:/root:/bin/bash
bin:x:1:1:bin:/dev/null:/usr/bin/false
daemon:x:2:2:daemon:/sbin:/usr/bin/false
messagebus:x:81:81:D-Bus system message bus:/run/dbus:/usr/bin/false
dhcpcd:x:68:68:DHCP client privilege separation:/var/lib/dhcpcd:/usr/bin/false
polkitd:x:102:102:PolicyKit daemon:/var/lib/polkit-1:/usr/bin/false
nobody:x:65534:65534:nobody:/var/empty:/usr/bin/false
PASSWD

cat > "$ROOTFS/etc/group" <<'GROUP'
root:x:0:
bin:x:1:
daemon:x:2:
sys:x:3:
adm:x:4:
tty:x:5:
disk:x:6:
lp:x:7:
kmem:x:9:
wheel:x:10:root
floppy:x:11:
mail:x:12:
news:x:13:
uucp:x:14:
man:x:15:
dialout:x:20:
tape:x:26:
video:x:27:
shadow:x:42:
utmp:x:43:
audio:x:63:
input:x:97:
messagebus:x:81:
dhcpcd:x:68:
polkitd:x:102:
users:x:1000:
nogroup:x:65534:
GROUP

cat > "$ROOTFS/etc/shadow" <<'SHADOW'
root:!:20000:0:99999:7:::
bin:!:20000:0:99999:7:::
daemon:!:20000:0:99999:7:::
messagebus:!:20000:0:99999:7:::
dhcpcd:!:20000:0:99999:7:::
polkitd:!:20000:0:99999:7:::
nobody:!:20000:0:99999:7:::
SHADOW

cat > "$ROOTFS/etc/gshadow" <<'GSHADOW'
root:!::
bin:!::
daemon:!::
sys:!::
adm:!::
tty:!::
disk:!::
lp:!::
kmem:!::
wheel:!::root
floppy:!::
mail:!::
news:!::
uucp:!::
man:!::
dialout:!::
tape:!::
video:!::
shadow:!::
utmp:!::
audio:!::
input:!::
messagebus:!::
dhcpcd:!::
polkitd:!::
users:!::
nogroup:!::
GSHADOW

chmod 0644 "$ROOTFS/etc/passwd" "$ROOTFS/etc/group"
chmod 0640 "$ROOTFS/etc/shadow" "$ROOTFS/etc/gshadow"
chown root:42 "$ROOTFS/etc/shadow" "$ROOTFS/etc/gshadow"

cat > "$ROOTFS/etc/hostname" <<'HOSTNAME'
khaymos
HOSTNAME

cat > "$ROOTFS/etc/hosts" <<'HOSTS'
127.0.0.1 localhost khaymos
::1 localhost ip6-localhost ip6-loopback khaymos
HOSTS

cat > "$ROOTFS/etc/nsswitch.conf" <<'NSSWITCH'
passwd: files
group:  files
shadow: files
hosts:  files dns
networks: files
protocols: files
services: files
ethers: files
rpc: files
NSSWITCH

cat > "$ROOTFS/etc/fstab" <<'FSTAB'
# KhaymOS development root: pseudo-filesystems are mounted by /init.
FSTAB

cat > "$ROOTFS/etc/securetty" <<'SECURETTY'
console
tty0
tty1
tty2
tty3
tty4
tty5
tty6
ttyS0
ttyS1
SECURETTY

echo
echo "=== Configuring PAM for local authentication ==="

mkdir -p "$ROOTFS/etc/pam.d"

cat > "$ROOTFS/etc/pam.d/system-auth" <<'PAM'
#%PAM-1.0

# Authenticate against the local password database.
auth     required    pam_unix.so

# Require a valid local account.
account  required    pam_unix.so

# Use the standard local password-change module.
password required    pam_unix.so

# Maintain PAM session state.
session  required    pam_unix.so
PAM

chmod 0644 "$ROOTFS/etc/pam.d/system-auth"

cat > "$ROOTFS/etc/shells" <<'SHELLS'
/bin/sh
/bin/bash
SHELLS

cat > "$ROOTFS/etc/os-release" <<'OSRELEASE'
NAME="KhaymOS"
ID=khaymos
PRETTY_NAME="KhaymOS Linux 0.1-dev"
VERSION="0.1-dev"
VERSION_ID="0.1"
OSRELEASE

cat > "$ROOTFS/etc/issue" <<'ISSUE'
KhaymOS Linux 0.1-dev \n \l

ISSUE

cat > "$ROOTFS/etc/motd" <<'MOTD'
Welcome to KhaymOS Linux.
MOTD

echo
echo "=== Installing the default KNPM mirrorlist ==="

if [[ ! -s "$TOP/config/official.mirrorlist" ]]; then
    echo "ERROR: official mirrorlist is missing: $TOP/config/official.mirrorlist" >&2
    exit 1
fi

install -m 0644 "$TOP/config/official.mirrorlist" "$ROOTFS/etc/knpm/mirrorlist"

if [[ ! -e "$ROOTFS/etc/login.defs" ]]; then
    cat > "$ROOTFS/etc/login.defs" <<'LOGINDEFS'
PASS_MAX_DAYS   99999
PASS_MIN_DAYS   0
PASS_WARN_AGE   7
UID_MIN         1000
UID_MAX         60000
GID_MIN         1000
GID_MAX         60000
CREATE_HOME     yes
USERGROUPS_ENAB yes
UMASK           022
ENCRYPT_METHOD  YESCRYPT
MAIL_DIR        /var/mail
LOGINDEFS
fi

echo
echo "=== Configuring OpenRC for the available packages ==="

# Keep unimplemented services disabled, but enable loopback and DHCP networking.
for level in boot default; do
    for service in hostname keymaps save-keymaps netmount; do
        rm -f "$ROOTFS/etc/runlevels/$level/$service"
    done
done

# netmount depends on a separate network-online service that is not packaged.
rm -f "$ROOTFS/etc/init.d/netmount"

if [[ ! -x "$ROOTFS/etc/init.d/loopback" ]]; then
    echo "ERROR: OpenRC loopback service is missing." >&2
    exit 1
fi
mkdir -p "$ROOTFS/etc/runlevels/boot"
ln -sfn /etc/init.d/loopback "$ROOTFS/etc/runlevels/boot/loopback"

cat > "$ROOTFS/etc/init.d/dhcpcd" <<'DHCPCD_INIT'
#!/sbin/openrc-run

description="DHCP client daemon"
command="/usr/sbin/dhcpcd"
command_args="${dhcpcd_args:-} -B"
command_background="yes"
pidfile="/run/dhcpcd.pid"

depend() {
    need localmount
    after bootmisc
}

start_pre() {
    mkdir -p /run/dhcpcd /var/lib/dhcpcd
}
DHCPCD_INIT
chmod 0755 "$ROOTFS/etc/init.d/dhcpcd"

cat > "$ROOTFS/etc/conf.d/dhcpcd" <<'DHCPCD_CONF'
dhcpcd_args="-q"
DHCPCD_CONF

if [[ ! -s "$ROOTFS/etc/dhcpcd.conf" ]]; then
    cat > "$ROOTFS/etc/dhcpcd.conf" <<'DHCPCD_CONFIG'
hostname
persistent
option domain_name_servers, domain_name, domain_search, host_name
option classless_static_routes
option interface_mtu
require dhcp_server_identifier
slaac private
DHCPCD_CONFIG
fi

mkdir -p "$ROOTFS/etc/runlevels/default"
ln -sfn /etc/init.d/dhcpcd "$ROOTFS/etc/runlevels/default/dhcpcd"

# Configure a normal serial login; no autologin or skipped authentication.
ln -sfn agetty "$ROOTFS/etc/init.d/agetty.ttyS0"

cat > "$ROOTFS/etc/conf.d/agetty.ttyS0" <<'AGETTY'
baud="115200"
term_type="vt100"
agetty_options="--noclear"
quiet="no"
AGETTY

ln -sfn /etc/init.d/agetty.ttyS0 \
    "$ROOTFS/etc/runlevels/default/agetty.ttyS0"

# Start D-Bus before elogind on the default runlevel.
ln -sfn /etc/init.d/dbus \
    "$ROOTFS/etc/runlevels/default/dbus"
ln -sfn /etc/init.d/elogind \
    "$ROOTFS/etc/runlevels/default/elogind"

# Ensure /run/dbus is writable by the D-Bus service account after /run
# has been mounted as tmpfs.
cat > "$ROOTFS/etc/init.d/dbus" <<'DBUS'
#!/usr/sbin/openrc-run

name="D-Bus System Message Bus"
command="/usr/bin/dbus-daemon"
pidfile="/run/dbus/pid"
command_args="${dbusd_args---system}"

depend() {
    need localmount
    after bootmisc
}

start_pre() {
    mkdir -p /run/dbus
    chown messagebus:messagebus /run/dbus
    chmod 0755 /run/dbus
}
DBUS
chmod 0755 "$ROOTFS/etc/init.d/dbus"

echo
echo "=== Preparing D-Bus machine ID ==="

# dbus-uuidgen --ensure rejects an existing empty machine-id file.
rm -f "$ROOTFS/etc/machine-id"

if command -v dbus-uuidgen >/dev/null 2>&1; then
    dbus-uuidgen --ensure="$ROOTFS/etc/machine-id"
else
    printf '%s\n' "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" \
        > "$ROOTFS/etc/machine-id"
fi

if ! grep -Eq '^[[:xdigit:]]{32}$' "$ROOTFS/etc/machine-id"; then
    echo "ERROR: invalid D-Bus machine ID" >&2
    exit 1
fi

chmod 0444 "$ROOTFS/etc/machine-id"
ln -sfn /etc/machine-id "$ROOTFS/var/lib/dbus/machine-id"

echo
echo "=== Creating initramfs entry point ==="

cat > "$ROOTFS/init" <<'INIT'
#!/bin/bash

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=linux

mkdir -p /dev /proc /sys /run /tmp

mount -t proc proc /proc ||
    echo "[WARN] Could not mount proc"
mount -t sysfs sysfs /sys ||
    echo "[WARN] Could not mount sysfs"
mount -t devtmpfs devtmpfs /dev ||
    echo "[WARN] Could not mount devtmpfs"

# devtmpfs replaces the original /dev contents, so create mount points now.
mkdir -p /dev/pts /dev/shm

mount -t devpts devpts /dev/pts ||
    echo "[WARN] Could not mount devpts"

mount -t tmpfs tmpfs /run ||
    echo "[WARN] Could not mount /run"
mount -t tmpfs tmpfs /tmp ||
    echo "[WARN] Could not mount /tmp"

printf 'khaymos\n' > /proc/sys/kernel/hostname ||
    echo "[WARN] Could not set hostname"

echo
echo "KhaymOS Linux 0.1-dev: starting OpenRC..."
exec /sbin/init
INIT
chmod 0755 "$ROOTFS/init"

echo
echo "=== Creating initramfs device nodes ==="

for node in console null tty; do
    if [[ -e "$ROOTFS/dev/$node" && ! -c "$ROOTFS/dev/$node" ]]; then
        rm -f "$ROOTFS/dev/$node"
    fi
done

[[ -c "$ROOTFS/dev/console" ]] ||
    mknod -m 600 "$ROOTFS/dev/console" c 5 1
[[ -c "$ROOTFS/dev/null" ]] ||
    mknod -m 666 "$ROOTFS/dev/null" c 1 3
[[ -c "$ROOTFS/dev/tty" ]] ||
    mknod -m 666 "$ROOTFS/dev/tty" c 5 0

echo
echo "=== Finalizing ownership and permissions ==="

# Real root files must be owned by root, not the KBuild developer account.
chown -hR root:root "$ROOTFS"

# Ownership changes can clear setuid/setgid bits. Restore those modes from
# their source package files so tools such as passwd keep their intended mode.
while IFS= read -r -d '' source_path; do
    relative="${source_path#"$SYSROOT"/}"
    destination="$ROOTFS/$relative"
    if [[ -e "$destination" && ! -L "$destination" ]]; then
        chmod --reference="$source_path" "$destination"
    fi
done < <(
    find "$SYSROOT" -xdev \
        \( -type f -o -type d \) -perm /6000 -print0
)

chmod 0700 "$ROOTFS/root"
chmod 1777 "$ROOTFS/tmp" "$ROOTFS/var/tmp"
chmod 0755 "$ROOTFS/dev" "$ROOTFS/proc" "$ROOTFS/sys" "$ROOTFS/run"
chmod 0644 "$ROOTFS/etc/passwd" "$ROOTFS/etc/group"
chmod 0640 "$ROOTFS/etc/shadow" "$ROOTFS/etc/gshadow"
chown root:42 "$ROOTFS/etc/shadow" "$ROOTFS/etc/gshadow"

# Service-account homes and data directories.
chown 81:81 "$ROOTFS/var/lib/dbus"
chmod 0755 "$ROOTFS/var/lib/dbus"
chown 68:68 "$ROOTFS/var/lib/dhcpcd"
chmod 0750 "$ROOTFS/var/lib/dhcpcd"
chown 102:102 "$ROOTFS/var/lib/polkit-1"
chmod 0750 "$ROOTFS/var/lib/polkit-1"

echo
echo "=== Root filesystem assembled ==="
echo "Path: $ROOTFS"
echo
echo "Kernel:"
ls -lh "$ROOTFS/boot/vmlinuz-khaymos-7.2.10"
echo
echo "Module count:"
find "$ROOTFS/lib/modules/7.2.10" -type f -name '*.ko*' | wc -l
echo
echo "Init and login paths:"
ls -l "$ROOTFS/init" "$ROOTFS/bin" "$ROOTFS/lib64" \
    "$ROOTFS/sbin/init" "$ROOTFS/sbin/agetty"
echo
echo "Root account is LOCKED until you set a password."
echo "Next command:"
echo "  sudo chroot '$ROOTFS' /usr/bin/passwd root"
