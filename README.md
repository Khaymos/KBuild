## KBuild (KhaymOS Builder)
### Current version: 0.4


## Optional package profiles

Recipes without a `profile` field belong to the default `base` build.
Recipes can opt into an additional profile by setting `profile = "name"`.
Optional recipes are excluded from `python3 kbuild.py build all` unless
a base recipe explicitly depends on them.

Build an optional profile with:

```sh
python3 kbuild.py build-profile <profile>
python3 kbuild.py build-profile <profile> --dry-run
```

Profile builds resolve dependencies in dependency-first order, reuse matching
cached or already-deployed packages, and deploy target packages to the sysroot.
Host packages are staged but never deployed to the target sysroot.

The native `knpm-host` helper belongs to the `iso` profile; it is used by
the ISO bootstrap process and is not installed into KhaymOS.

## KhaymOS live image

See [Networking](docs/NETWORKING.md) for Ethernet, USB tethering, Wi-Fi with
`iwd/iwctl`, and QEMU-specific networking notes.

The current ISO bootstrap packages include the wired-network tools and
wireless daemon. Building the full Wi-Fi firmware subset requires downloading
the upstream Linux firmware source archive, so allow for a large download when
building from a fresh cache.
