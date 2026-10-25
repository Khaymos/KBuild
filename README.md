## KBuild (KhaymOS Builder)
### Current version: 0.4

## KhaymOS live image

See [Networking](docs/NETWORKING.md) for Ethernet, USB tethering, Wi-Fi with
`iwd/iwctl`, and QEMU-specific networking notes.

The current ISO bootstrap packages include the wired-network tools and
wireless daemon. Building the full Wi-Fi firmware subset requires downloading
the upstream Linux firmware source archive, so allow for a large download when
building from a fresh cache.
