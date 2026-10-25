# Networking in KhaymOS

This page documents the wired and Wi-Fi networking tools included in the live
system. Networking setup is intentionally kept separate from the installer so
the installer can reuse the same commands and report the actual link state.

## Wired Ethernet and USB tethering

Check interfaces and routes:

```sh
ip -brief link
ip address
ip route
```

The OpenRC `dhcpcd` service obtains addresses from DHCP:

```sh
rc-service dhcpcd status
rc-service dhcpcd restart
```

Inspect DNS configuration and test HTTPS:

```sh
cat /etc/resolv.conf
curl -I https://github.com
```

A phone's USB tethering may appear as a new Ethernet-like interface if its
USB network mode and the corresponding kernel driver are supported. After
connecting the phone, run `ip -brief link` and restart `dhcpcd` if needed.

## Wi-Fi with iwd

The live rootfs starts the `iwd` daemon after D-Bus. IP addressing and DNS are
handled by `dhcpcd`, so iwd's network configuration feature is disabled in
`/etc/iwd/main.conf`.

Check the daemon and available radio devices:

```sh
rc-service iwd status
iwctl device list
```

Connect interactively (replace `wlan0` with the device name shown by `iwctl`):

```sh
iwctl station wlan0 scan
iwctl station wlan0 get-networks
iwctl station wlan0 connect "YOUR_WIFI_NAME"
```

Enter the Wi-Fi passphrase at the prompt. Then confirm the interface received
an address and has a route:

```sh
ip address show wlan0
ip route
cat /etc/resolv.conf
curl -I https://github.com
```

If no wireless device appears, check that the adapter is supported by the
running kernel and that its matching firmware is installed. Firmware is
hardware-specific; the bundled firmware subset covers several common Intel,
Atheros/Qualcomm, Broadcom/Cypress, MediaTek, and Realtek families, but it
cannot guarantee support for every adapter. Building that subset from the
upstream `linux-firmware-20261019.tar.xz` source currently downloads a 642 MB
archive before extracting only the selected Wi-Fi firmware files.

## QEMU note

A normal QEMU user-network adapter appears inside the guest as Ethernet (for
example, `eth0`), not as a Wi-Fi radio. The guest can use the host's internet
through this virtual Ethernet adapter. Testing `iwctl` requires passing a
compatible physical Wi-Fi device through to the VM, or booting KhaymOS on
hardware with a supported Wi-Fi adapter.

## Installer integration

The installer should not assume that an interface named `eth0` or `wlan0`
exists. It should enumerate `ip -brief link`, ask the user which wireless
device/network to use, run the appropriate commands, and verify connectivity
before downloading packages. If networking fails, installation should still
allow a local/offline path when all required packages are available locally.
